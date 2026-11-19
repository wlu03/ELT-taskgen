"""Compile a validated discovery graph, including its selected branches, into one mart."""

from dataclasses import asdict, dataclass
import json
from types import SimpleNamespace

from sqlglot.errors import ParseError

from elt_taskgen.generation.composite_evidence import ELIGIBLE_GROUP_REPORT
from elt_taskgen.generation.composite_plan import FragmentGraph, read_binding
from elt_taskgen.generation.composition_search import SEARCH_RECIPE, CompositionGraph
from elt_taskgen.generation.mart_plan import column_kinds_from_plan, detect_semantic_patterns, quote
from elt_taskgen.generation.recipe_admission import SemanticDifficultyError
from elt_taskgen.generation.recipe_evidence import RecipeEvidence
from elt_taskgen.models import (
    ColumnType, JoinType, MartColumn, MartColumnKind, MartPlan, MartSpec, canonical_json,
)


@dataclass(frozen=True)
class DiscoveredEvidence:
    source: RecipeEvidence
    graph: CompositionGraph

    @property
    def parent(self):
        return self.source.parent


def discovered_variants(candidate: DiscoveredEvidence, recipe: str) -> tuple[str, ...]:
    if recipe != SEARCH_RECIPE:
        raise ValueError(f"Unknown discovered recipe {recipe!r}")
    ops = {node.op for node in candidate.graph.nodes}
    variants = ["skip_eligibility", "inner_parent"]
    if "mean" in ops:
        variants.append("unfiltered_mean")
    if ops & {"above_mean", "below_mean"}:
        variants.extend(("reverse_threshold", "skip_threshold"))
    if ops & {"highest", "lowest"}:
        variants.extend(("reverse_choice", "reverse_tie"))
    if "count_groups" in ops:
        variants.append("wrong_group_count")
    if "compare" in ops:
        variants.append("reverse_comparison")
    return tuple(variants)


def _report_columns(candidate):
    source = candidate.source
    ops = [node.op for node in candidate.graph.nodes]
    scope = "eligible groups"
    if "above_mean" in ops or "below_mean" in ops:
        direction = "above" if "above_mean" in ops else "below"
        scope += f" with totals strictly {direction} the entity's mean eligible group total"
    if ops[-1] == "count_groups":
        return {"group_count": (ColumnType.BIGINT, f"Number of {scope}. Use zero when none qualify.")}
    direction = "largest" if "highest" in ops else "smallest"
    outputs = {
        "selected_group": (ColumnType.TEXT, f"Among {scope}, select the {source.group} with the {direction} total. "
                           "Break ties by the smallest group label in native text order. NULL if none qualify."),
        "selected_group_total": (ColumnType.FLOAT, f"Sum of {source.fact}.{source.measure} in the selected group, "
                                 "including repeated amounts. NULL when there is no selected group."),
    }
    if ops[-1] == "compare":
        outputs.update({
            "eligible_group_mean": (ColumnType.FLOAT, "Mean of the entity's eligible group totals, counting each "
                                    "group once, before any above- or below-mean filter. NULL if none are eligible."),
            "selected_minus_mean": (ColumnType.FLOAT, "Selected group total minus eligible_group_mean. "
                                    "NULL when no group is selected."),
        })
    return outputs


def _operations(task, candidate, variant):
    nodes = candidate.graph.canonical_nodes
    source = candidate.source
    graph = FragmentGraph(task)
    keys = tuple(f"g_{i}" for i in range(len(source.keys)))
    groups = (*keys, "group_label")
    parents = graph.source(source.parent, source.keys, keys, {}, "entities", unique=True)
    ports = []
    for node in nodes:
        inputs = tuple(ports[i] for i in node.inputs)
        op = node.op
        if op == "records":
            raw = graph.source(source.fact, source.links, keys, {
                "group_label": (source.group, "label"), "measurement": (source.measure, "number"),
            }, "fact_records")
            port = graph.filter(raw, "group_label IS NOT NULL", "known_groups")
        elif op == "summarize":
            port = graph.aggregate(inputs[0], groups, {
                "group_total": "SUM(measurement)", "measured_count": "COUNT(measurement)",
            }, "group_totals")
        elif op == "qualify":
            threshold = 1 if variant == "skip_eligibility" else 3
            port = graph.filter(inputs[0], f"measured_count >= {threshold}", "eligible_groups")
        elif op == "mean":
            population = inputs[0]
            if variant == "unfiltered_mean":
                # Bypass precisely this input's eligibility filter at its producer.
                eligibility = nodes[node.inputs[0]]
                population = ports[eligibility.inputs[0]]
            port = graph.aggregate(population, keys, {"eligible_group_mean": "AVG(group_total)"}, "group_mean")
        elif op in {"above_mean", "below_mean"}:
            compared = graph.attach(inputs[0], inputs[1], keys, ("eligible_group_mean",), "benchmarked_groups")
            greater = (op == "above_mean") != (variant == "reverse_threshold")
            predicate = "group_total > eligible_group_mean" if greater else "group_total < eligible_group_mean"
            port = inputs[0] if variant == "skip_threshold" else graph.filter(compared, predicate, "selected_groups")
        elif op in {"highest", "lowest"}:
            port = graph.winner(inputs[0], keys, "group_total", "group_label",
                                {"selected_group": "group_label", "selected_group_total": "group_total"},
                                "group_winner", highest=(op == "highest") != (variant == "reverse_choice"),
                                reverse_tie=variant == "reverse_tie")
        elif op == "count_groups":
            count = "SUM(measured_count)" if variant == "wrong_group_count" else "COUNT(*)"
            port = graph.aggregate(inputs[0], keys, {"group_count": count}, "group_counts")
        elif op == "compare":
            compared = graph.attach(inputs[1], inputs[0], keys, ("selected_group", "selected_group_total"),
                                    "benchmarked_winner")
            difference = ("eligible_group_mean - selected_group_total" if variant == "reverse_comparison"
                          else "selected_group_total - eligible_group_mean")
            port = graph.derive(compared, {"selected_minus_mean": difference}, "winner_difference")
        else:
            raise ValueError(f"Unsupported composition operation {op!r}")
        ports.append(port)
    columns = _report_columns(candidate)
    join = JoinType.INNER if variant == "inner_parent" else JoinType.LEFT
    report = graph.attach(parents, ports[-1], keys, tuple(columns), "complete_report", kind=join)
    select = [f"{quote(k)} AS {quote(v)}" for k, v in zip(keys, source.keys, strict=True)]
    for name, (kind, _) in columns.items():
        if kind is ColumnType.BIGINT:
            expr = f"CAST(COALESCE({quote(name)}, 0) AS BIGINT)"
        elif kind is ColumnType.FLOAT:
            expr = f"CAST({quote(name)} AS DOUBLE)"
        else:
            expr = quote(name)
        select.append(f"{expr} AS {quote(name)}")
    graph.builder.project(report.name, ", ".join(select), "report_output")
    return tuple(graph.builder.ops)


def read_discovered_binding(notes):
    data = json.loads(notes)
    if not isinstance(data, dict) or set(data) != {"evidence", "source_tables", "graph"}:
        raise ValueError("Invalid discovered graph binding")
    graph = CompositionGraph.from_dict(data.pop("graph"))
    source, tables = read_binding(canonical_json(data), ELIGIBLE_GROUP_REPORT)
    if not {source.fact, source.parent} <= set(tables):
        raise ValueError("Missing discovered graph source tables")
    return DiscoveredEvidence(source, graph), tables


def certifies_discovered(plan: MartPlan, recipe: str = SEARCH_RECIPE) -> bool:
    if recipe != SEARCH_RECIPE or plan.template_id != recipe:
        return False
    try:
        candidate, names = read_discovered_binding(plan.notes)
        task = SimpleNamespace(tables=tuple(SimpleNamespace(name=n) for n in names))
        return plan.ops == _operations(task, candidate, "")
    except (TypeError, ValueError, KeyError, AttributeError, ParseError):
        return False


def discovered_plan(task, candidate, mart, recipe, variant=""):
    if recipe != SEARCH_RECIPE or (variant and variant not in discovered_variants(candidate, recipe)):
        raise ValueError(f"Invalid discovered graph recipe or mutation {recipe!r}, {variant!r}")
    source = candidate.source
    outputs = _report_columns(candidate)
    if {k.casefold() for k in source.keys} & outputs.keys():
        raise SemanticDifficultyError("Source key collides with discovered report output")
    notes = canonical_json({"evidence": asdict(source), "graph": candidate.graph.as_dict(),
                            "source_tables": sorted(t.name for t in task.tables)})
    plan = MartPlan(mart=mart, template_id=recipe, notes=notes, ops=_operations(task, candidate, variant))
    plan = plan.model_copy(update={"semantic_patterns": detect_semantic_patterns(plan)})
    kinds = column_kinds_from_plan(plan)
    parent = task.table(source.parent)
    columns = [MartColumn(name=k, type=parent.column(k).type, description=f"Unchanged {parent.name}.{k}.",
                          kind=kinds.get(k, MartColumnKind.PASSTHROUGH)) for k in source.keys]
    columns.extend(MartColumn(name=name, type=kind, description=text,
                              kind=kinds.get(name, MartColumnKind.DERIVED))
                   for name, (kind, text) in outputs.items())
    return MartSpec(
        name=mart, plan=plan, key_columns=source.keys, columns=tuple(columns),
        grain=f"One row per {source.parent} {source.keys}, including entities without qualifying activity.",
        description=f"Match {source.fact} {source.links} to {source.parent} {source.keys}. Within each entity, "
                    f"group by non-NULL {source.fact}.{source.group}. A group needs at least three non-NULL "
                    f"{source.measure} measurements to be eligible. Its total sums all non-NULL measurements "
                    "including repeated values.",
    )

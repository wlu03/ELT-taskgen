"""Compose source-bound fragments into a single executable mart and certify its edges."""

from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields, replace
import json
from types import SimpleNamespace
from typing import Literal

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from elt_taskgen.generation.composite_evidence import DOCUMENT_BALANCE, ELIGIBLE_GROUP_REPORT
from elt_taskgen.generation.mart_plan import column_kinds_from_plan, detect_semantic_patterns, quote
from elt_taskgen.generation.recipe_evidence import RecipeEvidence
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.generation.scenario_evidence import ScenarioEvidence
from elt_taskgen.generation.scenario_financial import money
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen.models import (
    ColumnType, JoinType, MartColumn, MartColumnKind, MartOpKind, MartPlan, MartSpec, TaskIR, canonical_json,
)


Role = Literal["key", "label", "number"]
Evidence = RecipeEvidence | ScenarioEvidence


@dataclass(frozen=True)
class RelationPort:
    """Columns available to consumers and the key that determines a row.

    A raw stream can repeat its foreign keys, so its grain is unknown (None).
    Only a relation with an explicitly proven grain can enrich another relation.
    """
    name: str
    grain: tuple[str, ...] | None
    columns: dict[str, Role]

    def require(self, names: Iterable[str], role: Role | None = None) -> None:
        for name in names:
            if name not in self.columns or (role is not None and self.columns[name] != role):
                raise ValueError(f"{self.name} does not supply {name!r} with required role {role!r}")

    def check_expression(self, expression: str) -> exp.Expression:
        parsed = parse_one(expression, read="duckdb")
        self.require(c.name for c in parsed.find_all(exp.Column))
        return parsed


class FragmentGraph:
    """Lower fragments in dependency order. Passing a port twice shares its CTE."""
    def __init__(self, task):
        self.builder = PlanBuilder(task)
        # The compiler's target resolves quoted table names case-insensitively.
        self.builder.taken.update(t.name.casefold() for t in task.tables)

    def source(
        self, table: str, keys: tuple[str, ...], aliases: tuple[str, ...],
        values: dict[str, tuple[str, Role]], stem: str, *, unique: bool = False,
    ) -> RelationPort:
        if not keys or not all(keys) or len(set(aliases)) != len(aliases) or set(aliases) & values.keys():
            raise ValueError("A source fragment needs a complete key and distinct aliases")
        projection = [f"{quote(k)} AS {quote(a)}" for k, a in zip(keys, aliases, strict=True)]
        projection.extend(f"{quote(source)} AS {quote(a)}" for a, (source, _) in values.items())
        columns = {**dict.fromkeys(aliases, "key"), **{a: role for a, (_, role) in values.items()}}
        name = self.builder.project(table, ", ".join(projection), stem)
        return RelationPort(name, aliases if unique else None, columns)

    def filter(self, source: RelationPort, predicate: str, stem: str) -> RelationPort:
        source.check_expression(predicate)
        return replace(source, name=self.builder.filter(source.name, predicate, stem))

    def aggregate(
        self, source: RelationPort, grain: tuple[str, ...], measures: dict[str, str], stem: str,
    ) -> RelationPort:
        source.require(grain)
        for expression in measures.values():
            parsed = source.check_expression(expression)
            for aggregate in parsed.find_all(exp.Sum, exp.Avg):
                source.require((c.name for c in aggregate.find_all(exp.Column)), "number")
        name = self.builder.aggregate(source.name, grain, measures, stem)
        columns = {**{k: source.columns[k] for k in grain}, **dict.fromkeys(measures, "number")}
        return RelationPort(name, grain, columns)

    def derive(self, source: RelationPort, expressions: dict[str, str], stem: str) -> RelationPort:
        """Keep existing columns and add numeric expressions with explicit aliases."""
        if source.columns.keys() & expressions.keys():
            raise ValueError("Derived fragment aliases must be new")
        for expression in expressions.values():
            source.check_expression(expression)
        select = ", ".join(("*", *(f"{v} AS {quote(k)}" for k, v in expressions.items())))
        name = self.builder.project(source.name, select, stem)
        return RelationPort(name, source.grain, {**source.columns, **dict.fromkeys(expressions, "number")})

    def attach(
        self, left: RelationPort, right: RelationPort, keys: tuple[str, ...],
        carried: tuple[str, ...], stem: str, *, kind: JoinType = JoinType.LEFT,
    ) -> RelationPort:
        if right.grain != keys:
            raise ValueError(f"Expected one {right.name} row per {keys}, found grain {right.grain}")
        left.require(keys)
        right.require((*keys, *carried))
        if any(left.columns[k] != right.columns[k] for k in keys) or left.columns.keys() & set(carried):
            raise ValueError(f"Incompatible columns for joining {left.name} and {right.name}")
        name = self.builder.join(left.name, right.name, keys, carried, stem, kind=kind)
        return RelationPort(name, left.grain, {**left.columns, **{k: right.columns[k] for k in carried}})

    def winner(
        self, groups: RelationPort, keys: tuple[str, ...], score: str, label: str,
        outputs: dict[str, str], stem: str, *, highest: bool = True, reverse_tie: bool = False,
    ) -> RelationPort:
        """Choose one row per key, ordering tied scores by its unique group label."""
        if groups.grain != (*keys, label):
            raise ValueError("Ranking requires one row per key and tie-breaking label")
        groups.require((label,), "label")
        groups.require((score,), "number")
        groups.require(outputs.values())
        if set(keys) & outputs.keys():
            raise ValueError("Ranked fragment outputs overlap the partition key")
        projection = [*map(quote, keys), *(f"{quote(v)} AS {quote(k)}" for k, v in outputs.items())]
        order = "DESC" if highest else "ASC"
        tie = "DESC" if reverse_tie else "ASC"
        name = self.builder.add(
            MartOpKind.EXTREMA, (groups.name,), stem, select=", ".join(projection),
            details={"partition_by": ", ".join(map(quote, keys)),
                     "order_by": f"{quote(score)} {order} NULLS LAST, {quote(label)} {tie} NULLS LAST",
                     "tie_break": label},
        )
        columns = {k: groups.columns[k] for k in keys}
        columns.update({k: groups.columns[v] for k, v in outputs.items()})
        return RelationPort(name, keys, columns)


def composite_variants(evidence: Evidence, recipe: str) -> tuple[str, ...]:
    if recipe == ELIGIBLE_GROUP_REPORT:
        return ("rank_unfiltered", "baseline_unfiltered", "reverse_tie", "inner_parent", "distinct_group_values")
    if recipe == DOCUMENT_BALANCE:
        missing = tuple(f"missing_key_{i}" for i in range(len(evidence.keys)) if len(evidence.keys) > 1)
        return ("raw_fanout", "distinct_payments", "exclude_empty_baseline", *missing)
    raise ValueError(f"Unknown composite recipe {recipe!r}")


def _eligible_report(graph, parents, e, keys, variant):
    facts = graph.source(e.fact, e.links, keys, {"group_value": (e.group, "label"),
                         "measure_value": (e.measure, "number")}, "activity_records")
    known = graph.filter(facts, "group_value IS NOT NULL", "known_groups")
    total = "SUM(DISTINCT measure_value)" if variant == "distinct_group_values" else "SUM(measure_value)"
    groups = graph.aggregate(known, (*keys, "group_value"), {"group_total": total,
                             "measured_count": "COUNT(measure_value)"}, "group_totals")
    eligible = graph.filter(groups, "measured_count >= 3", "eligible_groups")
    ranked = groups if variant == "rank_unfiltered" else eligible
    measured = groups if variant == "baseline_unfiltered" else eligible
    winner = graph.winner(
        ranked, keys, "group_total", "group_value",
        {"selected_group": "group_value", "selected_group_total": "group_total"},
        "winning_group", reverse_tie=variant == "reverse_tie",
    )
    mean = graph.aggregate(measured, keys, {"eligible_group_mean": "AVG(group_total)"}, "eligible_mean")
    kind = JoinType.INNER if variant == "inner_parent" else JoinType.LEFT
    report = graph.attach(parents, winner, keys, ("selected_group", "selected_group_total"), "entity_winners", kind=kind)
    report = graph.attach(report, mean, keys, ("eligible_group_mean",), "entity_benchmarks")
    return graph.derive(report, {"above_group_mean": "selected_group_total - eligible_group_mean"}, "group_comparison")


def _document_report(graph, parents, e, keys, variant):
    lines = graph.source(e.fact, e.links, keys, {"line_value": (e.debit, "number")}, "lines")
    payments = graph.source(e.other, e.other_links, keys, {"paid_value": (e.other_amount, "number")}, "payments")
    if variant == "raw_fanout":
        # Deliberately break the cardinality contract before the line reduction.
        name = graph.builder.join(lines.name, payments.name, keys, ("paid_value",), "raw_transactions")
        lines = replace(lines, name=name, columns={**lines.columns, "paid_value": "number"})
    line_keys = tuple(k for i, k in enumerate(keys) if variant != f"missing_key_{i}")
    line_sums = graph.aggregate(lines, line_keys, {"line_sum": f"SUM({money('line_value')})"}, "line_totals")
    distinct = "DISTINCT " if variant == "distinct_payments" else ""
    paid_sum = f"SUM({distinct}{money('paid_value')})"
    payment_sums = graph.aggregate(payments, keys, {"paid_sum": paid_sum}, "payment_totals")
    report = graph.attach(parents, line_sums, line_keys, ("line_sum",), "document_lines")
    report = graph.attach(report, payment_sums, keys, ("paid_sum",), "document_payments")
    report = graph.derive(report, {"line_total": "COALESCE(line_sum, 0)", "paid_total": "COALESCE(paid_sum, 0)",
                                   "population_scope": "1"}, "complete_documents")
    gaps = graph.derive(report, {"line_payment_gap": "line_total - paid_total"}, "document_gaps")
    population = (graph.filter(gaps, "line_sum IS NOT NULL OR paid_sum IS NOT NULL", "active_documents")
                  if variant == "exclude_empty_baseline" else gaps)
    scope = ("population_scope",)
    mean = graph.aggregate(population, scope, {"mean_document_gap": "AVG(line_payment_gap)"}, "population_gap")
    compared = graph.attach(gaps, mean, scope, ("mean_document_gap",), "benchmarked_documents")
    return graph.derive(compared, {"above_mean_gap": "line_payment_gap - mean_document_gap"}, "gap_comparison")


def _outputs(e, recipe):
    if recipe == ELIGIBLE_GROUP_REPORT:
        return {
            "selected_group": (ColumnType.TEXT, f"{e.fact}.{e.group} with the largest total {e.measure} among this "
                               "entity's eligible groups. Ignore NULL group labels. A group needs at least three "
                               "non-NULL measurements. Ties select the smallest label in native text order."),
            "selected_group_total": (ColumnType.FLOAT, f"Sum of non-NULL {e.fact}.{e.measure} in the selected group, "
                                     "counting repeated amounts separately."),
            "eligible_group_mean": (ColumnType.FLOAT, "Arithmetic mean of this entity's eligible group totals. "
                                    "Each eligible group contributes exactly once, including the winner."),
            "above_group_mean": (ColumnType.FLOAT, "Selected group total minus this entity's eligible group mean."),
        }
    return {
        "line_total": (ColumnType.FLOAT, f"Sum of {e.fact}.{e.debit} for this document, counting duplicate "
                       "amounts separately. Ignore NULL amounts and use zero when none contribute."),
        "paid_total": (ColumnType.FLOAT, f"Sum of {e.other}.{e.other_amount} for this document, counting duplicate "
                       "amounts separately. Ignore NULL amounts and use zero when none contribute."),
        "line_payment_gap": (ColumnType.FLOAT, "Signed difference between line_total and paid_total."),
        "mean_document_gap": (ColumnType.FLOAT, f"Arithmetic mean of line_payment_gap across all {e.parent} "
                              "documents, including documents with neither lines nor payments. Each document counts once."),
        "above_mean_gap": (ColumnType.FLOAT, "This document's line_payment_gap minus mean_document_gap."),
    }


def _build_ops(task, e, recipe, variant):
    graph = FragmentGraph(task)
    keys = tuple(f"g_{i}" for i in range(len(e.keys)))
    parents = graph.source(e.parent, e.keys, keys, {}, "entity_keys", unique=True)
    if recipe == ELIGIBLE_GROUP_REPORT and isinstance(e, RecipeEvidence):
        report = _eligible_report(graph, parents, e, keys, variant)
    elif recipe == DOCUMENT_BALANCE and isinstance(e, ScenarioEvidence):
        report = _document_report(graph, parents, e, keys, variant)
    else:
        raise ValueError(f"Invalid source binding for {recipe!r}")
    outputs = _outputs(e, recipe)
    report.require(keys)
    if report.grain != keys:
        raise ValueError("Final fragment does not preserve the parent grain")
    projections = [f"{quote(g)} AS {quote(k)}" for g, k in zip(keys, e.keys, strict=True)]
    for name, (kind, _) in outputs.items():
        numeric = kind is ColumnType.FLOAT
        report.require((name,), "number" if numeric else "label")
        expression = f"CAST({quote(name)} AS DOUBLE)" if numeric else quote(name)
        projections.append(f"{expression} AS {quote(name)}")
    graph.builder.project(report.name, ", ".join(projections), "mart_output")
    return tuple(graph.builder.ops)


def read_binding(notes: str, recipe: str):
    binding_type = {ELIGIBLE_GROUP_REPORT: RecipeEvidence, DOCUMENT_BALANCE: ScenarioEvidence}[recipe]
    binding = json.loads(notes)
    if not isinstance(binding, dict) or set(binding) != {"evidence", "source_tables"}:
        raise ValueError("Invalid composite binding")
    data, sources = binding["evidence"], binding["source_tables"]
    if not isinstance(data, dict) or set(data) != {f.name for f in fields(binding_type)}:
        raise ValueError("Invalid composite evidence")
    if not isinstance(sources, list) or not all(isinstance(name, str) for name in sources):
        raise ValueError("Invalid composite source tables")
    for field in fields(binding_type):
        value = data[field.name]
        if field.type == tuple[str, ...]:
            if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
                raise ValueError(f"Invalid composite key {field.name}")
            data[field.name] = tuple(value)
        elif not isinstance(value, field.type):
            raise ValueError(f"Invalid composite binding field {field.name}")
    return binding_type(**data), tuple(sources)


def certifies_composite(plan: MartPlan, recipe: str) -> bool:
    """Reconstruct both live branches from the binding and compare the full graph."""
    if plan.template_id != recipe:
        return False
    try:
        evidence, sources = read_binding(plan.notes, recipe)
        if not set((evidence.parent, evidence.fact)) <= set(sources):
            return False
        if recipe == DOCUMENT_BALANCE and evidence.other not in sources:
            return False
        task = SimpleNamespace(tables=tuple(SimpleNamespace(name=name) for name in sources))
        return plan.ops == _build_ops(task, evidence, recipe, "")
    except (KeyError, ValueError, TypeError, AttributeError, ParseError):
        return False


def composite_plan(task: TaskIR, evidence: Evidence, mart: str, recipe: str, variant: str = "") -> MartSpec:
    if variant and variant not in composite_variants(evidence, recipe):
        raise ValueError(f"Unknown {recipe} mutation {variant!r}")
    ops = _build_ops(task, evidence, recipe, variant)
    outputs = _outputs(evidence, recipe)
    if {name.casefold() for name in evidence.keys} & set(outputs):
        raise SemanticDifficultyError("Source keys collide with composite report columns")
    notes = canonical_json({"evidence": asdict(evidence), "source_tables": sorted(t.name for t in task.tables)})
    plan = MartPlan(mart=mart, template_id=recipe, ops=ops, notes=notes)
    plan = plan.model_copy(update={"semantic_patterns": detect_semantic_patterns(plan)})
    kinds = column_kinds_from_plan(plan)
    entity = task.table(evidence.parent)
    columns = [MartColumn(name=k, type=entity.column(k).type, description=f"Component of the complete {entity.name} key.",
                          kind=kinds.get(k, MartColumnKind.PASSTHROUGH)) for k in evidence.keys]
    columns.extend(MartColumn(name=name, type=kind, description=text,
                   kind=kinds.get(name, MartColumnKind.DERIVED)) for name, (kind, text) in outputs.items())
    relationship = f"Match {evidence.fact} {evidence.links} to {entity.name} {evidence.keys}."
    if recipe == ELIGIBLE_GROUP_REPORT:
        description = f"Winning activity group and eligible group average per entity. {relationship} " \
                      "All four report values are NULL when the entity has no eligible group."
    else:
        description = f"Compare document lines and payments against the population mean difference. {relationship} " \
                      f"Independently match {evidence.other} {evidence.other_links} to the same complete document key. " \
                      "For both branches, convert each amount to decimal with nine fractional digits before summing."
    return MartSpec(name=mart, columns=tuple(columns), key_columns=evidence.keys, plan=plan, description=description,
                    grain=f"One row for every {entity.name} {evidence.keys}, including parents without matching records.")

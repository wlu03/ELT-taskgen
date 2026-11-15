"""One public contract and reconstructible graph for each source-backed scenario."""
from dataclasses import asdict, fields
import json
from types import SimpleNamespace

from elt_taskgen.generation.mart_plan import column_kinds_from_plan, detect_semantic_patterns
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.generation.scenario_evidence import (
    ScenarioEvidence, SCENARIO_RECIPES, INTERVAL, SESSION, LIFECYCLE, DOCUMENT, LEDGER, HIERARCHY,
)
from elt_taskgen.models import ColumnType as T, MartColumn, MartColumnKind, MartPlan, MartSpec, SemanticPattern, canonical_json

PATTERNS = {INTERVAL: SemanticPattern.INTERVAL_COVERAGE, SESSION: SemanticPattern.SESSIONIZATION,
    LIFECYCLE: SemanticPattern.ACTIVITY_LIFECYCLE, DOCUMENT: SemanticPattern.DOCUMENT_RECONCILIATION,
    LEDGER: SemanticPattern.LEDGER_RECONCILIATION, HIERARCHY: SemanticPattern.RECURSIVE_HIERARCHY}


def scenario_variants(e, recipe):
    variants = {
        INTERVAL: ("sum_overlaps", "previous_end_only", "no_clip", "inner_parent"),
        SESSION: ("inclusive_gap", "global_partition", "reverse_ties"),
        LIFECYCLE: ("observed_only", "restart_is_new", "before_creation", "thirty_day_month"),
        DOCUMENT: ("raw_fanout", "sum_distinct", "inner_parent"),
        LEDGER: ("global_balance", "sum_distinct", "ignore_credit_excess", *(("exclusive_tolerance",) if e.fractional else ()), "inner_parent"),
        HIERARCHY: ("one_hop", "count_paths", "include_self", "inner_parent"),
    }[recipe]
    return (*variants, *(f"missing_key_{i}" for i in range(len(e.keys)) if len(e.keys)>1 and recipe != HIERARCHY))


def build_scenario_ops(task, e, recipe, variant=""):
    from elt_taskgen.generation.scenario_temporal import interval_ops, session_ops, lifecycle_ops
    from elt_taskgen.generation.scenario_financial import ledger_ops, document_ops
    from elt_taskgen.generation.scenario_graph import hierarchy_ops
    b = PlanBuilder(task)
    {INTERVAL: interval_ops, SESSION: session_ops, LIFECYCLE: lifecycle_ops,
     DOCUMENT: document_ops, LEDGER: ledger_ops, HIERARCHY: hierarchy_ops}[recipe](b, e, variant)
    return tuple(b.ops)


def read_binding(notes):
    raw = json.loads(notes)
    if set(raw) != {"evidence", "source_tables"} or set(raw["evidence"]) != {f.name for f in fields(ScenarioEvidence)}:
        raise ValueError("Invalid scenario binding")
    if not isinstance(raw["source_tables"], list) or not all(isinstance(s, str) for s in raw["source_tables"]):
        raise ValueError("Invalid scenario sources")
    for field in ("keys", "links", "row_keys", "other_links", "targets"):
        value = raw["evidence"][field]
        if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
            raise ValueError("Invalid key tuple")
        raw["evidence"][field] = tuple(value)
    return ScenarioEvidence(**raw["evidence"]), tuple(raw["source_tables"])


def certifies_scenario(plan, recipe):
    if plan.template_id != recipe or recipe not in SCENARIO_RECIPES:
        return False
    try:
        e, sources = read_binding(plan.notes)
        task = SimpleNamespace(tables=tuple(SimpleNamespace(name=n) for n in sources))
        return plan.ops == build_scenario_ops(task, e, recipe)
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def scenario_plan(task, e, mart, recipe, variant=""):
    if variant and variant not in scenario_variants(e, recipe):
        raise ValueError(f"Unknown {recipe} mutation {variant}")
    keys = e.row_keys if recipe == SESSION else e.keys
    source = task.table(e.fact if recipe == SESSION else e.parent)
    columns = [MartColumn(name=k, type=source.column(k).type,
        description=f"Unchanged {source.name}.{k}; part of the complete record key {keys}.") for k in keys]
    def col(name, kind, description):
        columns.append(MartColumn(name=name, type=kind, description=description))
    relation = f"Join every component of {e.fact}.{e.links} to {e.parent}.{e.keys}; ignore unmatched facts and links with any NULL component."
    grain = f"One row per {e.parent} {e.keys}, including records without associated facts."
    if recipe == INTERVAL:
        description = f"Distinct time covered by {e.fact} activity within each {e.parent} reporting interval."
        rules = (f"{relation} Use half-open stored intervals [{e.start}, {e.end}) and parent windows "
            f"[{e.parent_start}, {e.parent_end}), without timezone conversion. Exclude NULL endpoints, zero/reversed intervals, "
            "and intervals not overlapping the parent window. Clip both ends to the parent window. Invalid parent windows return zero.")
        col("interval_count", T.BIGINT, "Number of qualifying source interval records before merging overlaps, including identical records with different keys. " + rules)
        col("covered_seconds", T.FLOAT, "Elapsed seconds in the union of clipped intervals; overlapping or nested time counts once. Touching intervals add no duplicate time. Preserve fractional seconds. 0.0 without valid intervals. " + rules)
    elif recipe == SESSION:
        description = f"Ordered inactivity bursts within each {e.parent} using {e.fact}.{e.start}."
        grain = f"One row per matched {e.fact} record with non-NULL {e.start}, keyed by {keys}."
        ordering = (f"{relation} Exclude NULL event times. Within each complete parent key, sort by {e.start} ascending, "
                    f"then complete {e.row_keys} ascending under native database ordering. Use stored timestamps without timezone conversion. "
                    "A gap strictly greater than 30 minutes from the immediately preceding event starts a new burst; exactly 30 minutes stays in the same burst.")
        col("burst_number", T.BIGINT, "Consecutive burst number starting at 1 separately for each parent. " + ordering)
        col("position_in_burst", T.BIGINT, "Consecutive record position starting at 1 within its burst, using the same total event order. " + ordering)
        col("burst_event_count", T.BIGINT, "Count of all event records in that burst, repeated for every member. Equal timestamps remain separate records. " + ordering)
    elif recipe == LIFECYCLE:
        description = f"Six calendar months of {e.parent} activity and transitions through {e.fact}."
        grain = f"One row per complete {e.parent} key and month_index 0 through 5, including inactive months. Exclude NULL {e.parent_start}."
        keys = (*keys, "month_index")
        col("month_index", T.INTEGER, f"Calendar month offset from {e.parent}.{e.parent_start}'s month, 0 through 5 inclusive. Use stored dates; December to January is one month, regardless of elapsed days.")
        col("event_count", T.BIGINT, f"Number of associated {e.fact} rows in this calendar month with non-NULL {e.start} at or after the parent's {e.parent_start}. Repeated events count separately. Zero for none. {relation}")
        col("activity_state", T.TEXT, "Use the complete six-month series, treating the prior count before month 0 as zero. Apply first matching rule: positive count with no earlier positive month => 'new'; positive after a zero prior month => 'reactivated'; zero after a positive prior month => 'stopped'; count greater than prior => 'increased'; lower than prior => 'decreased'; equal positive => 'unchanged'; otherwise 'inactive'.")
    elif recipe == LEDGER:
        description = f"Per-entry debit/credit reconciliation of {e.fact} for every {e.parent}."
        col("total_debit", T.DECIMAL, f"Sum of {e.fact}.{e.debit}, treating NULL as zero and counting equal values separately. Retain negative amounts. Use decimal arithmetic with nine fractional digits before summing or subtracting amounts. {relation}")
        col("total_credit", T.DECIMAL, f"Sum of {e.fact}.{e.credit} under the same decimal arithmetic rules as total_debit. Zero for no lines.")
        col("line_count", T.BIGINT, "Number of matching journal lines, including lines with NULL or zero debit/credit; zero for no lines.")
        col("imbalance", T.DECIMAL, "total_debit minus total_credit using decimal arithmetic, rounded to 4 decimal places. Keep the sign; zero for no lines.")
        col("is_balanced", T.BOOLEAN, "True exactly when ABS(imbalance) <= 0.01, inclusive, separately for this entry. Opposite imbalances on other entries cannot cancel this entry's error. An empty entry is balanced.")
    elif recipe == DOCUMENT:
        description = f"Independent line and payment totals with remaining balances for every {e.parent}."
        col("document_amount", T.DECIMAL, f"{e.parent}.{e.amount}, with NULL treated as zero and negatives retained. Use decimal arithmetic with nine fractional digits for amounts before summing or subtracting them.")
        col("line_total", T.DECIMAL, f"Sum of non-NULL {e.fact}.{e.debit} using decimal arithmetic, counting equal values separately. Zero for none. {relation} This informational line total is not assumed equal to the header total, which can include tax or other charges.")
        col("paid_amount", T.DECIMAL, f"Sum of non-NULL {e.other}.{e.other_amount} using decimal arithmetic, joining all {e.other_links} to {e.parent}.{e.keys}. Count equal payments separately and retain negative reversals; zero for none. Aggregate independently from line records to avoid multiplication.")
        col("line_count", T.BIGINT, "Number of associated line records, including NULL amounts; zero for none.")
        col("payment_count", T.BIGINT, "Number of associated payment records, including NULL amounts; zero for none.")
        col("remaining_amount", T.DECIMAL, "document_amount minus paid_amount using decimal arithmetic, rounded to 4 places; negative means overpaid. Do not derive the balance from line_total.")
        col("payment_status", T.TEXT, "Apply first matching rule: ABS(remaining_amount)<=0.01 => 'settled'; remaining_amount<0 => 'overpaid'; payment_count=0 => 'unpaid'; otherwise 'outstanding'.")
    else:
        description = f"Transitive descendants and cycle membership in the {e.parent} hierarchy."
        rules = (f"Each {e.fact} row defines a directed edge from tuple {e.links} to {e.targets}. Both endpoints must "
            f"match a complete {e.parent}.{e.keys}; ignore NULL or orphan endpoints. Deduplicate identical edges. Traverse any "
            "positive number of edges with no fixed depth limit. Repeated paths do not multiply reachable nodes; cycles must terminate.")
        col("reachable_count", T.BIGINT, "Number of distinct OTHER nodes reachable from this node; exclude the starting node even if a cycle returns to it. Zero for isolated nodes or leaves. " + rules)
        col("is_in_cycle", T.BOOLEAN, "True when at least one nonempty directed path returns to this starting node, including a direct self-loop. Reaching a cycle that does not return to the starting node is insufficient. " + rules)
    if len({c.name for c in columns}) != len(columns):
        from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
        raise SemanticDifficultyError("Source keys collide with scenario outputs")
    plan = MartPlan(mart=mart, ops=build_scenario_ops(task, e, recipe, variant), template_id=recipe,
        notes=canonical_json({"evidence": asdict(e), "source_tables": sorted(t.name for t in task.tables)}))
    plan = plan.model_copy(update={"semantic_patterns": detect_semantic_patterns(plan)})
    if not variant and PATTERNS[recipe] not in plan.semantic_patterns:
        raise ValueError(f"Uncertified scenario {recipe}")
    kinds = column_kinds_from_plan(plan)
    columns = tuple(c.model_copy(update={"kind": kinds.get(c.name,
        MartColumnKind.PASSTHROUGH if c.name in keys else MartColumnKind.DERIVED)}) for c in columns)
    return MartSpec(name=mart, description=description, grain=grain, key_columns=keys, columns=columns, plan=plan)

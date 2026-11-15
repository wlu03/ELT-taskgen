"""Schema-driven composed marts, shared by every generation source.

Admission is executable: a candidate must have legal counterfactual rows and
every declared semantic mutant must run and change the output. Existing task
populations and marts are retained; only the counterfactual receives new rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re

from elt_taskgen.models import (
    AttackCase, AttackKind, ColumnType, JoinType, MartColumn, MartColumnKind,
    MartOp, MartOpKind, MartPlan, MartSpec, PopulationName, TaskIR, canonical_json,
)
from elt_taskgen.generation.mart_plan import quote
from elt_taskgen.generation.recipe_admission import RecipeFamily, SemanticDifficultyError, _merged_counterfactual


TEMPLATE = "semantic_rollup_v1"
NUMERIC = frozenset((ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL))
MEASURE_WORDS = frozenset((
    "amount", "price", "cost", "revenue", "duration", "milliseconds", "seconds",
    "quantity", "weight", "distance", "length", "score", "points", "rating",
    "salary", "balance", "total", "value", "count", "volume", "size", "hours", "minutes", "days",
))




@dataclass(frozen=True)
class SemanticEvidence:
    parent: str
    keys: tuple[str, ...]
    fact: str
    links: tuple[str, ...]
    row_key: tuple[str, ...]
    measure: str
    group: str
    dimension: str = ""
    dimension_key: str = ""
    dimension_label: str = ""
    dimension_optional: bool = False
    second_fact: str = ""
    second_links: tuple[str, ...] = ()


def _words(name: str) -> set[str]:
    separated = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    return set(re.findall(r"[a-z]+", separated.casefold()))


def semantic_candidates(task: TaskIR) -> tuple[SemanticEvidence, ...]:
    """Use declared keys/FKs and descriptive measure names; never numeric IDs."""
    tables = {t.name: t for t in task.tables}
    found = []
    for rel in sorted(task.relationships, key=lambda r: (r.parent_table, r.child_table, r.child_columns)):
        parent, fact = tables[rel.parent_table], tables[rel.child_table]
        if parent.name == fact.name:
            continue
        if rel.parent_columns not in (parent.primary_key, parent.business_key):
            continue
        if any(parent.column(k).enum_values or parent.column(k).type in (ColumnType.BOOLEAN, ColumnType.JSON) for k in rel.parent_columns):
            continue
        if any(fact.column(c).type != parent.column(p).type for c, p in zip(rel.child_columns, rel.parent_columns, strict=True)):
            continue
        foreign = {c for r in task.relationships if r.child_table == fact.name for c in r.child_columns}
        identity = set(fact.primary_key) | set(fact.business_key) | foreign
        measures = [c for c in fact.columns if c.type in NUMERIC and c.name not in identity and not c.enum_values
                    and not (_words(c.name) & {"id", "key", "code", "position", "rank", "year"})
                    and (_words(c.name) | _words(c.description)) & MEASURE_WORDS]
        measures.sort(key=lambda c: (-len(_words(c.name) & MEASURE_WORDS), c.name))
        groups = []
        for edge in task.relationships:
            if edge.child_table != fact.name or edge.parent_table in (fact.name, parent.name) or len(edge.child_columns) != 1:
                continue
            dimension = tables[edge.parent_table]
            if edge.parent_columns not in (dimension.primary_key, dimension.business_key):
                continue
            key = edge.parent_columns[0]
            labels = [c.name for c in dimension.columns if c.type is ColumnType.TEXT and c.name not in (*dimension.primary_key, *dimension.business_key)]
            if labels and fact.column(edge.child_columns[0]).type == dimension.column(key).type:
                groups.append((edge.child_columns[0], dimension.name, key, labels[0], not edge.required))
        groups += [(c.name, "", "", "", False) for c in fact.columns
                   if c.type is ColumnType.TEXT and c.name not in identity
                   and (not c.enum_values or len(c.enum_values) >= 2)
                   and (_words(c.name) & {"category", "type", "status", "group", "segment", "label", "name", "country",
                                         "kind", "role", "title", "headline", "caption", "placement"} or c.enum_values)]
        for group, dimension, dimension_key, label, optional in groups:
            # A link keyed only by parent+group cannot hold unequal group sizes.
            unique_key = fact.primary_key or fact.business_key
            if unique_key and set(unique_key) <= set(rel.child_columns) | {group}:
                continue
            # Without a declared record key, order by the group itself. Equal
            # measure+group ties have identical projected payloads, so the
            # result is still deterministic without inventing a source key.
            row_key = unique_key or (group,)
            others = [r for r in task.relationships if r.parent_table == parent.name
                      and r.parent_columns == rel.parent_columns and r.child_table not in (parent.name, fact.name, dimension)
                      and all(tables[r.child_table].column(c).type == parent.column(p).type
                              for c, p in zip(r.child_columns, r.parent_columns, strict=True))
                      and not any(k and set(k) <= set(r.child_columns)
                                  for k in (tables[r.child_table].primary_key, tables[r.child_table].business_key))]
            other = sorted(others, key=lambda r: (r.child_table, r.child_columns))[0] if others else None
            for measure in measures:
                found.append(SemanticEvidence(parent.name, rel.parent_columns, fact.name, rel.child_columns,
                    row_key, measure.name, group, dimension, dimension_key, label, optional,
                    other.child_table if other else "", other.child_columns if other else ()))
    return tuple(sorted(set(found), key=lambda e: (not bool(e.second_fact), not bool(e.dimension), -len(e.keys), e.parent, e.fact, e.measure, e.group)))


def semantic_plan(task: TaskIR, e: SemanticEvidence, mart: str, variant: str = "") -> MartSpec:
    """One bounded composition; mutants change one named rule, never arbitrary SQL."""
    q = quote
    ops = [MartOp(kind=MartOpKind.SOURCE, tables=(t.name,), description=f"Source {t.name}.") for t in task.tables]
    taken = {t.name for t in task.tables}

    def name(stem):
        value = stem
        while value in taken:
            value += "_"
        taken.add(value)
        return value

    def projection(source, select, stem, kind=MartOpKind.DERIVE, **details):
        from sqlglot import parse_one
        out = name(stem)
        columns = tuple(p.alias_or_name for p in parse_one("SELECT " + select, read="duckdb").expressions
                        if p.alias_or_name and p.alias_or_name != "*")
        ops.append(MartOp(kind=kind, tables=(source,), columns=columns, description=stem.replace("_", " "),
                          details={"name": out, "select": select, **details}))
        return out

    def aggregate(source, keys, measures, stem, kind=MartOpKind.AGGREGATE, predicate=""):
        out = name(stem)
        ops.append(MartOp(kind=kind, tables=(source,), columns=tuple(measures), predicate=predicate,
                          description=stem.replace("_", " "), details={"name": out, "group_by": ", ".join(map(q, keys)), **measures}))
        return out

    def join(left, right, pairs, carried, stem, left_join=True):
        out = name(stem)
        predicate = " AND ".join(f"{q(left)}.{q(a)} = {q(right)}.{q(b)}" for a, b in pairs)
        select = f"{q(left)}.*" + "".join(f", {q(right)}.{q(c)} AS {q(alias)}" for c, alias in carried)
        ops.append(MartOp(kind=MartOpKind.JOIN, tables=(left, right), join_type=JoinType.LEFT if left_join else JoinType.INNER,
                          predicate=predicate, description=stem.replace("_", " "), details={"name": out, "select": select}))
        return out

    def filtered(source, predicate, stem):
        out = name(stem)
        ops.append(MartOp(kind=MartOpKind.FILTER, tables=(source,), predicate=predicate,
                          description=stem.replace("_", " "), details={"name": out}))
        return out

    grain = tuple(f"g_{i}" for i in range(len(e.keys)))
    row_keys = tuple(f"row_{i}" for i in range(len(e.row_key)))
    group_expr = q(e.group)
    source_select = [f"{q(c)} AS {q(k)}" for c, k in zip(e.links, grain, strict=True)]
    source_select += [f"{q(c)} AS {q(k)}" for c, k in zip(e.row_key, row_keys, strict=True)]
    source_select += [f"{q(e.measure)} AS m", f"{group_expr} AS raw_group"]
    facts = projection(e.fact, ", ".join(source_select), "semantic_records")
    if e.dimension:
        facts = join(facts, e.dimension, (("raw_group", e.dimension_key),),
                     ((e.dimension_key, "group_value"), (e.dimension_label, "group_label")), "semantic_dimension",
                     left_join=variant != "inner_dimension")
    else:
        facts = projection(facts, "*, raw_group AS group_value, raw_group AS group_label", "semantic_groups")
    if variant == "join_before_aggregate" and e.second_fact:
        facts = join(facts, e.second_fact, tuple(zip(grain, e.second_links, strict=True)), (), "semantic_cross_product")
    means = aggregate(facts, grain, {"parent_mean": "AVG(m)"}, "semantic_means")
    evaluated = join(facts, means, tuple(zip(grain, grain)), (("parent_mean", "parent_mean"),), "semantic_thresholds", left_join=False)
    condition = "m > parent_mean" if variant == "exclusive_threshold" else "m >= parent_mean"
    if variant == "ignore_filter":
        condition = "m IS NOT NULL"
    summary = aggregate(evaluated, grain, {
        "record_count": "COUNT(*)",
        "group_count": "COUNT(group_value)" if variant == "missing_distinct" else "COUNT(DISTINCT group_value)",
        "measure_total": "SUM(DISTINCT m)" if variant == "sum_distinct" else "SUM(m)",
        "qualifying_count": f"COUNT(CASE WHEN {condition} THEN 1 END)",
        "null_relationship_count": "COUNT(CASE WHEN raw_group IS NULL THEN 1 END)",
        "unmatched_relationship_count": "COUNT(CASE WHEN raw_group IS NOT NULL AND group_value IS NULL THEN 1 END)",
    }, "semantic_summary", MartOpKind.FILTERED_AGGREGATE,
        predicate="qualifying_count: measure at least the parent mean; null_relationship_count and unmatched_relationship_count: key/match tests")
    grouped_input = filtered(facts, "group_value IS NOT NULL", "semantic_known_groups")
    subgroup = aggregate(grouped_input, (*grain, "group_value"), {"group_total": "SUM(m)"}, "semantic_group_totals")
    group_average = aggregate(subgroup, grain, {"mean_group_total": "AVG(group_total)"}, "semantic_group_average")
    if variant == "wrong_aggregation_grain":
        group_average = aggregate(grouped_input, grain, {"mean_group_total": "AVG(m)"}, "semantic_wrong_average")
    key_direction = "DESC" if variant == "wrong_tiebreak" else "ASC"
    order = "m DESC NULLS LAST, " + ", ".join(f"{q(k)} {key_direction} NULLS LAST" for k in row_keys)
    selected_keys = [(k, f"selected_key_{i}") for i, k in enumerate(row_keys)]
    winner = projection(facts, ", ".join([*map(q, grain), *(f"{q(c)} AS {q(a)}" for c, a in selected_keys),
                                          "m AS selected_measure", "group_label AS selected_group"]), "semantic_winner_payload",
                        MartOpKind.EXTREMA, partition_by=", ".join(map(q, grain)), order_by=order, tie_break=row_keys[0])
    if variant == "mixed_selected_row":
        winner = aggregate(facts, grain, {**{a: f"MIN({q(k)})" for k, a in selected_keys},
                          "selected_measure": "MAX(m)", "selected_group": "MIN(group_label)"}, "semantic_mixed_payload")
    current = projection(e.parent, ", ".join(f"{q(c)} AS {q(k)}" for c, k in zip(e.keys, grain, strict=True)), "semantic_parent")
    carried = ("record_count", "group_count", "measure_total", "qualifying_count", "null_relationship_count", "unmatched_relationship_count")
    pairs = tuple(zip(grain, grain))
    if variant.startswith("missing_key_"):
        component = int(variant.rsplit("_", 1)[1])
        pairs = tuple(pair for index, pair in enumerate(pairs) if index != component)
    current = join(current, summary, pairs, tuple((c, c) for c in carried), "semantic_parent_summary", left_join=variant != "inner_parent")
    current = join(current, group_average, tuple(zip(grain, grain)), (("mean_group_total", "mean_group_total"),), "semantic_parent_average")
    current = join(current, winner, tuple(zip(grain, grain)), tuple((a, a) for _, a in selected_keys)
                   + (("selected_measure", "selected_measure"), ("selected_group", "selected_group")), "semantic_parent_winner")
    if e.second_fact:
        other_source = projection(e.second_fact, ", ".join(f"{q(c)} AS {q(k)}" for c, k in zip(e.second_links, grain, strict=True)), "semantic_other_records")
        other = aggregate(other_source, grain, {"other_record_count": "COUNT(*)"}, "semantic_other_summary")
        current = join(current, other, tuple(zip(grain, grain)), (("other_record_count", "other_record_count"),), "semantic_parent_other")
    fact = next(t for t in task.tables if t.name == e.fact)
    parent = next(t for t in task.tables if t.name == e.parent)
    cols = [MartColumn(name=c, type=parent.column(c).type, description=f"{e.parent}.{c} identifying the parent.", kind=MartColumnKind.PASSTHROUGH) for c in e.keys]
    descriptions = {
        "record_count": f"Number of {e.fact} records for this parent, including records with a missing or unmatched {e.group}; 0 when absent.",
        "group_count": f"Number of distinct non-missing {'matched ' + e.dimension + ' entities' if e.dimension else e.fact + '.' + e.group + ' values'} represented by these records; 0 when absent.",
        "measure_total": f"Total {e.fact}.{e.measure} across all records, retaining repeated values; 0 when no values exist. Source units are unchanged.",
        "qualifying_count": f"Number of records with {e.measure} at least the mean non-missing {e.measure} of ALL this parent's {e.fact} records. Equality qualifies; missing measures do not. 0 when absent.",
        "null_relationship_count": f"Number of records with a NULL {e.fact}.{e.group}; 0 when absent.",
        "unmatched_relationship_count": f"Number of records with a non-NULL {e.fact}.{e.group} that matches no {e.dimension} entity; 0 when absent.",
        "mean_group_total": f"Mean of the per-{'matched entity' if e.dimension else e.group} {e.measure} totals. Each group with a non-missing total has equal weight, regardless of its record count. Exclude missing {'or unmatched ' if e.dimension else ''}groups. NULL when no group has a total.",
    }
    visible = ["record_count", "group_count", "measure_total", "qualifying_count"]
    if e.dimension:
        visible += ["null_relationship_count", "unmatched_relationship_count"]
    total_type = fact.column(e.measure).type
    if total_type is ColumnType.INTEGER:
        total_type = ColumnType.BIGINT
    cols += [MartColumn(name=c, type=total_type if c == "measure_total" else ColumnType.BIGINT,
                       description=descriptions[c], kind=MartColumnKind.AGGREGATED) for c in visible]
    cols.append(MartColumn(name="mean_group_total", type=ColumnType.FLOAT, description=descriptions["mean_group_total"], kind=MartColumnKind.AGGREGATED))
    selection_rule = f"Choose the {e.fact} record with greatest {e.measure}, placing NULL measures last; break ties by ascending ({', '.join(e.row_key)}), NULLs last. Remaining ties have identical returned values."
    for key, alias in zip(e.row_key, (a for _, a in selected_keys), strict=True):
        cols.append(MartColumn(name=alias, type=fact.column(key).type, description=f"{key} from the chosen record. {selection_rule} NULL when no record exists.", kind=MartColumnKind.RANKED))
    cols += [MartColumn(name="selected_measure", type=fact.column(e.measure).type, description=f"{e.measure} from that same chosen record; NULL when absent or missing.", kind=MartColumnKind.RANKED),
             MartColumn(name="selected_group", type=ColumnType.TEXT, description=f"{'Matched ' + e.dimension + '.' + e.dimension_label if e.dimension else e.group} from that same chosen record; NULL when absent, missing, or unmatched.", kind=MartColumnKind.RANKED),
             MartColumn(name="qualifying_fraction", type=ColumnType.FLOAT, description="qualifying_count divided by record_count (including records with missing measures), rounded to four decimal places. A fraction, not a percentage; 0 when record_count is zero.", kind=MartColumnKind.DERIVED)]
    if e.second_fact:
        cols.append(MartColumn(name="other_record_count", type=ColumnType.BIGINT, description=f"Number of {e.second_fact} records for this parent; 0 when absent.", kind=MartColumnKind.AGGREGATED))
    selections = [f"{q(k)} AS {q(c)}" for c, k in zip(e.keys, grain, strict=True)]
    selections += [f"COALESCE({q(c)}, 0) AS {q(c)}" for c in visible]
    selections += ["mean_group_total", *(a for _, a in selected_keys), "selected_measure", "selected_group"]
    denominator = "qualifying_count" if variant == "wrong_denominator" else "record_count"
    selections += [f"COALESCE(ROUND(CAST(qualifying_count AS DOUBLE) / NULLIF({denominator}, 0), 4), 0) AS qualifying_fraction"]
    if e.second_fact:
        selections.append("COALESCE(other_record_count, 0) AS other_record_count")
    projection(current, ", ".join(selections), "semantic_output", MartOpKind.RATIO,
               units="fraction", null_result="0", rounding="four decimal places")
    plan = MartPlan(mart=mart, ops=tuple(ops), template_id=TEMPLATE, notes=canonical_json(asdict(e)))
    return MartSpec(name=mart, description=f"Activity, group totals, and the selected {e.fact} record for every {e.parent}.",
                    grain=f"One row per {e.parent} ({', '.join(e.keys)}), including parents without activity.",
                    key_columns=e.keys, columns=tuple(cols), plan=plan)


def variants(e: SemanticEvidence) -> tuple[str, ...]:
    out = ["ignore_filter", "exclusive_threshold", "missing_distinct", "sum_distinct", "wrong_aggregation_grain",
           "wrong_tiebreak", "mixed_selected_row", "wrong_denominator", "inner_parent"]
    if e.second_fact:
        out.append("join_before_aggregate")
    if len(e.keys) > 1:
        out += [f"missing_key_{i}" for i in range(len(e.keys))]
    if e.dimension and e.dimension_optional:
        out.append("inner_dimension")
    return tuple(out)


def mutation_cases(e: SemanticEvidence, mart: str) -> tuple[AttackCase, ...]:
    return _FAMILY.cases(e, mart, TEMPLATE)


def bound_evidence(task: TaskIR, mart: MartSpec) -> SemanticEvidence:
    try:
        raw = json.loads(mart.plan.notes)
        for field in ("keys", "links", "row_key", "second_links"):
            raw[field] = tuple(raw[field])
        e = SemanticEvidence(**raw)
    except (ValueError, TypeError, KeyError) as exc:
        raise SemanticDifficultyError("Invalid semantic plan binding") from exc
    if e not in semantic_candidates(task):
        raise SemanticDifficultyError("Semantic plan bindings no longer match schema evidence")
    if semantic_plan(task, e, mart.name).plan != mart.plan:
        raise SemanticDifficultyError("Semantic plan differs from its bound template")
    return e


def materialize_semantic_mutation(task: TaskIR, payload: dict) -> dict[str, str]:
    return _FAMILY.materialize_mutation(task, payload)


def semantic_witness_rows(task: TaskIR, e: SemanticEvidence) -> dict:
    """Unequal subgroup sizes, mean boundary, ties, empty/partial branches, keys."""
    from elt_taskgen.generation import populations as p
    tables = p._narrow_for_minting(task.tables, task.relationships)
    by_name = {t.name: t for t in tables}
    parent, fact = by_name[e.parent], by_name[e.fact]
    with p._shape_mint_offset(500_000):
        parents = [p._mint_row(parent, i) for i in range(8 + (len(e.keys) if len(e.keys) > 1 else 0))]
        for component in range(len(e.keys) if len(e.keys) > 1 else 0):
            changed = parents[8 + component][e.keys[component]]
            parents[8 + component].update({key: parents[0][key] for key in e.keys})
            parents[8 + component][e.keys[component]] = changed
        rows = {e.parent: parents, e.fact: []}
        pins = {id(row): frozenset((*parent.primary_key, *parent.business_key, *e.keys)) for row in parents}
        protected = set(pins)
        dimension_edge = None
        if e.dimension:
            dimension = by_name[e.dimension]
            dims = [p._mint_row(dimension, 300 + i) for i in range(2)]
            rows[e.dimension] = dims
            groups = [row[e.dimension_key] for row in dims]
            for row in dims:
                pins[id(row)] = frozenset((*dimension.primary_key, *dimension.business_key, e.dimension_key))
                protected.add(id(row))
            dimension_edge = (e.fact, (e.group,), e.dimension, (e.dimension_key,))
            orphan = p._mint(dimension.column(e.dimension_key), e.dimension, 777)
        else:
            domain = fact.column(e.group).enum_values
            groups = list(domain[:2]) if domain else ["semantic_group_a", "semantic_group_b"]
            orphan = None
        if groups[0] == groups[1]:
            raise SemanticDifficultyError("Grouping domain cannot provide two distinct groups")
        placements = {0: [(2, groups[0]), (5, groups[0]), (5, groups[1]), (5, groups[1]), (8, groups[1])],
                      2: [(9, groups[0]), (9, groups[1]), (1, groups[1])],
                      3: [(4, groups[0]), (4, groups[1])],
                      5: [(None if fact.column(e.measure).nullable else 0, groups[0])],
                      **{i: [(11 + i, groups[0])] for i in range(8, len(parents))}}
        if fact.column(e.group).nullable:
            placements[6] = [(6, None)]
        if e.dimension and e.dimension_optional:
            if orphan in groups:
                raise SemanticDifficultyError("Dimension key domain cannot provide an unmatched value")
            placements[7] = [(7, orphan)]
        for parent_index, values in placements.items():
            for measure, group in values:
                row = p._mint_row(fact, 100 + len(rows[e.fact]))
                row.update({c: parents[parent_index][k] for c, k in zip(e.links, e.keys, strict=True)})
                row[e.measure] = p._coerce(fact.column(e.measure), measure)
                row[e.group] = group
                rows[e.fact].append(row)
                pins[id(row)] = frozenset((*fact.primary_key, *fact.business_key, *e.links, e.measure, e.group))
                protected.add(id(row))
        if e.second_fact:
            other = by_name[e.second_fact]
            rows[e.second_fact] = []
            for parent_index, count in [(0, 3), (4, 2), *[(i, 1) for i in range(8, len(parents))]]:
                for _ in range(count):
                    row = p._mint_row(other, 200 + len(rows[e.second_fact]))
                    row.update({c: parents[parent_index][k] for c, k in zip(e.second_links, e.keys, strict=True)})
                    rows[e.second_fact].append(row)
                    pins[id(row)] = frozenset((*other.primary_key, *other.business_key, *e.second_links))
                    protected.add(id(row))
        for table in tables:
            rows.setdefault(table.name, [p._mint_row(table, 0)])
        owned = {(e.fact, e.links, e.parent, e.keys), (e.second_fact, e.second_links, e.parent, e.keys), dimension_edge}
        p._close_foreign_keys(by_name, task.relationships, rows,
            skip=lambda r: (r.child_table, r.child_columns, r.parent_table, r.parent_columns) in owned,
            preserve=frozenset(protected), pinned_columns=pins)
        p._enforce_primary_keys(by_name, rows)
    result = {name: tuple(payload) for name, payload in rows.items()}
    p.assert_literal_rows_typed(task.tables, result)
    return result




def certify_semantics(task: TaskIR, mart: MartSpec, e: SemanticEvidence, rows: dict) -> tuple[str, ...]:
    return _FAMILY.certify(task, mart, e, rows)


def apply_semantic_difficulty(task: TaskIR) -> TaskIR:
    """Add a witnessed composed mart through the shared admission contract."""
    return _FAMILY.apply(task, (TEMPLATE,))


def _candidates(task, recipe):
    candidates = semantic_candidates(task)
    if not candidates:
        raise SemanticDifficultyError("No semantic candidate: need a relationship to a declared unique parent key, a meaningful numeric measure, and a repeatable group or dimension")
    return candidates


_FAMILY = RecipeFamily(
    name="semantic", recipes=(TEMPLATE,), candidates=_candidates,
    plan=lambda task, evidence, mart, recipe, variant="": semantic_plan(task, evidence, mart, variant),
    witness_rows=lambda task, evidence, recipe: semantic_witness_rows(task, evidence),
    variants=lambda evidence, recipe: variants(evidence), bind=bound_evidence,
    mart_name=lambda evidence, recipe: "semantic_" + re.sub(r"[^a-z0-9_]", "_", evidence.parent.casefold())[:35] + "_activity",
    population_condition=lambda evidence, mart, recipe: f"{recipe}: isolated witnesses for {mart}; " + ", ".join(variants(evidence)),
    attack_description="Violate the {variant} rule in {mart}; executable witness required.",
    retryable_errors=(SemanticDifficultyError, ValueError),
    max_result_rows=100_000, max_result_bytes=16 * 1024 * 1024,
)

"""Versioned compositions over declared schema evidence and executable witnesses.

The v1 semantic rollup supplies shared relational construction. These recipes
change the scope of its baseline or add an independently ordered selection.
The existing branched-fact recipe remains available through adapter evidence.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
import json

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from elt_taskgen.generation.semantic_difficulty import (
    SemanticDifficultyError, SemanticEvidence, _merged_counterfactual,
    semantic_candidates, semantic_plan, semantic_witness_rows, variants,
)
from elt_taskgen.models import (
    AttackCase, AttackKind, JoinType, MartColumnKind, MartOp, MartOpKind,
    MartPlan, MartSpec, PopulationName, SemanticPattern, TaskIR, canonical_json, sha256_hex,
)

GLOBAL_BASELINE = "global_baseline_v1"
INDEPENDENT_WINNERS = "independent_winners_v1"
COMPOSITION_TEMPLATES = (GLOBAL_BASELINE, INDEPENDENT_WINNERS)


def composition_variants(e: SemanticEvidence, template: str) -> tuple[str, ...]:
    if template not in COMPOSITION_TEMPLATES:
        raise ValueError(f"Unknown composition {template!r}")
    extra = ("local_baseline",) if template == GLOBAL_BASELINE else (
        "reuse_highest_for_lowest", "wrong_lowest_tiebreak",
    )
    return (*variants(e), *extra)


def composition_plan(task: TaskIR, e: SemanticEvidence, mart: str,
                     template: str, variant: str = "") -> MartSpec:
    """Build through ordinary MartOps; the compiler owns all SQL emission."""
    if variant and variant not in composition_variants(e, template):
        raise ValueError(f"Unknown mutation {variant!r} for {template}")
    if template not in COMPOSITION_TEMPLATES:
        raise ValueError(f"Unknown composition {template!r}")
    base = semantic_plan(task, e, mart, variant if variant in variants(e) else "")
    ops = list(base.plan.ops)
    columns = list(base.columns)
    if template == GLOBAL_BASELINE:
        mean_index = next(i for i, op in enumerate(ops) if "parent_mean" in op.details)
        means = ops[mean_index]
        facts = means.tables[0]
        taken = {t.name for t in task.tables} | {o.details.get("name", "") for o in ops}
        scope_name = "global_baseline_input"
        while scope_name in taken:
            scope_name += "_"
        ops.insert(mean_index, MartOp(kind=MartOpKind.DERIVE, tables=(facts,),
            columns=("baseline_scope",), description="Use all fact records to calculate the shared baseline.",
            details={"name": scope_name, "select": "*, 1 AS baseline_scope"}))
        if variant != "local_baseline":
            ops[mean_index + 1] = means.model_copy(update={"tables": (scope_name,),
                "details": {**means.details, "group_by": "baseline_scope"},
                "description": f"Mean of all non-missing {e.fact}.{e.measure} values across every parent."})
            join_index = mean_index + 2
            ops[join_index] = ops[join_index].model_copy(update={
                "join_type": JoinType.CROSS, "predicate": "",
                "description": "Attach the same whole-dataset mean to each fact record."})
        columns = [c.model_copy(update={"description":
            f"Number of this parent's records whose {e.measure} is at least the mean non-missing "
            f"{e.measure} across ALL {e.fact} records, including records belonging to other parents "
            "or having no matching parent. Equality qualifies; missing measures do not. 0 when absent."
        }) if c.name == "qualifying_count" else c for c in columns]
        patterns = (SemanticPattern.GROUP_GLOBAL_BASELINE,)
        description = f"Activity and selected {e.fact} record per {e.parent}, compared with a whole-dataset baseline."
    else:
        winner = next((op for op in ops if op.kind is MartOpKind.EXTREMA), None)
        if winner is None:
            winner = next(op for op in semantic_plan(task, e, mart).plan.ops
                          if op.kind is MartOpKind.EXTREMA)
        taken = {t.name for t in task.tables} | {o.details.get("name", "") for o in ops}
        low_name, joined = "independent_lowest", "independent_winners"
        while low_name in taken:
            low_name += "_"
        while joined in taken or joined == low_name:
            joined += "_"
        direction = "ASC" if variant == "wrong_lowest_tiebreak" else "DESC"
        order = "m ASC NULLS LAST, " + ", ".join(f'"row_{i}" {direction} NULLS LAST' for i in range(len(e.row_key)))
        if variant == "reuse_highest_for_lowest":
            order = winner.details["order_by"]
        payload = parse_one("SELECT " + winner.details["select"], read="duckdb")
        aliases = {}
        for expression in payload.expressions:
            if expression.alias.startswith("selected_"):
                aliases[expression.alias] = "lowest_" + expression.alias
                expression.set("alias", exp.to_identifier(aliases[expression.alias]))
        low = winner.model_copy(update={
            "columns": tuple(aliases.get(c, c) for c in winner.columns),
            "description": "Independently choose the record with the smallest measure; ties use descending record keys.",
            "details": {**winner.details, "name": low_name,
                "select": ", ".join(x.sql(dialect="duckdb") for x in payload.expressions), "order_by": order},
        })
        last = ops[-1]
        current = last.tables[0]
        grain = tuple(f"g_{i}" for i in range(len(e.keys)))
        predicate = " AND ".join(f'"{current}"."{g}" = "{low_name}"."{g}"' for g in grain)
        select = f'"{current}".*' + ''.join(f', "{low_name}"."{a}" AS "{a}"' for a in aliases.values())
        join = MartOp(kind=MartOpKind.JOIN, tables=(current, low_name), join_type=JoinType.LEFT,
            predicate=predicate, description="Attach the independently selected lowest record at the complete parent key.",
            details={"name": joined, "select": select})
        last = last.model_copy(update={"tables": (joined,), "details": {**last.details,
            "select": last.details["select"] + ''.join(f', "{a}"' for a in aliases.values())}})
        ops[-1:] = [low, join, last]
        for column in base.columns:
            if column.name in aliases:
                columns.append(column.model_copy(update={"name": aliases[column.name],
                    "description": f"{column.name.removeprefix('selected_')} from the independently chosen lowest-measure "
                    f"{e.fact} record. Order {e.measure} ascending, NULLs last; break ties by "
                    f"({', '.join(e.row_key)}) descending, NULLs last. All returned fields belong to this same "
                    "record. NULL when no record exists.", "kind": MartColumnKind.RANKED}))
        patterns = (SemanticPattern.MULTIPLE_EXTREMA,)
        description = f"Activity plus independently selected highest and lowest {e.fact} records for each {e.parent}."
    plan = base.plan.model_copy(update={"ops": tuple(ops), "template_id": template,
                                        "semantic_patterns": patterns})
    return base.model_copy(update={"columns": tuple(columns), "plan": plan, "description": description})


def _reachable(plan: MartPlan) -> tuple[set[str], str]:
    body = [o for o in plan.ops if o.kind is not MartOpKind.TIE_BREAK]
    if not body:
        return set(), ""
    bindings = {o.details.get("name"): o for o in body if o.details.get("name")}
    pending, found = list(body[-1].tables), set()
    while pending:
        name = pending.pop()
        if name not in found:
            found.add(name)
            if name in bindings:
                pending.extend(bindings[name].tables)
    return found, body[-1].details.get("select", "")


def certifies_global_baseline(plan: MartPlan) -> bool:
    """Require constant-grain AVG feeding a cross join and downstream condition."""
    reachable, _ = _reachable(plan)
    bindings = {o.details.get("name"): o for o in plan.ops if o.details.get("name")}
    try:
        for op in plan.ops:
            if op.kind is not MartOpKind.AGGREGATE or op.details.get("name") not in reachable:
                continue
            groups = parse_one("SELECT " + op.details.get("group_by", ""), read="duckdb").expressions
            if len(groups) != 1:
                continue
            group = groups[0]
            if not isinstance(group, exp.Column) or len(op.tables) != 1:
                continue
            source = bindings.get(op.tables[0])
            if source is None or source.kind is not MartOpKind.DERIVE:
                continue
            projections = parse_one("SELECT " + source.details.get("select", ""), read="duckdb").expressions
            if not any(p.alias == group.name and isinstance(p.this, exp.Literal) for p in projections):
                continue
            means = {k for k, v in op.details.items() if k not in {"name", "group_by"}
                     and isinstance(parse_one(v, read="duckdb"), exp.Avg)}
            if not means:
                continue
            joins = [j for j in plan.ops if j.kind is MartOpKind.JOIN and j.join_type is JoinType.CROSS
                     and op.details["name"] in j.tables and j.details.get("name") in reachable]
            for later in plan.ops:
                if later.kind is not MartOpKind.FILTERED_AGGREGATE or later.details.get("name") not in reachable:
                    continue
                ancestors, _ = _reachable(plan.model_copy(update={"ops": tuple(
                    o for o in plan.ops[:plan.ops.index(later) + 1])}))
                if not any(j.details.get("name") in ancestors for j in joins):
                    continue
                for alias, expression in later.details.items():
                    if alias in {"name", "group_by"}:
                        continue
                    parsed = parse_one(expression, read="duckdb")
                    if joins and parsed is not None and parsed.find(exp.Case) and means & {c.name for c in parsed.find_all(exp.Column)}:
                        return True
    except (ParseError, ValueError, KeyError):
        return False
    return False


def certifies_multiple_extrema(plan: MartPlan) -> bool:
    reachable, final_select = _reachable(plan)
    try:
        final_names = {c.name for c in parse_one("SELECT " + final_select, read="duckdb").find_all(exp.Column)}
        winners = []
        for op in plan.ops:
            if op.kind is not MartOpKind.EXTREMA or op.details.get("name") not in reachable:
                continue
            projections = parse_one("SELECT " + op.details["select"], read="duckdb").expressions
            partition = {c.name for c in parse_one("SELECT " + op.details["partition_by"], read="duckdb").find_all(exp.Column)}
            payload = {p.alias for p in projections if isinstance(p, exp.Alias)} - partition
            if payload & final_names:
                order = parse_one("SELECT * FROM t ORDER BY " + op.details["order_by"], read="duckdb").args["order"]
                winners.append((op, payload & final_names, order))
        return any(a.tables == b.tables and a.details.get("partition_by") == b.details.get("partition_by")
                   and order_a != order_b and payload_a.isdisjoint(payload_b)
                   for i, (a, payload_a, order_a) in enumerate(winners)
                   for b, payload_b, order_b in winners[i + 1:])
    except (ParseError, KeyError, ValueError):
        return False


def composition_cases(e: SemanticEvidence, mart: str, template: str) -> tuple[AttackCase, ...]:
    return tuple(AttackCase(name=f"{mart}__{v}", kind=AttackKind.CUSTOM,
        description=f"Violate {v.replace('_', ' ')} in {mart}.",
        mutation="directive:structured:" + canonical_json({"operation": template, "mart": mart, "variant": v}),
        expected_pass={PopulationName.COUNTERFACTUAL: False}) for v in composition_variants(e, template))


def bound_composition(task: TaskIR, mart: MartSpec) -> SemanticEvidence:
    raw = json.loads(mart.plan.notes)
    for field in ("keys", "links", "row_key", "second_links"):
        raw[field] = tuple(raw[field])
    evidence = SemanticEvidence(**raw)
    if evidence not in semantic_candidates(task) or composition_plan(task, evidence, mart.name, mart.plan.template_id).plan != mart.plan:
        raise SemanticDifficultyError("Composition no longer matches its schema-bound recipe")
    return evidence


def materialize_composition_mutation(task: TaskIR, payload: dict) -> dict[str, str]:
    from elt_taskgen.reference.solution import build_reference, compile_plan_sql
    if set(payload) != {"operation", "mart", "variant"}:
        raise ValueError("Invalid composition mutation")
    mart = task.mart(payload["mart"])
    if payload["operation"] != mart.plan.template_id:
        raise ValueError("Composition mutation template mismatch")
    e = bound_composition(task, mart)
    result = dict(build_reference(task).sql_by_mart)
    result[mart.name] = compile_plan_sql(task, composition_plan(task, e, mart.name, mart.plan.template_id, payload["variant"]))
    return result


def certify_composition(task: TaskIR, mart: MartSpec, e: SemanticEvidence, rows: dict) -> tuple[str, ...]:
    from elt_taskgen.generation.mart_plan import validate_plan
    from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
    from elt_taskgen.reference.solution import create_table, _insert_rows, compile_plan_sql, execute_mart
    from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv
    problems = validate_plan(task, mart.plan)
    if problems:
        raise SemanticDifficultyError("; ".join(problems))
    with sandboxed_memory_connection(memory_limit_mb=512, threads=1, disable_temp_spill=True) as con:
        for table in task.tables:
            create_table(con, table)
            _insert_rows(con, table, rows.get(table.name, ()))
        correct = execute_mart(con, mart, compile_plan_sql(task, mart))
        gold = rows_to_canonical_csv(correct, tuple(c.name for c in mart.columns))
        for variant in composition_variants(e, mart.plan.template_id):
            mutant = composition_plan(task, e, mart.name, mart.plan.template_id, variant)
            wrong = execute_mart(con, mutant, compile_plan_sql(task, mutant))
            if compare_mart(gold, wrong, mart):
                raise SemanticDifficultyError(f"No executable witness for {variant}")
    return composition_variants(e, mart.plan.template_id)


def apply_composition_difficulty(task: TaskIR, *, template: str | None = None) -> TaskIR:
    """Add one supported recipe, choosing reproducibly across task identities."""
    from elt_taskgen.generation.populations import _coerce, _literal_row_problems
    from elt_taskgen.reference.solution import attach_reference
    existing = [m for m in task.marts if m.plan.template_id in COMPOSITION_TEMPLATES]
    if existing:
        for mart in existing:
            if template and mart.plan.template_id != template:
                raise SemanticDifficultyError("Task already contains a different composition")
            e = bound_composition(task, mart)
            if any(case not in task.attack_cases for case in composition_cases(e, mart.name, mart.plan.template_id)):
                raise SemanticDifficultyError("Composition is missing mandatory attacks")
            rows = next(p.literal_rows for p in task.populations if p.name is PopulationName.COUNTERFACTUAL)
            certify_composition(task, mart, e, rows)
        return task
    if template is None:
        template = COMPOSITION_TEMPLATES[int(sha256_hex(task.family_id)[:8], 16) % len(COMPOSITION_TEMPLATES)]
    if template not in COMPOSITION_TEMPLATES:
        raise ValueError(f"Unknown composition {template!r}")
    refusals = []
    for e in semantic_candidates(task)[:32]:
        try:
            new_rows = semantic_witness_rows(task, e)
            if template == GLOBAL_BASELINE:
                # Pin an actual equality at the GLOBAL mean after merging all
                # existing rows. Change only two newly minted witness records.
                old = next(p.literal_rows for p in task.populations if p.name is PopulationName.COUNTERFACTUAL)
                target_rows = list(new_rows[e.fact])
                remaining = [Decimal(str(r[e.measure])) for r in (*old.get(e.fact, ()), *target_rows[2:]) if r[e.measure] is not None]
                total = sum(remaining, Decimal(0))
                threshold = (total / max(1, len(remaining))).to_integral_value(rounding=ROUND_HALF_EVEN)
                balancing = threshold * (len(remaining) + 1) - total
                col = task.table(e.fact).column(e.measure)
                # Put the smaller value on the earlier record so independent
                # MIN(key)/MAX(measure) still constructs an impossible payload.
                target_rows[0][e.measure] = _coerce(col, min(threshold, balancing))
                target_rows[1][e.measure] = _coerce(col, max(threshold, balancing))
                # Keep a separate mixed-payload witness whose returned label
                # differs, even when numeric key differences fall within the
                # evaluator's relative tolerance. This preserves the mean.
                parent = new_rows[e.parent][2]
                tied = [r for r in target_rows if all(r[c] == parent[k]
                        for c, k in zip(e.links, e.keys, strict=True))]
                tied[0][e.measure], tied[-1][e.measure] = tied[-1][e.measure], tied[0][e.measure]
            rows = _merged_counterfactual(task, new_rows)
            name = template.removesuffix("_v1") + "_activity"
            occupied = {m.name for m in task.marts} | {t.name for t in task.tables}
            while name in occupied:
                name += "_"
            mart = composition_plan(task, e, name, template)
            populations = tuple(p.model_copy(update={"literal_rows": rows,
                "scale": {n: max(1, len(v)) for n, v in rows.items()},
                "conditions": (*p.conditions, f"{template}: executable witnesses for {name}")})
                if p.name is PopulationName.COUNTERFACTUAL else p for p in task.populations)
            changed = task.model_copy(update={"marts": (*task.marts, mart), "populations": populations,
                "attack_cases": (*task.attack_cases, *composition_cases(e, name, template)), "reference": None})
            changed = TaskIR.model_validate_json(changed.model_dump_json())
            problems = _literal_row_problems(changed, next(p for p in populations if p.name is PopulationName.COUNTERFACTUAL))
            if problems:
                raise SemanticDifficultyError("; ".join(problems))
            certify_composition(changed, mart, e, rows)
        except SemanticDifficultyError as exc:
            refusals.append(str(exc))
            continue
        return attach_reference(changed)
    detail = " | ".join(refusals[:4]) if refusals else (
        "schema needs a declared parent key, a many-to-one fact relationship, a numeric measure, and a usable group")
    raise SemanticDifficultyError(f"No witnessed {template} candidate: {detail}")

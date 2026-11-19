"""Private relationship coverage from current, executed mutation evidence.

Counts describe tested failure modes, not calibrated solver difficulty. Multiple
branches/components in one mutant family do not create extra families.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from elt_taskgen.models import AttackCase, PopulationName, TaskIR, canonical_json, sha256_hex

REPORT_REL = Path("reports") / "relationship_coverage.json"


def _read_record(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("unsafe or oversized mutation record")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("mutation record must be an object")
    return record


def _case_result(workspace: Path, task: TaskIR, case: AttackCase, directive: dict) -> dict:
    from elt_taskgen.verification.attacks import MUTATION_FIDELITY_FILENAME
    from elt_taskgen.verification.branched import materialize_branched_mutation

    result = {"case": case.name, "family": directive["variant"], "status": "not_run"}
    directory = workspace / "tasks" / task.task_id / "attacks" / case.name
    if case not in task.attack_cases:
        return {**result, "status": "not_declared"}
    try:
        record = _read_record(directory / "rewards.json")
        fidelity = _read_record(directory / MUTATION_FIDELITY_FILENAME)
        result["evidence"] = {"rewards": record, "fidelity": fidelity}
        if any(row.get("task_content_hash") != task.content_hash() for row in (record, fidelity)):
            return {**result, "status": "stale"}
        if (record.get("case") != case.name or record.get("kind") != case.kind.value
                or fidelity.get("case") != case.name or fidelity.get("requested") != directive
                or fidelity.get("realized") != directive or fidelity.get("passed") is not True):
            return {**result, "status": "invalid"}
        if record.get("errors") != {} or fidelity.get("errors") != []:
            return {**result, "status": "execution_error"}
        from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY
        sql_by_mart = (RECIPE_REGISTRY[directive["operation"]].materialize_mutation(task, directive)
                      if directive["operation"] in RECIPE_REGISTRY else materialize_branched_mutation(task, directive))
        for mart, expected in sql_by_mart.items():
            sql_path = directory / f"mutation_{mart}.sql"
            if sql_path.is_symlink() or sql_path.read_text(encoding="utf-8") != expected:
                return {**result, "status": "invalid"}
        result["evidence"]["sql_sha256"] = {mart: sha256_hex(sql) for mart, sql in sql_by_mart.items()}
        measured = record.get("rewards_by_variant", {}).get("transform", {})
        required = {spec.name.value for spec in task.populations if spec.name is not PopulationName.DEVELOPMENT}
        if not required <= set(measured) or any(type(measured[p]) not in (int, float) or not 0 <= measured[p] <= 1 for p in required):
            return {**result, "status": "incomplete"}
        # Task reward averages correctness across marts. A faithful mutation of
        # one mart in a multi-mart task loses part of the reward, not all of it.
        return {**result, "status": "killed" if measured.get("counterfactual", 1) < 1 else "survived"}
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return {**result, "status": "invalid", "detail": str(exc)}


def relationship_coverage(task: TaskIR, workspace: Path) -> dict | None:
    """Recompute the report; never trust a previously saved summary."""
    from elt_taskgen.generation.shapes.branched_fact_rollup import branched_fact_rollup
    from elt_taskgen.verification.attacks import STRUCTURED_DIRECTIVE_PREFIX
    from elt_taskgen.verification.branched import bound_branched_candidate, branched_attack_cases
    from elt_taskgen.generation.composite_evidence import COMPOSITE_RECIPES
    from elt_taskgen.generation.composite_recipes import bound_composite, composite_cases
    from elt_taskgen.generation.graph_evidence import GRAPH_RECIPES
    from elt_taskgen.generation.graph_recipes import bound_graph, graph_cases
    from elt_taskgen.generation.advanced_evidence import ADVANCED_RECIPES
    from elt_taskgen.generation.advanced_recipes import bound_advanced, advanced_cases
    from elt_taskgen.generation.scenario_evidence import SCENARIO_RECIPES
    from elt_taskgen.generation.scenario_recipes import bound_scenario, scenario_cases
    from elt_taskgen.generation.relationship_graph import relationship_graph

    marts = []
    for mart in task.marts:
        recipe = mart.plan.template_id
        handler = ((bound_composite,composite_cases) if recipe in COMPOSITE_RECIPES else
                   (bound_graph,graph_cases) if recipe in GRAPH_RECIPES else
                   (bound_advanced,advanced_cases) if recipe in ADVANCED_RECIPES else
                   (bound_scenario,scenario_cases) if recipe in SCENARIO_RECIPES else None)
        if handler:
            bind, required_cases = handler
            e = bind(task,mart)
            cases = []
            for case in required_cases(e,mart.name,recipe):
                directive = json.loads(case.mutation.removeprefix(STRUCTURED_DIRECTIVE_PREFIX))
                result = _case_result(Path(workspace),task,case,directive)
                if result["family"].startswith(("missing_component_","missing_key_")):
                    result["family"] = "missing_composite_component"
                cases.append(result)
            grain_table = e.grain_table if recipe in GRAPH_RECIPES else e.fact if recipe == "historical_asof_v1" else e.parent
            marts.append({"mart":mart.name,"recipe":recipe,"grain_table":grain_table,
                "grain_key":list(mart.key_columns),"composite_key_arity":len(mart.key_columns),"mutants":cases,
                "graph":asdict(relationship_graph(task,grain_table=grain_table,
                    rows=task.population(PopulationName.COUNTERFACTUAL).literal_rows,population="counterfactual"))})
            continue
        if mart.plan.template_id != "branched_fact_rollup":
            continue
        candidate = bound_branched_candidate(task, mart)
        shape = branched_fact_rollup(candidate, mart=mart.name).shape
        cases = []
        for case in branched_attack_cases(shape):
            directive = json.loads(case.mutation.removeprefix(STRUCTURED_DIRECTIVE_PREFIX))
            cases.append(_case_result(Path(workspace), task, case, directive))
        marts.append({
            "mart": mart.name, "grain_table": candidate.grain_table,
            "grain_key": list(candidate.grain_key),
            "fact_branch_count": len(candidate.branches),
            "composite_key_arity": len(candidate.grain_key),
            "mutants": cases,
        })
    if not marts:
        return None
    cases = [case for mart in marts for case in mart["mutants"]]
    killed = [case for case in cases if case["status"] == "killed"]
    assignments = {item.table: item.backend.value for item in task.backends}
    return {
        "schema_version": "relationship-coverage-v1",
        "task_id": task.task_id, "task_content_hash": task.content_hash(),
        "interpretation": "Executed counterfactual mutation coverage; no calibrated difficulty score.",
        "relationship_reasoning": {
            "marts": marts, "expected_mutant_cases": len(cases),
            "killed_mutant_cases": len(killed),
            "killed_mutant_families": sorted({case["family"] for case in killed}),
            "complete": len(killed) == len(cases),
        },
        "relationship_reasoning_difficulty": {
            "calibrated_score": None,
            "witness_backed_mutant_family_count": len({case["family"] for case in killed}),
            "measurement": "Distinct executed failure families; not an estimate of solver success or statistical independence.",
        },
        "integration_backend_difficulty": {
            "calibrated_score": None,
            "backend_count": len(set(assignments.values())),
            "cross_backend_edge_count": sum(assignments[r.child_table] != assignments[r.parent_table] for r in task.relationships),
            "applies_to": "Extraction/loading; backend diversity does not add SQL difficulty once all raw tables share one warehouse.",
        },
        "integration_backends": {
            "scope": "all task tables and declared relationships",
            "backend_count": len(set(assignments.values())),
            "cross_backend_edge_count": sum(
                assignments.get(rel.child_table) != assignments.get(rel.parent_table)
                for rel in task.relationships
                if rel.child_table in assignments and rel.parent_table in assignments
            ),
        },
    }


def write_relationship_report(task: TaskIR, workspace: Path, *, destination: Path | None = None) -> dict | None:
    """Write deterministic task evidence, or stage it inside a private release."""
    report = relationship_coverage(task, workspace)
    if report is None:
        return None
    target = destination if destination is not None else Path(workspace) / "tasks" / task.task_id / REPORT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".relationship-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(report) + "\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return report

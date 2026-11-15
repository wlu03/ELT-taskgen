"""Closed graph recipes: full-key relationships, explicit membership semantics."""
from dataclasses import asdict, fields
import json
from types import SimpleNamespace

from elt_taskgen.generation.graph_evidence import GraphBinding, GRAPH_RECIPES, ROLES, PATHS, BRIDGES, OPTIONAL, NORMALIZED, COMPOSITE
from elt_taskgen.generation.relationship_graph import RelationshipEdgeEvidence
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.generation.mart_plan import quote as q, column_kinds_from_plan, detect_semantic_patterns
from elt_taskgen.models import ColumnType as T, JoinType, MartColumn, MartColumnKind, MartOpKind, MartPlan, MartSpec, SemanticPattern, canonical_json


class GraphPlanBuilder(PlanBuilder):
    def aggregate(self, source, grain, measures, stem):
        from elt_taskgen.generation.mart_plan import is_filtered_aggregate_expr
        result = super().aggregate(source, grain, measures, stem)
        if self.ops[-1].kind is MartOpKind.FILTERED_AGGREGATE:
            predicate = "; ".join(f"{name} counts only rows specified by {expr}" for name,expr in measures.items()
                                  if is_filtered_aggregate_expr(expr))
            self.ops[-1] = self.ops[-1].model_copy(update={"predicate":predicate})
        return result

PATTERNS = {ROLES: SemanticPattern.ROLE_PLAYING_DIMENSION, PATHS: SemanticPattern.AMBIGUOUS_PATH,
    BRIDGES: SemanticPattern.MULTI_BRIDGE_DEDUP, OPTIONAL: SemanticPattern.OPTIONAL_CHAIN,
    NORMALIZED: SemanticPattern.HETEROGENEOUS_KEY_JOIN, COMPOSITE: SemanticPattern.COMPOSITE_KEY_BRIDGE}


def graph_variants(e, recipe):
    variants = {
        ROLES: ("single_alias_for_multiple_roles",),
        PATHS: ("wrong_relationship_path", "count_instead_of_count_distinct", "inner_instead_of_left"),
        BRIDGES: ("missing_bridge_deduplication", "count_instead_of_count_distinct", "inner_instead_of_left"),
        OPTIONAL: (),
        NORMALIZED: ("no_normalization", "unsafe_numeric_conversion", "inner_instead_of_left",
                     *(("null_equals_unmatched",) if e.edges[0].child_nullable else ())),
        COMPOSITE: ("count_instead_of_count_distinct", "inner_instead_of_left"),
    }[recipe]
    if recipe == OPTIONAL:
        variants = ("inner_instead_of_left", *(("left_instead_of_inner",) if not e.edges[1].required else ()),
                   *(("null_equals_unmatched",) if e.edges[2].child_nullable else ()))
    if recipe == ROLES:
        if any(not edge.required for edge in e.edges):
            variants += ("inner_instead_of_left",)
        if any(not edge.required and edge.child_nullable for edge in e.edges):
            variants += ("null_equals_unmatched",)
    # Each composite component gets its own collision and mutant. Role lookups
    # have different FK tuples, so exercise each role separately.
    indexes = range(len(e.edges)) if recipe == ROLES else (0,)
    return (*variants, *(f"missing_component_{i}_{j}" for i in indexes
                          for j in range(len(e.edges[i].parent_columns)) if len(e.edges[i].parent_columns)>1))


def _names(prefix, columns):
    return tuple(f"{prefix}{i}" for i in range(len(columns)))


def _project(columns, aliases):
    return ", ".join(f"{q(c)} AS {q(a)}" for c,a in zip(columns,aliases,strict=True))


def _null(columns):
    return "(" + " OR ".join(f"{q(c)} IS NULL" for c in columns) + ")"


def _join(b, left, right, pairs, carried, name, *, kind=JoinType.LEFT, variant="", edge=0):
    predicate = " AND ".join(f"{q(left)}.{q(a)} = {q(right)}.{q(c)}" for i,(a,c) in enumerate(pairs)
                             if variant != f"missing_component_{edge}_{i}")
    return b.add(MartOpKind.JOIN,(left,right),name,join_type=kind,predicate=predicate,
        select=f"{q(left)}.*" + "".join(f", {q(right)}.{q(c)} AS {q(a)}" for c,a in carried))


def build_graph_ops(task, e, recipe, variant=""):
    b = GraphPlanBuilder(task)
    g = _names("g",e.grain_key)
    owner = e.edges[0]
    metric_names = []
    if recipe in (ROLES,NORMALIZED):
        select = [_project(e.grain_key,g)]
        for i,edge in enumerate(e.edges):
            select.append(_project(edge.child_columns,_names(f"raw{i}_",edge.child_columns)))
        current = b.project(e.grain_table, ", ".join(select), "relationship_records")
        for i,edge in enumerate(e.edges):
            raw = _names(f"raw{i}_",edge.child_columns)
            keys = _names(f"lookup{i}_",edge.parent_columns)
            if recipe == NORMALIZED:
                exprs = []
                for c,k,rule in zip(raw,keys,edge.normalization,strict=True):
                    value = q(c)
                    if rule == "decimal_integer":
                        cast = f"TRY_CAST(TRIM({value}, ' ') AS BIGINT)"
                        if variant == "no_normalization":
                            value = value
                        elif variant == "unsafe_numeric_conversion":
                            value = cast
                        else:
                            value = f"CASE WHEN REGEXP_FULL_MATCH(TRIM({value}, ' '), '[+-]?[0-9]+') THEN {cast} END"
                    exprs.append(f"{value} AS {q(k)}")
                current = b.project(current, "*, " + ", ".join(exprs), "normalized_relationship_keys")
                join_left = keys
            else:
                join_left = _names("raw0_",edge.child_columns) if variant == "single_alias_for_multiple_roles" else raw
            marker = f"present_{i}"
            right_keys = _names(f"target{i}_",edge.parent_columns)
            projections = []
            for c,k,rule in zip(edge.parent_columns,right_keys,edge.normalization,strict=True):
                value = f"CAST({q(c)} AS VARCHAR)" if recipe == NORMALIZED and variant == "no_normalization" and rule == "decimal_integer" else q(c)
                projections.append(f"{value} AS {q(k)}")
            projections.append(f"1 AS {marker}")
            label = f"role_{i}_label"
            if recipe == ROLES:
                projections.append(f"{q(e.label)} AS {label}")
            target = b.project(edge.parent_table, ", ".join(projections), f"role_{i}_entities")
            carried = [(marker,marker)] + ([(label,label)] if recipe == ROLES else [(k,f"resolved_key_{j}") for j,k in enumerate(right_keys)])
            current = _join(b,current,target,tuple(zip(join_left,right_keys)),carried,f"resolved_role_{i}",
                kind=JoinType.INNER if variant == "inner_instead_of_left" else JoinType.LEFT,variant=variant,edge=i)
            missing = "FALSE" if variant == "null_equals_unmatched" else _null(raw)
            invalid = f"WHEN {_null(keys)} THEN 'invalid' " if recipe == NORMALIZED and variant != "no_normalization" else ""
            state = f"role_{i}_state" if recipe == ROLES else "relationship_state"
            current = b.project(current, f"*, CASE WHEN {missing} THEN 'missing' {invalid}WHEN {marker} IS NULL THEN 'unmatched' ELSE 'matched' END AS {state}", f"role_{i}_coverage")
            metric_names.extend((label,state) if recipe == ROLES else (*(f"resolved_key_{j}" for j in range(len(keys))),state))
        b.project(current, _project(g,e.grain_key) + ", " + ", ".join(metric_names), "relationship_output")
        return tuple(b.ops)

    parents = b.project(e.grain_table,_project(e.grain_key,g),"parent_keys")

    def records(first, second, prefix):
        targets = _names(prefix,second.parent_columns)
        relation = b.project(first.child_table,_project(first.child_columns,g)+", "+_project(second.child_columns,targets),"bridge_records")
        return relation, targets

    def resolve(relation, edge, keys, marker, inner=False, carried=()):
        target_keys = _names("dimension_key_",edge.parent_columns)
        projection = _project(edge.parent_columns,target_keys) + f", 1 AS {marker}"
        projection += "".join(f", {q(c)} AS {q(a)}" for c,a in carried)
        target = b.project(edge.parent_table,projection,"dimension_entities")
        return _join(b,relation,target,tuple(zip(keys,target_keys)),((marker,marker),*((a,a) for _,a in carried)),
                     "dimension_memberships",kind=JoinType.INNER if inner else JoinType.LEFT)

    def unique(relation, keys, stem):
        return b.aggregate(relation,keys,{"physical_copies":"COUNT(*)"},stem)

    if recipe == COMPOSITE:
        records_table,t = records(*e.edges,"t")
        resolved = resolve(records_table,e.edges[1],t,"target_present")
        totals = b.aggregate(resolved,g,{"physical_link_count":"COUNT(*)", "matched_link_count":"COUNT(target_present)",
            "null_target_count":f"COUNT(CASE WHEN {_null(t)} THEN 1 END)",
            "unmatched_target_count":f"COUNT(CASE WHEN NOT {_null(t)} AND target_present IS NULL THEN 1 END)"},"link_totals")
        matched = b.filter(resolved,"target_present IS NOT NULL","matched_memberships")
        memberships = matched if variant == "count_instead_of_count_distinct" else unique(matched,(*g,*t),"unique_memberships")
        counts = b.aggregate(memberships,g,{"distinct_target_count":"COUNT(*)"},"membership_totals")
        metrics = ("physical_link_count","matched_link_count","null_target_count","unmatched_target_count")
        totals = b.join(totals,counts,g,("distinct_target_count",),"bridge_summary")
        metrics += ("distinct_target_count",)
    elif recipe == PATHS:
        paths = []
        for i in (0,1):
            relation,t = records(e.edges[2*i],e.edges[2*i+1],"t")
            relation = resolve(relation,e.edges[2*i+1],t,"target_present",inner=True)
            if variant != "count_instead_of_count_distinct":
                relation = unique(relation,(*g,*t),"path_memberships")
            selected = 1-i if variant == "wrong_relationship_path" else i
            paths.append(b.project(relation,", ".join((*g,*t))+f", {int(selected==0)} AS requested, {int(selected==1)} AS alternate","labelled_paths"))
        combined = b.add(MartOpKind.UNION,paths,"all_memberships",details={"mode":"all"})
        by_target = b.aggregate(combined,(*g,*t),{"requested":"SUM(requested)","alternate":"SUM(alternate)"},"memberships_by_path")
        totals = b.aggregate(by_target,g,{"requested_member_count":"SUM(requested)", "alternate_member_count":"SUM(alternate)",
            "both_path_count":"COUNT(CASE WHEN requested>0 AND alternate>0 THEN 1 END)",
            "requested_only_count":"COUNT(CASE WHEN requested>0 AND alternate=0 THEN 1 END)"},"path_summary")
        metrics = ("requested_member_count","alternate_member_count","both_path_count","requested_only_count")
    elif recipe == BRIDGES:
        first,m = records(e.edges[0],e.edges[1],"m")
        first = resolve(first,e.edges[1],m,"middle_present",inner=True)
        t = _names("t",e.edges[3].parent_columns)
        second = b.project(e.edges[2].child_table,_project(e.edges[2].child_columns,m)+", "+_project(e.edges[3].child_columns,t),"second_bridge")
        second = resolve(second,e.edges[3],t,"target_present",inner=True)
        if variant != "missing_bridge_deduplication":
            first = unique(first,(*g,*m),"first_bridge_memberships")
            second = unique(second,(*m,*t),"second_bridge_memberships")
        joined = _join(b,first,second,tuple(zip(m,m)),tuple((k,k) for k in t),"reachable_memberships",kind=JoinType.INNER)
        totals = b.aggregate(joined,g,{"membership_path_count":"COUNT(*)"},"path_totals")
        targets = joined if variant == "count_instead_of_count_distinct" else unique(joined,(*g,*t),"reachable_targets")
        counts = b.aggregate(targets,g,{"distinct_target_count":"COUNT(*)"},"target_totals")
        middle = unique(joined,(*g,*m),"reachable_intermediates")
        middle = b.aggregate(middle,g,{"distinct_middle_count":"COUNT(*)"},"middle_totals")
        totals = b.join(totals,counts,g,("distinct_target_count",),"target_summary")
        totals = b.join(totals,middle,g,("distinct_middle_count",),"bridge_summary")
        metrics = ("membership_path_count","distinct_target_count","distinct_middle_count")
    else:
        relation,m = records(e.edges[0],e.edges[1],"m")
        t = _names("t",e.edges[2].parent_columns)
        relation = resolve(relation,e.edges[1],m,"middle_present",inner=variant != "left_instead_of_inner",
                           carried=tuple(zip(e.edges[2].child_columns,t)))
        relation = resolve(relation,e.edges[2],t,"target_present",inner=variant == "inner_instead_of_left")
        orphan_condition = "target_present IS NULL" if variant == "null_equals_unmatched" else f"NOT {_null(t)} AND target_present IS NULL"
        totals = b.aggregate(relation,g,{"eligible_record_count":"COUNT(*)", "complete_record_count":"COUNT(target_present)",
            "null_final_count":f"COUNT(CASE WHEN {_null(t)} THEN 1 END)",
            "orphan_final_count":f"COUNT(CASE WHEN {orphan_condition} THEN 1 END)"},"chain_coverage")
        metrics = ("eligible_record_count","complete_record_count","null_final_count","orphan_final_count")
    current = _join(b,parents,totals,tuple(zip(g,g)),tuple((c,c) for c in metrics),"all_parent_coverage",
        kind=JoinType.INNER if variant == "inner_instead_of_left" and recipe != OPTIONAL else JoinType.LEFT,variant=variant)
    outputs = [_project(g,e.grain_key), *(f"COALESCE({c},0) AS {c}" for c in metrics)]
    if recipe == COMPOSITE:
        outputs.append("COALESCE(matched_link_count,0)-COALESCE(distinct_target_count,0) AS duplicate_link_count")
    if recipe == OPTIONAL:
        outputs.append("COALESCE(complete_record_count,0)=0 AS no_complete_activity")
    b.project(current,", ".join(outputs),"relationship_output")
    return tuple(b.ops)


def read_graph_binding(notes):
    data = json.loads(notes)
    if set(data) != {"evidence","source_tables"} or set(data["evidence"]) != {f.name for f in fields(GraphBinding)}:
        raise ValueError("Invalid graph recipe binding")
    raw = data["evidence"]
    raw["grain_key"] = tuple(raw["grain_key"])
    edges = []
    for item in raw["edges"]:
        for name in ("child_columns","parent_columns","nullable_columns","child_types","parent_types","normalization"):
            item[name] = tuple(item[name])
        edges.append(RelationshipEdgeEvidence(**item))
    raw["edges"] = tuple(edges)
    return GraphBinding(**raw), tuple(data["source_tables"])


def certifies_graph(plan, recipe):
    if plan.template_id != recipe:
        return False
    try:
        e,sources = read_graph_binding(plan.notes)
        stub = SimpleNamespace(tables=tuple(SimpleNamespace(name=n) for n in sources))
        return plan.ops == build_graph_ops(stub,e,recipe)
    except (ValueError,TypeError,KeyError,AttributeError,IndexError):
        return False


def graph_plan(task, e, mart, recipe, variant=""):
    if variant and variant not in graph_variants(e,recipe):
        raise ValueError("Unknown graph mutation")
    columns = [MartColumn(name=k,type=task.table(e.grain_table).column(k).type,description=f"{e.grain_table}.{k}, part of the complete output key.") for k in e.grain_key]
    def col(name,description,kind=T.BIGINT):
        columns.append(MartColumn(name=name,type=kind,description=description))
    relationships = "; ".join(f"{r.child_table}{r.child_columns} references {r.parent_table}{r.parent_columns}" for r in e.edges)
    if recipe == ROLES:
        description = f"Separate entity identities for each declared role on {e.grain_table}."
        for i,edge in enumerate(e.edges):
            col(f"role_{i}_label",f"{edge.parent_table}.{e.label} for role {i}, identified only by {e.grain_table}{edge.child_columns} against the complete {edge.parent_columns}. NULL for missing/unmatched roles; a matched entity can also have a NULL label. Other roles cannot supply this label.",T.TEXT)
            col(f"role_{i}_state",f"For role {i}: 'missing' if any {edge.child_columns} component is NULL; otherwise 'unmatched' when no entity matches all components, else 'matched'.",T.TEXT)
    elif recipe == NORMALIZED:
        edge = e.edges[0]
        description = f"Resolve represented foreign keys from {e.grain_table} to {edge.parent_table}."
        rules = (f"Match all components of {edge.child_columns} to {edge.parent_columns}. For each text FK referencing an integer, trim ASCII spaces, accept only an optional sign followed by ASCII digits, and require a signed 64-bit integer. Leading zeros are insignificant. Reject decimal points, exponents, empty strings, other characters, and overflow. Other components use their declared types.")
        for i,k in enumerate(edge.parent_columns):
            col(f"resolved_key_{i}",f"Matched {edge.parent_table}.{k}; NULL unless the complete key resolves. "+rules,task.table(edge.parent_table).column(k).type)
        col("relationship_state","'missing' if any raw FK component is NULL; else 'invalid' if numeric normalization fails; else 'unmatched' if no complete parent key matches; else 'matched'. "+rules,T.TEXT)
    elif recipe == COMPOSITE:
        description = "Membership and duplicate coverage through a complete composite parent key. " + relationships
        col("physical_link_count",f"Number of {e.edges[0].child_table} rows for this complete parent key, including repeated memberships and missing target links.")
        col("matched_link_count","Number of those physical rows whose complete target key resolves.")
        col("null_target_count","Number with any NULL target FK component.")
        col("unmatched_target_count","Number with all target components non-NULL but no matching target entity.")
        col("distinct_target_count","Number of distinct complete matched target keys, regardless of repeated link records.")
        col("duplicate_link_count","matched_link_count minus distinct_target_count; repeated memberships count as duplicates even when link record identities differ.")
    elif recipe == PATHS:
        description = "Compare two separately named membership paths. " + relationships
        col("requested_member_count",f"Distinct complete {e.edges[1].parent_table} keys reachable through {e.edges[0].child_table}, the requested path. Ignore unresolved target links.")
        col("alternate_member_count",f"Distinct complete {e.edges[3].parent_table} keys reachable through {e.edges[2].child_table}, the alternate path. Keep its meaning separate from the requested path.")
        col("both_path_count","Number of target entities in both membership sets; duplicate bridge rows never increase this count.")
        col("requested_only_count","Number of entities in the requested membership set and absent from the alternate set.")
    elif recipe == BRIDGES:
        description = "Distinct memberships across two successive many-to-many bridges. " + relationships
        col("membership_path_count","Number of distinct (intermediate entity key, target entity key) pairs reachable for this parent. Resolve both entities and deduplicate each bridge's complete relationship pair; link record IDs do not define membership.")
        col("distinct_target_count","Number of distinct complete target entity keys reachable through any intermediate; an entity reached via multiple intermediates counts once.")
        col("distinct_middle_count","Number of distinct complete intermediate keys participating in at least one resolved target path.")
    else:
        description = "Coverage of activity requiring an intermediate entity and optionally a final entity. " + relationships
        col("eligible_record_count",f"Number of {e.edges[0].child_table} records for this parent whose complete intermediate key resolves. Exclude records with NULL or unmatched intermediate keys.")
        col("complete_record_count","Number of eligible records whose final entity resolves; keep separate records even when their final entity is the same.")
        col("null_final_count","Number of eligible records whose intermediate entity has any NULL final FK component.")
        col("orphan_final_count","Number of eligible records whose intermediate entity has a non-NULL complete final FK with no matching final entity.")
        col("no_complete_activity","True when complete_record_count is zero, including parents with no eligible records. Partial activity must remain visible in the other counts.",T.BOOLEAN)
    grain = f"One row per complete {e.grain_table}{e.grain_key}; retain every source key, including unmatched roles or parents without activity. Counts are zero when absent."
    if len({c.name for c in columns}) != len(columns):
        from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
        raise SemanticDifficultyError("Graph output names collide with source keys")
    plan = MartPlan(mart=mart,ops=build_graph_ops(task,e,recipe,variant),template_id=recipe,
        notes=canonical_json({"evidence":asdict(e),"source_tables":sorted(t.name for t in task.tables)}))
    plan = plan.model_copy(update={"semantic_patterns":detect_semantic_patterns(plan)})
    if not variant and PATTERNS[recipe] not in plan.semantic_patterns:
        raise ValueError(f"Uncertified graph recipe: {recipe}")
    kinds = column_kinds_from_plan(plan)
    columns = tuple(c.model_copy(update={"kind":kinds.get(c.name,MartColumnKind.PASSTHROUGH if c.name in e.grain_key else MartColumnKind.DERIVED)}) for c in columns)
    return MartSpec(name=mart,description=description,grain=grain,key_columns=e.grain_key,columns=columns,plan=plan)

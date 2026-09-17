"""Plan five populations, validate coverage, and derive targeted attacks.

Population definitions are created at ingest; later stages only validate and
materialize content-bound specifications.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from itertools import islice, product

from elt_taskgen.generation import source_data
from elt_taskgen.generation.difficulty_profiles import GenerationDifficultyProfile
from elt_taskgen.generation.mart_plan import (
    WITNESS_ALL_FAIL,
    WITNESS_ARGMAX_CASE_ORDER,
    WITNESS_ARGMAX_NULL_ORDER,
    WITNESS_BELOW_THRESHOLD,
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_DUPLICATE,
    WITNESS_DISTINCT_MEASURE,
    WITNESS_LATEST_FIRST,
    WITNESS_NULL_MEASURE,
    WITNESS_ON_THRESHOLD,
    WITNESS_ORDER,
    WITNESS_OUT_OF_DOMAIN,
    WITNESS_SAME_CHILD,
    WITNESS_SECOND_PERIOD,
    WITNESS_TIE,
    AGGREGATE_FAMILY_KINDS,
    AllNullAggregateWitness,
    FactRoles,
    RoundBeforeSumWitness,
    StarShape,
    TEXT_ORDER_PROSE,
    guard_literals,
)
from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    ColumnSpec,
    ColumnType,
    PopulationName,
    PopulationSpec,
    Relationship,
    Row,
    TableSpec,
    TaskIR,
    derive_seed,
)

#: Fixed epoch for minted temporal literals — never the wall clock.
_CF_EPOCH_DATE = date(2024, 1, 1)
_CF_EPOCH_DATETIME = datetime(2024, 1, 1, 0, 0, 0)

#: Re-exported so the generate stage can record which generator ran.
GENERATION_POLICY_VERSION = source_data.GENERATION_POLICY_VERSION


class CounterfactualMergeError(ValueError):
    """The multi-shape counterfactual merge introduced a broken foreign key.

    FK resolution is monotone under adding parent rows, so an unresolved key no
    standalone build had is a merge defect and is refused at ingest.
    """


class InertDuplicateWitnessError(ValueError):
    """The constructed duplicate witness is NOT byte-identical — it is inert.

    DISTINCT never collapses distinct rows, so a pair split by a post-
    construction pass witnesses nothing: construction RAISES rather than freeze
    it.
    """

#: The directive verification/attacks.py turns into "emit the frozen primary gold".
_HARDCODE_PRIMARY_DIRECTIVE = "directive:hardcode-population-outputs:primary"

#: Development scale: tiny and readable. max(2, min(_DEV_CAP, hint // 125)).
_DEV_CAP = 8
#: Stress scale multiplier over the primary scale hint, capped per table.
STRESS_FACTOR = 10
STRESS_CAP = 50_000

#: Coverage budget (see `coverage_budget_scale`): STRESS asks for this multiple
#: of (parents x enum values) so a REMAINDER is left for the whale to skew.
STRESS_WHALE_HEADROOM = 2
#: Development stays tiny: a child's coverage budget never pushes its declared
#: scale past this; beyond it the population DEGRADES and its conditions say so.
DEV_COVERAGE_CAP = 120

#: The neutral stress-duplicate sentence `default_populations` emits;
#: `derive_populations_and_attacks` replaces it with plan-derived prose.
_STRESS_DUPLICATE_NEUTRAL = (
    "Exact-duplicate rows are injected into every table that declares no "
    "primary key."
)


def _dev_scale(hint: dict[str, int]) -> dict[str, int]:
    return {t: max(2, min(_DEV_CAP, int(n) // 125)) for t, n in sorted(hint.items())}


def _stress_scale(hint: dict[str, int]) -> dict[str, int]:
    return {t: min(int(n) * STRESS_FACTOR, STRESS_CAP) for t, n in sorted(hint.items())}


def _scale_realizing_at_least(task_id: str, table: str, need: int, floor: int) -> int:
    """The smallest declared scale >= floor whose REALIZED count is >= need.

    Checked against `realized_row_count`, never the declared number.
    """
    scale = max(int(floor), int(need))
    while source_data.realized_row_count(task_id, table, scale) < need:
        scale += 1
    return scale


def _parent_pool_bound(
    task_id: str,
    relationships: Sequence[Relationship],
    parent: TableSpec,
    rel: Relationship,
    scale: Mapping[str, int],
) -> int | None:
    """How many distinct parent keys `rel` can draw from, or None if unknown.

    Identity columns are minted one per row, so the pool IS the realized parent
    count; a non-identity column is bounded by its domain instead.
    """
    if parent.name not in scale:
        return None
    realized = source_data.realized_row_count(task_id, parent.name, scale[parent.name])
    parent_fk = {c for r in relationships if r.child_table == parent.name for c in r.child_columns}
    identity = source_data.identity_columns_for(relationships, parent, parent_fk)
    if all(c in identity for c in rel.parent_columns):
        return realized
    bound = 1
    for name in rel.parent_columns:
        col = parent.column(name)
        if col.enum_values:
            bound *= len(col.enum_values)
        elif col.type is ColumnType.BOOLEAN:
            bound *= 2
        else:
            bound *= max(realized, 1)
    return min(realized, bound)


def coverage_budget_scale(
    task_id: str,
    tables: Sequence[TableSpec],
    relationships: Sequence[Relationship],
    scale: Mapping[str, int],
    *,
    population: PopulationName,
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Raise each child's declared scale until its coverage conditions are TRUE.

    Returns (scale, notes); only development and stress carry a coverage policy.
    When a cap binds, the scale DEGRADES to the best tier that fits and a note
    names the shortfall — callers append the notes to the population's conditions
    so the prose never claims coverage the data does not have.
    """
    scale_out = {t: int(n) for t, n in scale.items()}
    notes: list[str] = []
    if population not in (PopulationName.DEVELOPMENT, PopulationName.STRESS):
        return scale_out, ()
    by_name = {t.name: t for t in tables}
    order = _topological_tables(by_name, list(relationships))
    is_stress = population is PopulationName.STRESS
    cap = STRESS_CAP if is_stress else DEV_COVERAGE_CAP
    for name in order:
        rels = [r for r in relationships if r.child_table == name]
        if not rels or name not in scale_out or scale_out[name] <= 0:
            continue
        tspec = by_name[name]
        rel = rels[0]
        parent = by_name.get(rel.parent_table)
        if parent is None or rel.parent_table == name:
            # Unknown parent, or a self link whose generator pool is empty
            # (every such FK is NULL): nothing to cover.
            continue
        fk_cols = {c for r in rels for c in r.child_columns}
        id_cols = source_data.identity_columns_for(relationships, tspec, fk_cols)
        primary_enum = source_data.primary_enum_column(tspec, fk_cols, id_cols)
        domain = len(tspec.column(primary_enum).enum_values or ()) if primary_enum else 1
        pool = _parent_pool_bound(task_id, relationships, parent, rel, scale_out)
        if not pool:
            continue
        current = scale_out[name]
        fk_only_groups = [
            group
            for group in (tspec.primary_key, tspec.business_key)
            if group and set(group) <= fk_cols
        ]
        if any(set(group) <= set(rel.child_columns) for group in fk_only_groups):
            # A key made SOLELY of the first link's columns (a 1:1 child): its
            # capacity IS the parent pool, so not every parent can get a child.
            notes.append(
                f"COVERAGE SHORTFALL: {name} is keyed by its foreign key(s), so "
                f"its rows are bounded by the key capacity — not every "
                f"{parent.name} row can receive a {name} row here."
            )
            continue
        table_cap = cap
        cap_reason = f"the row cap of {cap}"
        if fk_only_groups:
            # A key spanning SEVERAL links (a bridge): capacity is the product
            # of the key's parent pools; the tiers apply below that bound.
            capacity: int | None = None
            for group in fk_only_groups:
                product = 1
                for r in rels:
                    if not set(r.child_columns) & set(group):
                        continue
                    r_parent = by_name.get(r.parent_table)
                    bound = (
                        _parent_pool_bound(task_id, relationships, r_parent, r, scale_out)
                        if r_parent is not None and r.parent_table != name
                        else None
                    )
                    if bound is None:
                        product = 0
                        break
                    product *= bound
                if product <= 0:
                    continue
                capacity = product if capacity is None else min(capacity, product)
            if capacity is None:
                continue  # unknown parent pool: the generator's capacity error is the backstop
            key_cap = int(FK_IDENTITY_CAPACITY_SAFETY * capacity)
            if key_cap < table_cap:
                table_cap = key_cap
                cap_reason = (
                    f"the key capacity of {key_cap} ({name} is keyed entirely by "
                    "foreign keys)"
                )
        pairs = pool * max(domain, 1)
        # (target rows, what falls short when THIS tier is the best that fits)
        tiers: list[tuple[int, str]] = []
        if is_stress:
            tiers.append((STRESS_WHALE_HEADROOM * pairs, ""))
            tiers.append((pairs, "whale headroom"))
        else:
            tiers.append((pairs, ""))
        if domain > 1:
            tiers.append((pool, "the (parent x enum value) cross product"))
        realized = source_data.realized_row_count(task_id, name, current)
        chosen: int | None = None
        for target, shortfall in tiers:
            if realized >= target:
                chosen = current
            else:
                wanted = _scale_realizing_at_least(task_id, name, target, current)
                if wanted > table_cap:
                    continue
                chosen = wanted
            scale_out[name] = chosen
            if shortfall:
                notes.append(
                    f"COVERAGE SHORTFALL: {name} -> {parent.name} is budgeted for "
                    f"{target} rows, short of {shortfall} ({cap_reason} "
                    "binds); parents are served first, so every "
                    f"{parent.name} row still has a {name} row"
                    + (
                        " but not every (parent, enum value) pair occurs."
                        if shortfall.startswith("the (parent")
                        else " and every pair occurs, but the skew remainder is small."
                    )
                )
            break
        if chosen is None:
            scale_out[name] = max(current, min(table_cap, pool))
            notes.append(
                f"COVERAGE SHORTFALL: {name} -> {parent.name} needs at least "
                f"{pool} rows to give every parent a child but {cap_reason} "
                f"binds; some {parent.name} rows have NO {name} rows here."
            )
    return scale_out, tuple(notes)


def scales_by_population(
    task_id: str,
    scale_hint: Mapping[str, int],
    *,
    tables: Sequence[TableSpec] = (),
    relationships: Sequence[Relationship] = (),
) -> dict[PopulationName, dict[str, int]]:
    """The declared scale each population gets from `default_populations`
    (counterfactual: {} — it carries literal rows). With `tables` and
    `relationships` the coverage budget is applied exactly as
    `derive_populations_and_attacks` applies it."""
    return {
        spec.name: dict(spec.scale)
        for spec in default_populations(
            task_id, dict(scale_hint), tables=tables, relationships=relationships
        )
    }


def default_populations(
    task_id: str,
    scale_hint: dict[str, int],
    *,
    tables: Sequence[TableSpec] = (),
    relationships: Sequence[Relationship] = (),
) -> tuple[PopulationSpec, ...]:
    """Build the default five-population plan for a task.

    `scale_hint` is the INTENDED primary row count per table and seeds are
    derived, never chosen. INTENDED, NOT REALIZED: `realized_row_count` moves
    each table off its declared scale, so prose derived from one must hedge.
    """
    hint = {t: int(n) for t, n in scale_hint.items()}
    dev_scale, dev_notes = _dev_scale(hint), ()
    stress_scale, stress_notes = _stress_scale(hint), ()
    if tables:
        dev_scale, dev_notes = coverage_budget_scale(
            task_id, tables, relationships, dev_scale, population=PopulationName.DEVELOPMENT
        )
        stress_scale, stress_notes = coverage_budget_scale(
            task_id, tables, relationships, stress_scale, population=PopulationName.STRESS
        )
    return (
        PopulationSpec(
            name=PopulationName.DEVELOPMENT,
            seed=derive_seed(task_id, PopulationName.DEVELOPMENT.value),
            scale=dev_scale,
            conditions=(
                "Tiny, readable debug data.",
                "Full link coverage: every parent row has child rows and every "
                "enum value occurs for every parent WHILE THE TABLE'S ROW "
                "BUDGET ALLOWS — parents are served first, so a realized child "
                "count below the (parent x enum value) cross product costs "
                "pairs before parents; the budget is sized for both and any "
                "shortfall is stated below (wrong join types are "
                "indistinguishable here by design).",
                "No NULL foreign keys, no duplicate rows.",
            )
            + dev_notes,
        ),
        PopulationSpec(
            name=PopulationName.PRIMARY,
            seed=derive_seed(task_id, PopulationName.PRIMARY.value),
            scale=dict(sorted(hint.items())),
            conditions=(
                "Realistic hidden reward data at approximately the hinted "
                "scale (the realized per-table row counts sit a few percent "
                "off it — the declared scale is an intent, not a count).",
                "Some parent rows have no child rows through the FIRST declared "
                "link of a child table when that link is optional (later links, "
                "and required links, give every parent children while the "
                "row budget allows).",
                "For every enum value, some parents have children that avoid it "
                "entirely (e.g. customers whose orders are never completed).",
                "Optional nullable foreign keys contain NULLs.",
            ),
        ),
        PopulationSpec(
            name=PopulationName.RESAMPLED,
            seed=derive_seed(task_id, PopulationName.RESAMPLED.value),
            scale=dict(sorted(hint.items())),
            conditions=(
                "Same generator and conditions as primary; new derived seed "
                "and new id ranges (memorization check).",
            ),
        ),
        PopulationSpec(
            name=PopulationName.COUNTERFACTUAL,
            seed=derive_seed(task_id, PopulationName.COUNTERFACTUAL.value),
            conditions=(
                "PLACEHOLDER: construct literal rows that individually break "
                "each required attack mutant, then set literal_rows. "
                "validate_population_coverage rejects this placeholder.",
            ),
        ),
        PopulationSpec(
            name=PopulationName.STRESS,
            seed=derive_seed(task_id, PopulationName.STRESS.value),
            scale=stress_scale,
            conditions=(
                "Scale, skew, duplicates, ties.",
                "One heavily skewed parent holds a large share of all children.",
                _STRESS_DUPLICATE_NEUTRAL,
                "Measure columns draw from small palettes so ties occur.",
                "Full link coverage: every parent has children for every enum "
                "value WHILE THE TABLE'S ROW BUDGET ALLOWS — parents are served "
                "first, then (parent, enum value) pairs, then the skew "
                "remainder; the budget is sized for all three and any "
                "shortfall is stated below (wrong join types are "
                "indistinguishable here by design).",
            )
            + stress_notes,
        ),
    )


_DIFFICULTY_PROFILE_CONDITION_PREFIX = "GENERATION DIFFICULTY PROFILE "


def apply_data_scale_profile(
    task: TaskIR, profile: GenerationDifficultyProfile
) -> TaskIR:
    """Apply a bounded row-volume target to a synthetic TaskIR.

    The operation is deterministic and idempotent.  It is deliberately safe to
    call after effective-lineage pruning: the floor is computed over only the
    source tables the compiled marts actually read.  DEVELOPMENT remains tiny,
    COUNTERFACTUAL keeps its literal semantic witnesses, and literal PRIMARY
    populations (WikiDBs/other real data) are left byte-for-byte unchanged.
    """

    by_name = {population.name: population for population in task.populations}
    primary = by_name.get(PopulationName.PRIMARY)
    if (
        primary is None
        or not primary.scale
        or primary.literal_rows
        or not profile.synthetic_primary_row_floor
    ):
        return task

    scaled_hint = profile.scale_hint(primary.scale)
    profile_condition = (
        f"{_DIFFICULTY_PROFILE_CONDITION_PREFIX}{profile.name}: the active "
        f"synthetic PRIMARY and RESAMPLED scale vectors contain at least "
        f"{profile.synthetic_primary_row_floor} declared rows in total."
    )
    bounded_stress_condition = (
        f"{_DIFFICULTY_PROFILE_CONDITION_PREFIX}{profile.name}: STRESS keeps "
        "the adapter's bounded semantic-adversarial scale; the larger row-volume "
        "challenge is carried by PRIMARY and RESAMPLED."
    )
    populations: list[PopulationSpec] = []
    for population in task.populations:
        if population.name in {PopulationName.PRIMARY, PopulationName.RESAMPLED}:
            conditions = tuple(
                condition
                for condition in population.conditions
                if not condition.startswith(_DIFFICULTY_PROFILE_CONDITION_PREFIX)
            )
            conditions += (profile_condition,)
            population = population.model_copy(
                update={"scale": scaled_hint, "conditions": conditions}
            )
        elif population.name is PopulationName.STRESS:
            conditions = tuple(
                condition
                for condition in population.conditions
                if not condition.startswith(_DIFFICULTY_PROFILE_CONDITION_PREFIX)
            ) + (bounded_stress_condition,)
            population = population.model_copy(update={"conditions": conditions})
        populations.append(population)

    return task.model_copy(update={"populations": tuple(populations)})


def stress_duplicate_conditions(
    tables: Sequence[TableSpec],
    deduped_tables: Iterable[str],
    read_tables: Iterable[str],
    *,
    duplicated_tables: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """The stress population's duplicate sentence(s), GENERATED FROM PLAN FACTS.

    Duplicates are injected into every PK-less stress table, but only a plan
    carrying a DEDUPE op counts each copy once, so the sentence must name which
    tables a mart deduplicates and which it reads raw; a fixed claim is a lie.
    """
    declared = {table.name: table for table in tables}
    if duplicated_tables is None:
        duplicated = sorted(t.name for t in tables if not t.primary_key)
    else:
        duplicated = sorted(set(duplicated_tables))
        unknown = sorted(set(duplicated) - set(declared))
        if unknown:
            raise ValueError(
                "stress duplicate conditions name undeclared tables: "
                + ", ".join(unknown)
            )
    if not duplicated:
        return ("Every table declares a primary key, so no exact-duplicate rows are injected.",)
    deduped = sorted(set(deduped_tables) & set(duplicated))
    raw = sorted(
        table
        for table in duplicated
        if table not in deduped and table in set(read_tables)
    )
    lines = [
        "Exact-duplicate rows are injected into "
        f"{', '.join(duplicated)}."
    ]
    if deduped:
        lines.append(
            f"The marts deduplicate {', '.join(deduped)}: each byte-identical copy "
            "counts ONCE, as that mart's own column descriptions state."
        )
    if raw:
        lines.append(
            f"No mart deduplicates {', '.join(raw)}: every physical copy is an "
            "ordinary row and counts each time it appears (a replica of an "
            "extremal row therefore TIES with it)."
        )
    return tuple(lines)


# --- Coverage validation ---

def _literal_row_problems(task: TaskIR, pop: PopulationSpec) -> list[str]:
    """Schema-check literal rows: columns, nullability, enums, PKs, business
    keys (unique among DISTINCT rows — a byte-identical duplicate witness is the
    one sanctioned repetition), required FKs. Everything the generator would
    refuse is reported HERE, as a stage-3 RepairRoute.POPULATION refusal."""
    problems: list[str] = []
    for table in sorted(pop.literal_rows):
        tspec = task.table(table)  # existence guaranteed by TaskIR validator
        known = {c.name for c in tspec.columns}
        seen_pk: set[tuple] = set()
        seen_bk: dict[tuple, tuple] = {}
        for i, row in enumerate(pop.literal_rows[table]):
            where = f"population {pop.name.value}: {table} literal row {i}"
            unknown = sorted(set(row) - known)
            if unknown:
                problems.append(f"{where}: unknown columns {unknown}")
            for col in tspec.columns:
                value = row.get(col.name)
                if value is None:
                    if not col.nullable:
                        problems.append(
                            f"{where}: column {col.name!r} is not nullable but is missing/None"
                        )
                elif col.enum_values is not None and value not in col.enum_values:
                    problems.append(
                        f"{where}: column {col.name!r} value {value!r} outside enum domain "
                        f"{list(col.enum_values)}"
                    )
            if tspec.primary_key:
                key = tuple(row.get(c) for c in tspec.primary_key)
                if key in seen_pk:
                    problems.append(f"{where}: duplicate primary key {key}")
                seen_pk.add(key)
            if tspec.business_key:
                bkey = tuple(row.get(c) for c in tspec.business_key)
                if not any(v is None for v in bkey):
                    identity = tuple(sorted(row.items(), key=lambda kv: kv[0]))
                    if bkey in seen_bk and seen_bk[bkey] != identity:
                        problems.append(
                            f"{where}: business key {tuple(tspec.business_key)} repeats "
                            f"{bkey} across DISTINCT rows (only a byte-identical "
                            "duplicate may repeat a business key)"
                        )
                    seen_bk.setdefault(bkey, identity)

    for rel in task.relationships:
        if not rel.required:
            continue
        if rel.child_table not in pop.literal_rows or rel.parent_table not in pop.literal_rows:
            continue
        parent_keys = {
            tuple(r.get(c) for c in rel.parent_columns)
            for r in pop.literal_rows[rel.parent_table]
        }
        for i, row in enumerate(pop.literal_rows[rel.child_table]):
            key = tuple(row.get(c) for c in rel.child_columns)
            if key not in parent_keys:
                problems.append(
                    f"population {pop.name.value}: {rel.child_table} literal row {i} "
                    f"references missing {rel.parent_table} key {key} (required link)"
                )
    return problems


def _dead_predicate_problems(task: TaskIR) -> list[str]:
    """A predicate no row can satisfy is a DEAD measure — refuse it here.

    THE TEST IS PRODUCIBILITY, NOT PRESENCE (a declared enum domain or a literal
    row), the weakest sound test: no false positives, but it does not prove the
    literal reaches the RIGHT column — adapters own the exact binding.
    """
    producible: set[str] = set()
    for table in task.tables:
        for column in table.columns:
            producible.update(column.enum_values or ())
    for pop in task.populations:
        for rows in pop.literal_rows.values():
            for row in rows:
                producible.update(v for v in row.values() if isinstance(v, str))

    problems: list[str] = []
    for mart in task.marts:
        for idx, op in enumerate(mart.plan.ops):
            # EVERY aggregate-family op: a plain `aggregate` op CAN carry a
            # CASE guard, and a dead measure is dead whatever the op is called.
            if op.kind not in AGGREGATE_FAMILY_KINDS:
                continue
            for alias, column, literal in guard_literals(op):
                if literal in producible:
                    continue
                problems.append(
                    f"mart {mart.name!r} op[{idx}] measure {alias!r}: its guard selects "
                    f"on {column} = {literal!r}, which NO column of this task can "
                    "produce (it is in no declared enum domain and in no literal row) "
                    "— the measure is a constant over every population, so the plan "
                    "must not select on it, or the schema must declare the domain "
                    "that contains it"
                )
    return problems


def _fk_identity_capacity_problems(task: TaskIR) -> list[str]:
    """A key made only of foreign keys needs a key space >= the row count.

    Asking for more rows than the parent pools' product fails closed HERE, at
    stage 3, before the LLM stages are paid for; an unknown pool is left to the
    generator's `FkIdentityCapacityError`.
    """
    problems: list[str] = []
    by_name = {t.name: t for t in task.tables}
    for pop in task.populations:
        if not pop.scale:
            continue
        for tspec in task.tables:
            if tspec.name in pop.literal_rows or pop.scale.get(tspec.name, 0) <= 0:
                continue
            rels = [r for r in task.relationships if r.child_table == tspec.name]
            fk_cols = {c for r in rels for c in r.child_columns}
            for group, label in ((tspec.primary_key, "primary key"), (tspec.business_key, "business key")):
                if not group or not set(group) <= fk_cols:
                    continue
                capacity = 1
                known = True
                for rel in rels:
                    if not set(rel.child_columns) & set(group):
                        continue
                    parent = by_name[rel.parent_table]
                    bound = _parent_pool_bound(
                        task.task_id, task.relationships, parent, rel, pop.scale
                    )
                    if bound is None:
                        known = False
                        break
                    capacity *= bound
                if not known:
                    continue
                n = source_data.realized_row_count(
                    task.task_id, tspec.name, pop.scale[tspec.name]
                )
                if n > capacity:
                    problems.append(
                        f"population {pop.name.value}: table {tspec.name} {label} "
                        f"{tuple(group)} is composed entirely of foreign keys; unique "
                        f"key space is at most {capacity} but the scale realizes {n} "
                        "rows — lower the scale or declare a surrogate key"
                    )
    return problems


def _synthetic_required_parent_problems(task: TaskIR) -> list[str]:
    """Reject scaled rows whose required parent pool cannot exist yet.

    ``source_data.generate_rows`` builds one table at a time. Optional edges may
    break a dependency cycle, but a cycle made entirely of required edges has no
    legal first table: whichever child is generated first sees an empty parent
    pool. Catch the exact generation-order failure during intake instead of
    waiting for fixture materialization after provider-backed stages.
    """

    order = source_data._topo_order(task)
    position = {name: index for index, name in enumerate(order)}
    problems: list[str] = []
    for pop in task.populations:
        for rel in task.relationships:
            if not rel.required or rel.child_table in pop.literal_rows:
                continue
            child_scale = pop.scale.get(rel.child_table, 0)
            if child_scale <= 0:
                continue
            child_rows = source_data.realized_row_count(
                task.task_id, rel.child_table, child_scale
            )
            if child_rows <= 0:
                continue

            parent_available = position[rel.parent_table] < position[rel.child_table]
            if rel.parent_table in pop.literal_rows:
                parent_rows = len(pop.literal_rows[rel.parent_table])
            else:
                parent_scale = pop.scale.get(rel.parent_table, 0)
                parent_rows = (
                    source_data.realized_row_count(
                        task.task_id, rel.parent_table, parent_scale
                    )
                    if parent_scale > 0
                    else 0
                )
            if parent_available and parent_rows > 0:
                continue
            reason = (
                "is generated after its child (a required relationship cycle)"
                if not parent_available
                else "realizes zero rows"
            )
            problems.append(
                f"population {pop.name.value}: table {rel.child_table} realizes "
                f"{child_rows} row(s), but required parent {rel.parent_table} "
                f"{reason}; source generation would see an empty parent pool"
            )
    return problems


def relationship_parent_problems(task: TaskIR) -> list[str]:
    """ADVISORY: relationship parent columns that can never be unique.

    An enum/BOOLEAN/JSON parent column is not minted as an identity, so a link
    onto it fans out on the parent side. Not a coverage failure, just recorded.
    """
    notes: list[str] = []
    for rel in task.relationships:
        parent = task.table(rel.parent_table)
        for name in rel.parent_columns:
            col = parent.column(name)
            if col.enum_values:
                why = f"draws from its enum domain {list(col.enum_values)}"
            elif col.type in source_data._UNMINTABLE_TYPES:
                why = f"is {col.type.value}, which cannot be unique"
            else:
                continue
            notes.append(
                f"relationship {rel.child_table}.{list(rel.child_columns)} -> "
                f"{rel.parent_table}.{name}: the parent column {why}, so the link "
                "is not a key and fans out on the parent side"
            )
    return notes


def validate_population_coverage(task: TaskIR) -> list[str]:
    """Return problem strings; empty list = the population set is ready.

    Fails closed, reporting every problem: a missing population, a dead
    predicate, primary/resampled scale drift, an ungeneratable population, bad
    literal rows, an FK-only key too small for its scale, and a counterfactual
    with neither literal rows nor targeted attack kinds.
    """
    problems: list[str] = _dead_predicate_problems(task)
    problems.extend(_fk_identity_capacity_problems(task))
    problems.extend(_synthetic_required_parent_problems(task))
    by_name = {p.name: p for p in task.populations}

    for name in PopulationName:
        if name not in by_name:
            problems.append(f"missing population: {name.value}")

    primary = by_name.get(PopulationName.PRIMARY)
    resampled = by_name.get(PopulationName.RESAMPLED)
    if primary is not None and resampled is not None and primary.scale != resampled.scale:
        problems.append(
            "primary and resampled populations must share the same scale "
            f"(primary={primary.scale!r}, resampled={resampled.scale!r})"
        )

    for pop in task.populations:
        if (
            pop.name is not PopulationName.COUNTERFACTUAL
            and not pop.scale
            and not pop.literal_rows
        ):
            problems.append(
                f"population {pop.name.value}: no scale and no literal rows — "
                "nothing to generate"
            )
        problems.extend(_literal_row_problems(task, pop))

    counterfactual = by_name.get(PopulationName.COUNTERFACTUAL)
    if counterfactual is not None and not counterfactual.literal_rows:
        text = " ".join(counterfactual.conditions).lower()
        # LOAD-side cases are excluded on purpose: an EL mutant is discriminated
        # by the COUNT VECTOR, not by any row the counterfactual constructs.
        required_kinds = sorted(
            {
                case.kind.value
                for case in task.attack_cases
                if case.required
                and not case.mutation.strip().startswith(_LOAD_DIRECTIVE_PREFIX)
            }
        )
        for kind in required_kinds:
            if kind not in text and kind.replace("_", " ") not in text:
                problems.append(
                    "counterfactual population has no literal rows and its "
                    f"conditions do not target required attack kind {kind!r}"
                )
        if not task.attack_cases:
            problems.append(
                "counterfactual population has no literal rows and the task "
                "declares no attack cases to target"
            )
    return problems


# --- Schema-derived counterfactual + attack catalogue (the shared derivation) ---

#: Literal counterfactual rows mint from this base so no value can coincide with
#: a generated primary/resampled one (the `constants` attack must not score).
COUNTERFACTUAL_ID_BASE = 900

#: Row budget of the constructed counterfactual, per role. Three parent rows is
#: the minimum that carries all three discriminations at once.
_PARENT_ROWS = 3
_FACT_ROWS_MATCHED = 2
_FACT_ROWS_DUPLICATED = 2
#: Reserved source-row indexes for recovered all-NULL aggregate witnesses.
#: Below bridge rows (200), identity-parent repair (300), and dangling-key
#: search (400), but above every legacy A/B/C or plan-library witness row.
_ALL_NULL_MINT_BASE = 50
_ALL_NULL_MINT_ATTEMPTS = 128
_ALL_NULL_PARENT_MINT_BASE = 350
#: Recovered rounding-order witnesses live after the all-NULL search window and
#: before plan-library bridge rows (which start at 200).
_ROUND_BEFORE_SUM_MINT_BASE = 178
_ROUND_BEFORE_SUM_MINT_LIMIT = 200
#: A dedicated child row for the standard shape's optional fact-to-parent
#: discriminator. It must not reuse a functional A--L witness: changing one of
#: those rows can make an unrelated mutation observable for the wrong reason.
#: The value remains below identity-parent repair (300) and dangling-key search
#: (400), and the per-shape mint offset keeps merged marts collision-free.
_OPTIONAL_OWNER_ORPHAN_MINT_BASE = 275

#: Default synthetic scale for pools that ship no rows: a table nothing
#: references is a dimension, anything with an inbound FK is a fact.
DIMENSION_ROWS = 60
FACT_TABLE_ROWS = 240

#: An FK-only key holds at most (product of its parent pools) rows; the hint is
#: capped at this fraction so the realized count (declared +/- 7 %) still fits.
FK_IDENTITY_CAPACITY_SAFETY = 0.85


def schema_scale_hint(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    *,
    dimension_rows: int = DIMENSION_ROWS,
    fact_rows: int = FACT_TABLE_ROWS,
) -> dict[str, int]:
    """Primary-population row counts per table, derived from the FK graph only.

    The ONE place row counts are chosen for a pool that ships no rows: a child of
    some relationship is a fact, everything else a dimension. A table keyed
    ENTIRELY by FKs is capped at FK_IDENTITY_CAPACITY_SAFETY of its pool product.
    """
    children = {r.child_table for r in relationships}
    hint = {
        t.name: (fact_rows if t.name in children else dimension_rows)
        for t in sorted(tables, key=lambda t: t.name)
    }
    by_name = {t.name: t for t in tables}
    for name in _topological_tables(by_name, list(relationships)):
        tspec = by_name[name]
        rels = [r for r in relationships if r.child_table == name]
        fk_cols = {c for r in rels for c in r.child_columns}
        for group in (tspec.primary_key, tspec.business_key):
            if not group or not set(group) <= fk_cols:
                continue
            capacity = 1
            for rel in rels:
                if set(rel.child_columns) & set(group):
                    capacity *= max(1, hint.get(rel.parent_table, 1))
            hint[name] = min(hint[name], int(FK_IDENTITY_CAPACITY_SAFETY * capacity))
    return hint


#: Per-shape minting offset (see `_shape_mint_offset`): shifts every index-driven
#: literal so two shapes' rows share a table without key collisions. 0 by default.
_MINT_INDEX_OFFSET = 0

#: Index stride between shapes — far above any witness build's row count.
_MINT_OFFSET_STRIDE = 500


class _shape_mint_offset:
    """Deterministic per-shape minting namespace (context manager).

    Module state because `_mint` has dozens of call sites; the offset comes from
    the shape's POSITION, never a clock or RNG, so determinism is untouched.
    """

    def __init__(self, offset: int) -> None:
        self._offset = offset

    def __enter__(self) -> None:
        global _MINT_INDEX_OFFSET
        self._prior = _MINT_INDEX_OFFSET
        _MINT_INDEX_OFFSET = self._offset

    def __exit__(self, *exc) -> None:
        global _MINT_INDEX_OFFSET
        _MINT_INDEX_OFFSET = self._prior


def _mint(col: ColumnSpec, table: str, index: int) -> object:
    """One deterministic literal value for a constructed counterfactual row.

    Index-driven, never random: two runs must produce byte-identical rows.
    """
    index = index + _MINT_INDEX_OFFSET
    n = COUNTERFACTUAL_ID_BASE + index
    if col.enum_values:
        return col.enum_values[index % len(col.enum_values)]
    if col.type in (ColumnType.INTEGER, ColumnType.BIGINT):
        return n
    if col.type in (ColumnType.FLOAT, ColumnType.DECIMAL):
        return float(n)
    if col.type is ColumnType.BOOLEAN:
        return index % 2 == 0
    if col.type is ColumnType.DATE:
        return (_CF_EPOCH_DATE + timedelta(days=index)).isoformat()
    if col.type is ColumnType.TIMESTAMP:
        # DAYS, not seconds: without advancing the calendar day every row lands
        # on the epoch and a mart grained on the DATE PART of a timestamp
        # collapses to ONE gold row, which is guessable.
        return (_CF_EPOCH_DATETIME + timedelta(days=index)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    if col.type is ColumnType.JSON:
        return '{"value":' + str(n) + "}"
    return f"{table}_{col.name}_{n}"


def _mint_row(table: TableSpec, index: int) -> dict:
    return {c.name: _mint(c, table.name, index) for c in table.columns}


_MINT_NUMERIC = (
    ColumnType.INTEGER,
    ColumnType.BIGINT,
    ColumnType.FLOAT,
    ColumnType.DECIMAL,
)

_TEMPORAL_TYPES = (ColumnType.DATE, ColumnType.TIMESTAMP)


def _same_type_class(left: ColumnType, right: ColumnType) -> bool:
    """May a value be copied between columns of these two declared types?

    Same type, both numeric, or both temporal — nothing else.
    """
    if left is right:
        return True
    if left in _MINT_NUMERIC and right in _MINT_NUMERIC:
        return True
    return left in _TEMPORAL_TYPES and right in _TEMPORAL_TYPES


def _typed_link_pairs(
    parent: TableSpec, fact: TableSpec, shape: StarShape
) -> tuple[tuple[str, str], ...]:
    """(fact link column, parent key) pairs a value can legally be copied across.

    Types are already narrowed, so whatever still disagrees disagrees for real:
    the rule is SAME TYPE CLASS, and being laxer puts a text token in an integer
    column. A dropped pair is closed afterwards by the declared relationship.
    """
    parent_columns = {c.name: c for c in parent.columns}
    fact_columns = {c.name: c for c in fact.columns}
    pairs: list[tuple[str, str]] = []
    for child_col, parent_col in zip(shape.fact_link_columns, shape.parent_keys):
        pspec, cspec = parent_columns.get(parent_col), fact_columns.get(child_col)
        if pspec is not None and cspec is not None:
            if not _same_type_class(pspec.type, cspec.type):
                continue
        pairs.append((child_col, parent_col))
    return tuple(pairs)


def _column_is_temporal(table: TableSpec | None, column: str) -> bool:
    if table is None:
        return False
    for c in table.columns:
        if c.name == column:
            return c.type in _TEMPORAL_TYPES
    return False


def _entity_anchor(
    by_name: dict[str, TableSpec],
    shape: StarShape,
    anchor_table: str,
    anchor_key: str,
    bridge_table: str,
    bridge_fk: str,
) -> tuple[str, str]:
    """(anchor key, bridge FK) whose identity column is NOT temporal.

    Witness rows copy the anchor key into the bridge FK, so a temporal key would
    write a DATE into whatever column that FK is; `parent_keys` and
    `fact_link_columns` are parallel, so the first non-temporal index is still a
    real FK. Only the FALLBACK is repaired — an explicit anchor is left alone.
    """
    if shape.witness_anchor_key:
        return anchor_key, bridge_fk
    anchor, bridge = by_name.get(anchor_table), by_name.get(bridge_table)
    if not _column_is_temporal(anchor, anchor_key):
        return anchor_key, bridge_fk
    bridge_columns = {c.name for c in bridge.columns} if bridge else set()
    for key, link in zip(shape.parent_keys, shape.fact_link_columns):
        if not _column_is_temporal(anchor, key) and link in bridge_columns:
            return key, link
    return anchor_key, bridge_fk


def assert_literal_rows_typed(
    tables: tuple[TableSpec, ...], rows: dict[str, tuple[Row, ...]]
) -> None:
    """No constructed literal value may violate its column's declared type.

    Raises: turns a corruption that would otherwise surface as a DuckDB
    conversion error at reference-run into an ingest-time refusal.
    """
    by_name = {t.name: t for t in tables}
    problems: list[str] = []
    for table_name, table_rows in sorted(rows.items()):
        table = by_name.get(table_name)
        if table is None:
            continue
        types = {c.name: c.type for c in table.columns}
        for index, row in enumerate(table_rows):
            for column, value in sorted(row.items()):
                declared = types.get(column)
                if declared is None or value is None:
                    continue
                if declared in _MINT_NUMERIC and isinstance(value, str):
                    problems.append(
                        f"{table_name}[{index}].{column} declares "
                        f"{declared.value} but carries {value!r}"
                    )
                elif declared in _TEMPORAL_TYPES and not isinstance(value, str):
                    problems.append(
                        f"{table_name}[{index}].{column} declares "
                        f"{declared.value} but carries {value!r}"
                    )
    if problems:
        raise ValueError(
            "constructed counterfactual violates declared column types: "
            + "; ".join(problems[:6])
            + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else "")
        )


def _link_is_closable(rel: Relationship, by_name: dict[str, TableSpec]) -> bool:
    """Can a value be copied across this link without violating a declared type?

    Closure copies one endpoint into the other, so a TEMPORAL-with-NUMERIC link
    cannot be closed either way. TEXT paired with anything stays closable —
    `_narrow_for_minting` reconciles it against its typed peer first.
    """
    parent, child = by_name.get(rel.parent_table), by_name.get(rel.child_table)
    if parent is None or child is None:
        return False
    parent_columns = {c.name: c for c in parent.columns}
    child_columns = {c.name: c for c in child.columns}
    for pcol, ccol in zip(rel.parent_columns, rel.child_columns):
        pspec, cspec = parent_columns.get(pcol), child_columns.get(ccol)
        if pspec is None or cspec is None:
            continue
        if not _same_type_class(pspec.type, cspec.type):
            return False
    return True


def _narrow_for_minting(
    tables: tuple[TableSpec, ...], relationships: tuple[Relationship, ...]
) -> tuple[TableSpec, ...]:
    """A parent column mints under the NARROWEST contract its children declare.

    `_close_foreign_keys` copies a parent's minted value down every FK edge, so
    without this a value only the parent's own spec allows lands in a child that
    forbids it. MINTING ONLY — the shipped TableSpec is untouched.
    """
    by_name = {t.name: t for t in tables}
    kids: dict[tuple[str, str], list[ColumnSpec]] = {}
    for rel in relationships:
        parent, child = by_name.get(rel.parent_table), by_name.get(rel.child_table)
        if parent is None or child is None:
            continue
        parent_columns = {c.name: c for c in parent.columns}
        child_columns = {c.name: c for c in child.columns}
        for pcol, ccol in zip(rel.parent_columns, rel.child_columns):
            # BOTH DIRECTIONS: values travel either way along an edge, so the
            # two endpoints must mint under one shared contract.
            pspec, cspec = parent_columns.get(pcol), child_columns.get(ccol)
            if cspec is not None:
                kids.setdefault((parent.name, pcol), []).append(cspec)
            if pspec is not None:
                kids.setdefault((child.name, ccol), []).append(pspec)
    if not kids:
        return tables

    out: list[TableSpec] = []
    for table in tables:
        columns: list[ColumnSpec] = []
        changed = False
        for col in table.columns:
            enum, ctype = col.enum_values, col.type
            for kid in kids.get((table.name, col.name), ()):
                if not enum and kid.enum_values:
                    enum = kid.enum_values
                if ctype is ColumnType.TEXT and kid.type in _MINT_NUMERIC:
                    ctype = kid.type
            if enum != col.enum_values or ctype is not col.type:
                columns.append(
                    col.model_copy(update={"enum_values": enum, "type": ctype})
                )
                changed = True
            else:
                columns.append(col)
        out.append(
            table.model_copy(update={"columns": tuple(columns)}) if changed else table
        )
    return tuple(out)


def _active_all_null_aggregate_witnesses(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
) -> tuple[AllNullAggregateWitness, ...]:
    """Schema-certified recovered aggregate witnesses, consolidated by group.

    The adapter records exact source bindings before final ``TableSpec``
    nullability exists. Activate a measure only after every bound input is
    proven nullable. A required FK cannot be nulled, and a measure input cannot
    also be a direct group key. Compatible measures share one source row so the
    counterfactual stays compact.
    """

    by_name = {table.name: table for table in tables}
    required_child_columns = {
        (relationship.child_table, column)
        for relationship in relationships
        if relationship.required
        for column in relationship.child_columns
    }
    grouped: dict[
        tuple[str, tuple[str, ...]],
        tuple[list[str], list[str]],
    ] = {}
    for witness in shape.all_null_aggregate_witnesses:
        if witness.source_table != shape.parent:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} declares an all-NULL "
                f"aggregate witness on {witness.source_table!r}, but recovered "
                f"base-table construction writes {shape.parent!r}"
            )
        table = by_name.get(witness.source_table)
        if table is None:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} all-NULL witness names "
                f"unknown source table {witness.source_table!r}"
            )
        declared = {column.name: column for column in table.columns}
        missing = sorted(
            (set(witness.input_columns) | set(witness.direct_group_columns))
            - set(declared)
        )
        if missing:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} all-NULL witness names "
                f"unknown {table.name} columns {missing}"
            )
        overlap = sorted(
            set(witness.input_columns) & set(witness.direct_group_columns)
        )
        if overlap:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} cannot NULL aggregate "
                f"input(s) {overlap}; they also identify the output group"
            )
        if any(declared[column].nullable for column in witness.direct_group_columns):
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} all-NULL witness has a "
                "nullable direct group key; it cannot certify an isolated group"
            )
        if not all(declared[column].nullable for column in witness.input_columns):
            # Schema-conditional evidence: a recovered expression can be
            # null-preserving while this particular generated source forbids
            # the all-missing state. In that case there is no boundary to add.
            continue
        if any(
            (table.name, column) in required_child_columns
            for column in witness.input_columns
        ):
            continue
        key = (witness.source_table, witness.direct_group_columns)
        inputs, measures = grouped.setdefault(key, ([], []))
        for column in witness.input_columns:
            if column not in inputs:
                inputs.append(column)
        for column in witness.measure_columns:
            if column not in measures:
                measures.append(column)

    return tuple(
        AllNullAggregateWitness(
            source_table=table,
            input_columns=tuple(inputs),
            measure_columns=tuple(measures),
            direct_group_columns=group_columns,
        )
        for (table, group_columns), (inputs, measures) in sorted(grouped.items())
    )


def _round_before_sum_outputs(
    value: int | float,
    witness: RoundBeforeSumWitness,
) -> tuple[Decimal, Decimal] | None:
    """Return (per-row-then-sum, sum-then-round) for a safe planted value.

    Values are deliberately admitted only well inside the rounding interval,
    never on a half-way boundary.  That margin makes the proof stable when the
    canonical DuckDB runtime stores FLOAT/DECIMAL inputs as doubles.
    """

    quantum = Decimal(1).scaleb(-witness.decimal_places)
    normalized = Decimal(str(value)) / witness.divisor
    position = normalized / quantum
    if not Decimal("0.55") <= position <= Decimal("0.70"):
        return None
    per_row = normalized.quantize(quantum, rounding=ROUND_HALF_UP)
    correct = per_row * 2
    wrong = (normalized * 2).quantize(quantum, rounding=ROUND_HALF_UP)
    return (correct, wrong) if correct != wrong else None


def _round_before_sum_input_value(
    column: ColumnSpec,
    witness: RoundBeforeSumWitness,
) -> int | float | None:
    """A deterministic, type-safe value whose two copies prove order matters."""

    # Keep double conversion and integer coercion comfortably finite.  This is
    # a witness admission rule, so declining an exotic literal is preferable
    # to publishing a numerically fragile claim.
    if not Decimal("1e-12") <= witness.divisor <= Decimal("1e15"):
        return None
    quantum = Decimal(1).scaleb(-witness.decimal_places)
    ideal = witness.divisor * quantum * Decimal("0.6")
    candidates: tuple[int | float, ...]
    if column.type in (ColumnType.INTEGER, ColumnType.BIGINT):
        floor = int(ideal.to_integral_value(rounding=ROUND_FLOOR))
        ceiling = int(ideal.to_integral_value(rounding=ROUND_CEILING))
        limit = (
            2_000_000_000
            if column.type is ColumnType.INTEGER
            else 9_000_000_000_000_000_000
        )
        candidates = tuple(
            value
            for value in dict.fromkeys((floor, ceiling))
            if 0 < value <= limit
        )
    elif column.type in (ColumnType.FLOAT, ColumnType.DECIMAL):
        value = float(ideal)
        candidates = (value,) if math.isfinite(value) and value > 0 else ()
    else:
        return None
    return next(
        (
            value
            for value in candidates
            if _round_before_sum_outputs(value, witness) is not None
        ),
        None,
    )


def _active_round_before_sum_witnesses(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
) -> tuple[RoundBeforeSumWitness, ...]:
    """Constraint-certified rounding-order witnesses, consolidated by input."""

    by_name = {table.name: table for table in tables}
    relationship_columns = {
        (relationship.child_table, column)
        for relationship in relationships
        for column in relationship.child_columns
    } | {
        (relationship.parent_table, column)
        for relationship in relationships
        for column in relationship.parent_columns
    }
    grouped: dict[
        tuple[str, str, tuple[str, ...], Decimal, int],
        list[str],
    ] = {}
    for witness in shape.round_before_sum_witnesses:
        if witness.source_table != shape.parent:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} declares a round-before-"
                f"sum witness on {witness.source_table!r}, but recovered base-"
                f"table construction writes {shape.parent!r}"
            )
        table = by_name.get(witness.source_table)
        if table is None:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} round-before-sum witness "
                f"names unknown source table {witness.source_table!r}"
            )
        declared = {column.name: column for column in table.columns}
        missing = sorted(
            ({witness.input_column} | set(witness.direct_group_columns))
            - set(declared)
        )
        if missing:
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} round-before-sum witness "
                f"names unknown {table.name} columns {missing}"
            )
        if witness.input_column in set(witness.direct_group_columns):
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} cannot vary rounding "
                f"input {witness.input_column!r}; it also identifies the group"
            )
        # The proof needs two rows with the same group.  A declared identity
        # forbids the byte-identical pair, and changing it could silently widen
        # a derived GROUP BY.  Decline instead of guessing a harmless column.
        if table.primary_key or table.business_key:
            continue
        if any(declared[column].nullable for column in witness.direct_group_columns):
            continue
        input_spec = declared[witness.input_column]
        if input_spec.enum_values or (
            table.name,
            witness.input_column,
        ) in relationship_columns:
            continue
        if _round_before_sum_input_value(input_spec, witness) is None:
            continue
        key = (
            witness.source_table,
            witness.input_column,
            witness.direct_group_columns,
            witness.divisor,
            witness.decimal_places,
        )
        measures = grouped.setdefault(key, [])
        for measure in witness.measure_columns:
            if measure not in measures:
                measures.append(measure)

    return tuple(
        RoundBeforeSumWitness(
            source_table=table,
            input_column=input_column,
            measure_columns=tuple(measures),
            direct_group_columns=group_columns,
            divisor=divisor,
            decimal_places=places,
        )
        for (
            table,
            input_column,
            group_columns,
            divisor,
            places,
        ), measures in sorted(grouped.items(), key=lambda item: repr(item[0]))
    )


def _append_round_before_sum_rows(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
    rows: dict[str, list[dict]],
) -> tuple[tuple[dict, dict, RoundBeforeSumWitness], ...]:
    """Append exactly two real, same-group rows for each active witness."""

    by_name = {table.name: table for table in tables}
    targets: list[tuple[dict, dict, RoundBeforeSumWitness]] = []
    next_index = _ROUND_BEFORE_SUM_MINT_BASE
    for witness in _active_round_before_sum_witnesses(
        tables, relationships, shape
    ):
        table = by_name[witness.source_table]
        value = _round_before_sum_input_value(
            table.column(witness.input_column), witness
        )
        if value is None:  # pragma: no cover - active admission proved it
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} lost its certified "
                "round-before-sum numeric value during construction"
            )
        existing = rows.setdefault(table.name, [])
        seen = {
            tuple(row.get(column) for column in witness.direct_group_columns)
            for row in existing
        }
        first = None
        while next_index < _ROUND_BEFORE_SUM_MINT_LIMIT:
            candidate = _mint_row(table, next_index)
            next_index += 1
            group = tuple(
                candidate.get(column) for column in witness.direct_group_columns
            )
            if not any(item is None for item in group) and group not in seen:
                first = candidate
                break
        if first is None:
            # The direct group has a finite, exhausted domain.  No condition is
            # advertised unless a dedicated two-row group actually exists.
            continue
        first[witness.input_column] = value
        second = dict(first)
        existing.extend((first, second))
        targets.append((first, second, witness))
    return tuple(targets)


def _append_all_null_aggregate_rows(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
    rows: dict[str, list[dict]],
) -> tuple[tuple[dict, AllNullAggregateWitness], ...]:
    """Append one isolated real source row per compatible witness group."""

    by_name = {table.name: table for table in tables}
    targets: list[tuple[dict, AllNullAggregateWitness]] = []
    for ordinal, witness in enumerate(
        _active_all_null_aggregate_witnesses(tables, relationships, shape)
    ):
        table = by_name[witness.source_table]
        existing = rows.setdefault(table.name, [])
        seen = {
            tuple(row.get(column) for column in witness.direct_group_columns)
            for row in existing
        }
        target = None
        start = _ALL_NULL_MINT_BASE + ordinal * _ALL_NULL_MINT_ATTEMPTS
        for index in range(start, start + _ALL_NULL_MINT_ATTEMPTS):
            candidate = _mint_row(table, index)
            group = tuple(
                candidate.get(column) for column in witness.direct_group_columns
            )
            if not any(value is None for value in group) and group not in seen:
                target = candidate
                break
        if target is None:
            # A finite direct-key domain can be exhausted by the ordinary
            # witnesses. Do not advertise an all-NULL output group unless a
            # distinct one can actually be constructed.
            continue
        existing.append(target)
        targets.append((target, witness))
    return tuple(targets)


def _prepare_isolated_group_links(
    by_name: dict[str, TableSpec],
    relationships: tuple[Relationship, ...],
    rows: dict[str, list[dict]],
    targets: Sequence[tuple[dict, str, tuple[str, ...]]],
) -> dict[int, frozenset[str]]:
    """Keep dedicated direct-group tuples stable while closing their FKs.

    A recovered rollup commonly groups by a foreign key. Ordinary closure
    repoints every child at the first parent and would merge the dedicated
    all-NULL or rounding-order rows into an ordinary group. Ensure each minted
    key has a real parent, then pin those relationship columns so closure keeps
    the matching parent. Callers separately exclude these rows from optional-
    orphan selection, leaving that discriminator on an ordinary row.
    """

    pinned: dict[int, set[str]] = {}
    added = 0
    for target, source_table, direct_group_columns in targets:
        direct = set(direct_group_columns)
        for relationship in relationships:
            if (
                relationship.child_table != source_table
                or not direct.intersection(relationship.child_columns)
            ):
                continue
            key = tuple(target.get(column) for column in relationship.child_columns)
            if any(value is None for value in key):
                continue
            parent_rows = rows[relationship.parent_table]
            present = {
                tuple(row.get(column) for column in relationship.parent_columns)
                for row in parent_rows
            }
            if key not in present:
                parent = by_name[relationship.parent_table]
                minted = _mint_row(
                    parent, _ALL_NULL_PARENT_MINT_BASE + added
                )
                added += 1
                for child_column, parent_column in zip(
                    relationship.child_columns,
                    relationship.parent_columns,
                    strict=True,
                ):
                    minted[parent_column] = target[child_column]
                parent_rows.append(minted)
            pinned.setdefault(id(target), set()).update(
                relationship.child_columns
            )
    return {
        row_id: frozenset(columns) for row_id, columns in pinned.items()
    }


def _all_null_aggregate_witness_present(
    witness: AllNullAggregateWitness,
    rows: Mapping[str, Sequence[Row]],
) -> bool:
    """Does the final literal data contain the exact advertised boundary?"""

    source_rows = rows.get(witness.source_table, ())
    groups: dict[tuple[object, ...], int] = {}
    for row in source_rows:
        key = tuple(row.get(column) for column in witness.direct_group_columns)
        groups[key] = groups.get(key, 0) + 1
    return any(
        groups.get(
            tuple(row.get(column) for column in witness.direct_group_columns)
        )
        == 1
        and not any(
            row.get(column) is not None for column in witness.input_columns
        )
        for row in source_rows
    )


def _all_null_aggregate_conditions(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shapes: tuple[StarShape, ...],
    rows: Mapping[str, Sequence[Row]],
) -> tuple[str, ...]:
    """Public claims for only the all-NULL groups proven in final rows."""

    lines: list[str] = []
    for shape in shapes:
        for witness in _active_all_null_aggregate_witnesses(
            tables, relationships, shape
        ):
            if not _all_null_aggregate_witness_present(witness, rows):
                continue
            line = (
                f"ALL-NULL AGGREGATE WITNESS for mart {shape.mart}: in "
                f"{witness.source_table}, the direct group-key tuple "
                f"({', '.join(witness.direct_group_columns)}) appears on exactly "
                f"one real source row, and that row has NULL in every input "
                f"column ({', '.join(witness.input_columns)}). Therefore every "
                f"contributing input is missing for undefaulted measure(s) "
                f"{', '.join(witness.measure_columns)}; the result of each is "
                "NULL, not 0."
            )
            if line not in lines:
                lines.append(line)
    return tuple(lines)


def _numeric_literal_matches(value: object, expected: int | float) -> bool:
    try:
        return Decimal(str(value)) == Decimal(str(expected))
    except Exception:  # noqa: BLE001 — malformed final data is not a witness
        return False


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _round_before_sum_conditions(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shapes: tuple[StarShape, ...],
    rows: Mapping[str, Sequence[Row]],
) -> tuple[str, ...]:
    """Advertise only rounding-order proofs present in the final literals."""

    by_name = {table.name: table for table in tables}
    lines: list[str] = []
    for shape in shapes:
        for witness in _active_round_before_sum_witnesses(
            tables, relationships, shape
        ):
            table = by_name[witness.source_table]
            value = _round_before_sum_input_value(
                table.column(witness.input_column), witness
            )
            if value is None:
                continue
            grouped: dict[tuple[object, ...], list[Row]] = {}
            for row in rows.get(witness.source_table, ()):
                key = tuple(
                    row.get(column) for column in witness.direct_group_columns
                )
                grouped.setdefault(key, []).append(row)
            observed = next(
                (
                    (key, group_rows)
                    for key, group_rows in grouped.items()
                    if len(group_rows) == 2
                    and group_rows[0] == group_rows[1]
                    and all(
                        _numeric_literal_matches(
                            row.get(witness.input_column), value
                        )
                        for row in group_rows
                    )
                ),
                None,
            )
            outputs = _round_before_sum_outputs(value, witness)
            if observed is None or outputs is None:
                continue
            key, _group_rows = observed
            correct, wrong = outputs
            group_text = ", ".join(
                f"{column}={item!r}"
                for column, item in zip(
                    witness.direct_group_columns, key, strict=True
                )
            )
            line = (
                f"ROUND-BEFORE-SUM WITNESS for mart {shape.mart}: exactly two "
                f"real {witness.source_table} rows share direct group-key "
                f"values ({group_text}); {witness.input_column} is {value!r} "
                "on each row. The two source rows are byte-identical, so all "
                "derived and joined grouping values are identical too. For "
                f"measure(s) "
                f"{', '.join(witness.measure_columns)}, "
                f"SUM(ROUND({witness.input_column} / "
                f"{_decimal_text(witness.divisor)}, "
                f"{witness.decimal_places})) therefore yields "
                f"{_decimal_text(correct)}, while the wrong ordering "
                f"ROUND(SUM({witness.input_column}) / "
                f"{_decimal_text(witness.divisor)}, "
                f"{witness.decimal_places}) yields {_decimal_text(wrong)}."
            )
            if line not in lines:
                lines.append(line)
    return tuple(lines)


def counterfactual_literal_rows(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
) -> dict[str, tuple[Row, ...]]:
    """Construct the counterfactual rows a pool with NO oracle can still derive.

    Row A is the control, row B a parent with NO fact rows (LEFT vs INNER, null
    defaults), row C a parent whose fact rows are BYTE-IDENTICAL duplicates
    (dedupe; only when the fact declares no PK). Every other table gets a minted
    row and every FK is closed, so nothing renders empty.
    """
    tables = _narrow_for_minting(tables, relationships)
    if shape.witnesses:
        return witness_literal_rows(tables, relationships, shape)

    by_name = {t.name: t for t in tables}
    if shape.parent not in by_name:
        raise ValueError(f"counterfactual: parent table {shape.parent!r} is not a task table")

    rows: dict[str, list[dict]] = {}
    parent = by_name[shape.parent]
    rows[parent.name] = [_mint_row(parent, i) for i in range(_PARENT_ROWS)]
    all_null_targets = _append_all_null_aggregate_rows(
        tables, relationships, shape, rows
    )
    round_before_sum_targets = _append_round_before_sum_rows(
        tables, relationships, shape, rows
    )

    fact = by_name.get(shape.fact) if shape.fact else None
    duplicate_group: list[dict] = []
    duplicate_table = ""
    if fact is not None and shape.fact_link_columns:
        matched = [_mint_row(fact, i) for i in range(_FACT_ROWS_MATCHED)]
        if shape.fact_dedupe:
            duplicated_row = _mint_row(fact, _FACT_ROWS_MATCHED)
            duplicated = [dict(duplicated_row) for _ in range(_FACT_ROWS_DUPLICATED)]
            duplicate_group = duplicated
            duplicate_table = parent.name if fact.name == parent.name else fact.name
        else:
            duplicated = [
                _mint_row(fact, _FACT_ROWS_MATCHED + i)
                for i in range(_FACT_ROWS_DUPLICATED)
            ]
        fact_rows = matched + duplicated
        # Row A (index 0) gets the distinct children, row C (index 2) the
        # duplicates, row B (index 1) none. TYPE-COMPATIBLE PAIRS ONLY — a
        # skipped pair is closed below by the DECLARED relationship.
        link_pairs = _typed_link_pairs(parent, fact, shape)
        for row in matched:
            for child_col, parent_col in link_pairs:
                row[child_col] = rows[parent.name][0][parent_col]
        for row in duplicated:
            for child_col, parent_col in link_pairs:
                row[child_col] = rows[parent.name][2][parent_col]
        rows.setdefault(fact.name, [])
        if fact.name == parent.name:  # self-referencing star: keep one table
            rows[parent.name].extend(fact_rows)
        else:
            rows[fact.name] = fact_rows

    for table in tables:
        rows.setdefault(table.name, [_mint_row(table, 0)])

    optional_owner_orphan = _append_optional_owner_orphan(
        by_name, relationships, rows, shape
    )

    isolated_group_pins = _prepare_isolated_group_links(
        by_name,
        relationships,
        rows,
        tuple(
            (row, witness.source_table, witness.direct_group_columns)
            for row, witness in all_null_targets
        )
        + tuple(
            (row, witness.source_table, witness.direct_group_columns)
            for first, second, witness in round_before_sum_targets
            for row in (first, second)
        ),
    )

    _close_foreign_keys(
        by_name,
        relationships,
        rows,
        # The star's own link carries the row A/B/C construction; closing it
        # would repoint every fact row at row A and lose the childless parent.
        skip=lambda r: (
            fact is not None
            and r.child_table == fact.name
            and r.parent_table == parent.name
            and set(r.child_columns) == set(shape.fact_link_columns)
        ),
        preserve=frozenset(
            {id(r) for r in duplicate_group}
            | {id(row) for row, _witness in all_null_targets}
            | {
                id(row)
                for first, second, _witness in round_before_sum_targets
                for row in (first, second)
            }
            | (
                {id(optional_owner_orphan[1])}
                if optional_owner_orphan is not None
                else set()
            )
        ),
        optional_dangling_edges=shape.join_edges,
        pinned_columns=isolated_group_pins,
    )
    _enforce_primary_keys(by_name, rows)
    _verify_optional_owner_orphan(optional_owner_orphan, rows, shape)
    for target, witness in all_null_targets:
        if not any(target is row for row in rows.get(witness.source_table, ())):
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} all-NULL aggregate witness "
                "was lost during primary-key enforcement"
            )
        for column in witness.input_columns:
            target[column] = None
    for first, second, witness in round_before_sum_targets:
        surviving = rows.get(witness.source_table, ())
        if not all(any(target is row for row in surviving) for target in (first, second)):
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} round-before-sum witness "
                "lost one of its two source rows during primary-key enforcement"
            )
        group = tuple(
            first.get(column) for column in witness.direct_group_columns
        )
        grouped = [
            row
            for row in surviving
            if tuple(row.get(column) for column in witness.direct_group_columns)
            == group
        ]
        value = _round_before_sum_input_value(
            by_name[witness.source_table].column(witness.input_column), witness
        )
        if (
            len(grouped) != 2
            or (len(grouped) == 2 and grouped[0] != grouped[1])
            or value is None
            or any(
                not _numeric_literal_matches(
                    row.get(witness.input_column), value
                )
                for row in grouped
            )
            or _round_before_sum_outputs(value, witness) is None
        ):
            raise ValueError(
                f"counterfactual: mart {shape.mart!r} round-before-sum witness "
                "did not survive as exactly two same-group rows with the "
                "certified numeric input"
            )
    _verify_duplicate_witness(rows, duplicate_table, duplicate_group)
    return {name: tuple(rows[name]) for name in sorted(rows)}


def _shape_core_tables(shape: StarShape) -> frozenset[str]:
    """The tables a shape's witness construction actually populates."""
    return frozenset(t for t in (shape.parent, shape.fact, shape.child) if t)


def counterfactual_rows_for_shapes(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shapes: tuple[StarShape, ...],
) -> dict[str, tuple[Row, ...]]:
    """Witness rows for EVERY mart's star, as the UNION of the standalone builds.

    Each shape builds standalone in its own minting namespace and the merge only
    unions — FK resolution is MONOTONE under adding parent rows, so the union is
    closed by construction. Core rows come FIRST so a PK collision costs a
    filler, never a witness; a new unresolved key raises CounterfactualMergeError.
    """
    if not shapes:
        raise ValueError("counterfactual: no shapes to build witnesses for")
    if len(shapes) == 1:
        built = counterfactual_literal_rows(tables, relationships, shapes[0])
        assert_literal_rows_typed(tables, built)
        return built

    builds: list[dict[str, tuple[Row, ...]]] = []
    cores: list[frozenset[str]] = []
    for index, shape in enumerate(shapes):
        with _shape_mint_offset(index * _MINT_OFFSET_STRIDE):
            builds.append(counterfactual_literal_rows(tables, relationships, shape))
        cores.append(_shape_core_tables(shape))

    merged: dict[str, list[dict]] = {}
    for name in sorted(t.name for t in tables):
        core_rows = [
            dict(row)
            for build, core in zip(builds, cores, strict=True)
            if name in core
            for row in build.get(name, ())
        ]
        filler_rows = [
            dict(row)
            for build, core in zip(builds, cores, strict=True)
            if name not in core
            for row in build.get(name, ())
        ]
        merged[name] = core_rows + filler_rows

    by_name = {t.name: t for t in tables}
    _enforce_primary_keys(by_name, merged)

    allowed: set[tuple] = set()
    for build in builds:
        allowed.update(_unresolved_keys(relationships, build))
    introduced = sorted(_unresolved_keys(relationships, merged) - allowed, key=repr)
    if introduced:
        child, parent, columns, key = introduced[0]
        raise CounterfactualMergeError(
            f"counterfactual merge over marts {[s.mart for s in shapes]} introduced "
            f"{len(introduced)} unresolved key(s) no standalone build had, e.g. "
            f"{child}.{list(columns)} = {key} -> {parent} — the union of closed "
            "builds must stay closed; refusing to freeze the counterfactual"
        )
    built = {name: tuple(rows) for name, rows in sorted(merged.items())}
    assert_literal_rows_typed(tables, built)
    return built


def _unresolved_keys(
    relationships: tuple[Relationship, ...], rows: Mapping[str, Sequence[Row]]
) -> set[tuple[str, str, tuple[str, ...], tuple]]:
    """(child, parent, child columns, key) for every non-NULL child key matching
    no parent row — the closure witness the multi-shape merge compares."""
    unresolved: set[tuple[str, str, tuple[str, ...], tuple]] = set()
    for rel in relationships:
        parents = {
            tuple(r.get(c) for c in rel.parent_columns)
            for r in rows.get(rel.parent_table, ())
        }
        for row in rows.get(rel.child_table, ()):
            key = tuple(row.get(c) for c in rel.child_columns)
            if any(v is None for v in key):
                continue
            if key not in parents:
                unresolved.add((rel.child_table, rel.parent_table, tuple(rel.child_columns), key))
    return unresolved


def _edge_has_unmatched_left_row(
    edge: tuple[str, tuple[str, ...], str, tuple[str, ...]],
    rows: Mapping[str, Sequence[Row]],
) -> bool:
    """Whether one literal left row fails to match the recorded join edge.

    This is the data proof a recovered LEFT join needs before the attack
    catalogue may claim ``inner_join``. SQL NULL equality never matches, even
    when a right-side row also contains NULL, so a NULL component is sufficient.
    """

    left_table, left_columns, right_table, right_columns = edge
    if (
        not left_columns
        or len(left_columns) != len(right_columns)
        or left_table not in rows
        or right_table not in rows
    ):
        return False
    right_keys = {
        tuple(row.get(column) for column in right_columns)
        for row in rows[right_table]
    }
    for row in rows[left_table]:
        key = tuple(row.get(column) for column in left_columns)
        if any(value is None for value in key) or key not in right_keys:
            return True
    return False


def _join_edge_relationship_keys(
    join_edges: Iterable[
        tuple[str, tuple[str, ...], str, tuple[str, ...]]
    ],
) -> frozenset[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    """Relationship identities for recovered child-to-parent plan edges.

    ``StarShape.join_edges`` records the emitted join orientation as
    ``(left table, left columns, right table, right columns)``.  A deliberate
    non-NULL orphan is useful only when that exact left edge is the optional FK
    whose LEFT-to-INNER mutation the recovered mart exercises.  Reversed edges
    intentionally do not match: breaking a child FK cannot make a left-side
    parent row unmatched.
    """

    return frozenset(
        (left_table, right_table, tuple(left_columns), tuple(right_columns))
        for left_table, left_columns, right_table, right_columns in join_edges
    )


def _optional_dangling_conditions(
    relationships: tuple[Relationship, ...],
    rows: Mapping[str, Sequence[Row]],
    join_edges: Iterable[
        tuple[str, tuple[str, ...], str, tuple[str, ...]]
    ] = (),
    owner_relationships: Iterable[Relationship] = (),
) -> tuple[str, ...]:
    """Describe only realized optional orphans on admitted mart edges."""

    eligible = _join_edge_relationship_keys(join_edges) | frozenset(
        _relationship_key(relationship)
        for relationship in owner_relationships
    )
    lines: list[str] = []
    for rel in sorted(
        (
            relationship
            for relationship in relationships
            if not relationship.required
            and _relationship_key(relationship) in eligible
        ),
        key=_relationship_key,
    ):
        parents = {
            tuple(row.get(column) for column in rel.parent_columns)
            for row in rows.get(rel.parent_table, ())
        }
        dangling = any(
            not any(value is None for value in key) and key not in parents
            for key in (
                tuple(row.get(column) for column in rel.child_columns)
                for row in rows.get(rel.child_table, ())
            )
        )
        if dangling:
            lines.append(
                "OPTIONAL-LINK WITNESS: "
                f"{rel.child_table}.{', '.join(rel.child_columns)} contains a "
                f"non-NULL key with no matching {rel.parent_table}."
                f"{', '.join(rel.parent_columns)} row; the source row remains "
                "valid because this relationship is optional."
            )
    return tuple(lines)


# Constructed witness rows. Every declared wrong implementation must be
# observable; nullable cases activate only for proven-nullable columns.

#: Measure values used by the witness rows: small, distinct, and ordered so the
#: control row's LOW value comes FIRST (a lost argmax ORDER BY then picks wrong).
_W_LOW = 10
_W_HIGH = 40
_W_TIE = 25


def _coerce(col: ColumnSpec, value: object) -> object:
    """Fit a witness value to the column's declared type (fail closed on text)."""
    if value is None:
        return None
    if col.type in (ColumnType.INTEGER, ColumnType.BIGINT):
        return int(value)  # type: ignore[arg-type]
    if col.type in (ColumnType.FLOAT, ColumnType.DECIMAL):
        return float(value)  # type: ignore[arg-type]
    if col.type is ColumnType.DATE:
        return (_CF_EPOCH_DATE + timedelta(days=int(value))).isoformat()  # type: ignore[arg-type]
    if col.type is ColumnType.TIMESTAMP:
        return (_CF_EPOCH_DATETIME + timedelta(days=int(value))).strftime(  # type: ignore[arg-type]
            "%Y-%m-%d %H:%M:%S"
        )
    if col.type is ColumnType.BOOLEAN:
        return bool(value)
    return str(value)


def _control_winner_label_is_nullable(
    tables: tuple[TableSpec, ...],
    shape: StarShape,
    relationships: tuple[Relationship, ...] = (),
) -> bool:
    """Can row A put a real NULL in the controlled argmax label column?

    The label role can point at a child-table label for some shapes; witness
    row A can directly control it only when it is a column of the bridge named
    by ``witness_anchor``.  Nulling any other column would claim a witness the
    constructed row does not actually contain.
    """

    if WITNESS_CONTROL not in shape.witnesses or not shape.roles.label:
        return False
    _anchor, _anchor_key, bridge_name, _bridge_fk = shape.witness_anchor()
    bridge = next((table for table in tables if table.name == bridge_name), None)
    if bridge is None:
        return False
    column = next(
        (column for column in bridge.columns if column.name == shape.roles.label),
        None,
    )
    if column is None or not column.nullable:
        return False
    # A nullable physical column can still participate in a relationship whose
    # task contract requires every child row to resolve.  Such a column cannot
    # legally carry the real NULL used by this witness.
    return not any(
        relationship.required
        and relationship.child_table == bridge_name
        and shape.roles.label in relationship.child_columns
        for relationship in relationships
    )


def _argmax_label_column(
    tables: tuple[TableSpec, ...], shape: StarShape
) -> ColumnSpec | None:
    """Return the exact bridge-side argmax label, never a same-named column."""

    if shape.shape_name != "argmax_profile" or not shape.roles.label:
        return None
    _anchor, _anchor_key, bridge_name, _bridge_fk = shape.witness_anchor()
    bridge = next((table for table in tables if table.name == bridge_name), None)
    if bridge is None:
        return None
    return next(
        (column for column in bridge.columns if column.name == shape.roles.label),
        None,
    )


def _argmax_case_order_labels(column: ColumnSpec | None) -> tuple[str, str] | None:
    """A schema-legal pair whose binary and LOWER orders choose differently.

    The unrestricted pair is deliberately ASCII, so Python's ordering and the
    warehouse's documented plain stored-text ordering agree.  A finite enum is
    searched instead of receiving out-of-domain literals; if its legal values
    cannot express the boundary, no condition is advertised.
    """

    if column is None or column.type is not ColumnType.TEXT:
        return None
    if column.enum_values is None:
        return ("Zeta", "alpha")
    values = sorted(
        {
            value
            for value in column.enum_values
            if value and value.isascii()
        }
    )
    for left, right in product(values, repeat=2):
        if left >= right:
            continue
        lower_winner = min((left, right), key=lambda value: (value.lower(), value))
        if lower_winner != left and left.lower() != right.lower():
            return left, right
    return None


def _argmax_null_order_label(column: ColumnSpec | None) -> str | None:
    """One schema-legal non-NULL label to oppose the NULL ordering arm."""

    if column is None or column.type is not ColumnType.TEXT or not column.nullable:
        return None
    if column.enum_values is None:
        return "kept"
    return next((value for value in column.enum_values if value), None)


def _argmax_null_order_is_legal(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
) -> bool:
    """Whether a real NULL label obeys both column and required-FK contracts."""

    column = _argmax_label_column(tables, shape)
    if _argmax_null_order_label(column) is None:
        return False
    _anchor, _anchor_key, bridge_name, _bridge_fk = shape.witness_anchor()
    return not any(
        relationship.required
        and relationship.child_table == bridge_name
        and shape.roles.label in relationship.child_columns
        for relationship in relationships
    )


def _bridge_measure_is_nullable(
    tables: tuple[TableSpec, ...], shape: StarShape
) -> bool:
    """Can this shape construct the schema-conditional all-NULL measure row?

    ``FactRoles.measure`` is explicitly a bridge-side role.  Resolve it against
    the shape's witness bridge instead of accepting a same-named column from a
    different table.  The new witness is therefore added only when the exact
    source column whose NULL semantics the mart documents is declared nullable.
    """

    if not shape.roles.measure or not shape.roles.link_key:
        return False
    _anchor, _anchor_key, bridge_name, _bridge_fk = shape.witness_anchor()
    bridge = next((table for table in tables if table.name == bridge_name), None)
    if bridge is None:
        return False
    column = next(
        (column for column in bridge.columns if column.name == shape.roles.measure),
        None,
    )
    return bool(column is not None and column.nullable)


def _effective_witnesses(
    tables: tuple[TableSpec, ...],
    shape: StarShape,
    relationships: tuple[Relationship, ...] = (),
) -> tuple[str, ...]:
    """Declared shape witnesses plus schema-driven boundary witnesses.

    Schema-driven witnesses occupy the tail of ``WITNESS_ORDER``. Adding one
    cannot renumber the legacy A--K anchors or move their bridge rows, which
    preserves the deliberately opposed row-A/row-F ordering and every prior
    discriminator.
    """

    enabled = set(shape.witnesses)
    if _bridge_measure_is_nullable(tables, shape):
        enabled.add(WITNESS_NULL_MEASURE)
    label = _argmax_label_column(tables, shape)
    if _argmax_case_order_labels(label) is not None:
        enabled.add(WITNESS_ARGMAX_CASE_ORDER)
    if _argmax_null_order_is_legal(tables, relationships, shape):
        enabled.add(WITNESS_ARGMAX_NULL_ORDER)
    return tuple(witness for witness in WITNESS_ORDER if witness in enabled)


def _rel_between(
    relationships: tuple[Relationship, ...], child: str, parent: str, child_column: str
) -> Relationship | None:
    for rel in relationships:
        if (
            rel.child_table == child
            and rel.parent_table == parent
            and child_column in rel.child_columns
        ):
            return rel
    return None


#: Parent rows created to retain exact argmax-label witness values. Keep them in
#: the tail of each deterministic 500-row shape namespace, away from ordinary
#: anchors/children/bridge rows and the usual identity/dangling repair ranges.
_ARGMAX_LABEL_PARENT_MINT_BASE = 450


def _prepare_argmax_label_links(
    by_name: dict[str, TableSpec],
    relationships: tuple[Relationship, ...],
    rows: dict[str, list[dict]],
    *,
    bridge_table: str,
    label_column: str,
    targets: Sequence[dict],
) -> dict[int, frozenset[str]]:
    """Close and pin relationships that overlap an argmax label witness.

    Ordinary FK closure is intentionally free to repoint a child value at an
    existing parent.  That would erase the exact ``Zeta``/``alpha`` or
    non-NULL/NULL pair these rows exist to test.  For every non-NULL controlled
    tuple, create a matching deterministic parent when necessary; for a legal
    optional NULL tuple, pin the child columns without inventing a NULL parent.
    """

    pinned: dict[int, set[str]] = {}
    added = 0
    for target in targets:
        for relationship in relationships:
            if (
                relationship.child_table != bridge_table
                or label_column not in relationship.child_columns
            ):
                continue
            pinned.setdefault(id(target), set()).update(
                relationship.child_columns
            )
            key = tuple(
                target.get(column) for column in relationship.child_columns
            )
            if any(value is None for value in key):
                # Activation excludes required-label NULLs. For an optional
                # relationship, SQL NULL is itself a legal unresolved key.
                continue
            parent_rows = rows[relationship.parent_table]
            present = {
                tuple(row.get(column) for column in relationship.parent_columns)
                for row in parent_rows
            }
            if key in present:
                continue
            parent = by_name[relationship.parent_table]
            minted = _mint_row(
                parent, _ARGMAX_LABEL_PARENT_MINT_BASE + added
            )
            added += 1
            for child_column, parent_column in zip(
                relationship.child_columns,
                relationship.parent_columns,
                strict=True,
            ):
                minted[parent_column] = target[child_column]
            parent_rows.append(minted)
    return {
        row_id: frozenset(columns) for row_id, columns in pinned.items()
    }


def witness_literal_rows(
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shape: StarShape,
) -> dict[str, tuple[Row, ...]]:
    """Construct one anchor row per declared or schema-activated witness, plus
    its bridge and child rows. Deterministic: index-driven, never random.

    What each witness row means is one line each in `_WITNESS_PROSE`; the write
    ORDER matters as much as the values (rows A and F are deliberate mirrors).
    """
    tables = _narrow_for_minting(tables, relationships)
    by_name = {t.name: t for t in tables}
    anchor_table, anchor_key, bridge_table, bridge_fk = shape.witness_anchor()
    anchor_key, bridge_fk = _entity_anchor(
        by_name, shape, anchor_table, anchor_key, bridge_table, bridge_fk
    )
    roles: FactRoles = shape.roles

    for name, label in (
        (anchor_table, "witness anchor table"),
        (bridge_table, "witness bridge table"),
    ):
        if name and name not in by_name:
            raise ValueError(f"counterfactual: {label} {name!r} is not a task table")
    if not (anchor_table and anchor_key):
        raise ValueError(
            f"counterfactual: shape {shape.mart!r} declares witnesses but names no "
            "anchor table/key — the rows would have nowhere to go"
        )

    anchor = by_name[anchor_table]
    bridge = by_name[bridge_table] if bridge_table else None
    child = by_name[shape.child] if shape.child and shape.child in by_name else None

    ordered = _effective_witnesses(tables, shape, relationships)
    rows: dict[str, list[dict]] = {}

    # -- the anchor rows, one per witness -----------------------------------
    anchor_rows = [_mint_row(anchor, i) for i in range(len(ordered))]
    rows[anchor_table] = anchor_rows
    by_witness = dict(zip(ordered, anchor_rows))

    if WITNESS_OUT_OF_DOMAIN in by_witness and roles.domain_column:
        by_witness[WITNESS_OUT_OF_DOMAIN][roles.domain_column] = roles.out_of_domain
    # Every OTHER anchor row carries a domain value the ladder DOES name, so
    # the out-of-domain row is the only one whose ELSE branch fires.
    if roles.domain_column and roles.domain:
        named = [v for v in roles.domain if v != roles.out_of_domain]
        for witness, row in by_witness.items():
            if witness is not WITNESS_OUT_OF_DOMAIN and named:
                row[roles.domain_column] = named[0]

    # -- the child rows the bridge fans out onto -----------------------------
    child_keys: list[object] = []
    if child is not None and roles.child_primary_key:
        if child.name == anchor_table:
            # `orphan_coverage` fans the bridge back onto the ANCHOR itself, so
            # the child rows already exist — minting more would overwrite them.
            child_keys = [r[roles.child_primary_key] for r in anchor_rows]
        else:
            child_rows = [_mint_row(child, 100 + i) for i in range(2)]
            rows[child.name] = child_rows
            child_keys = [r[roles.child_primary_key] for r in child_rows]

    # -- the bridge rows ------------------------------------------------------
    bridge_rows: list[dict] = []
    duplicate_group: list[dict] = []
    controlled_label_rows: list[dict] = []
    case_order_group: tuple[dict, dict] | None = None
    null_order_group: tuple[dict, dict] | None = None
    distinct_measure_group: tuple[dict, dict] | None = None
    #: The two ordering-axis pairs the wrong_window claim rests on: row A
    #: (winner listed LAST) and row P (winner listed FIRST); both re-verified
    #: after key enforcement and foreign-key closure.
    control_group: tuple[dict, dict] | None = None
    latest_first_group: tuple[dict, dict] | None = None
    counter = 0

    def mint_bridge(
        witness: str,
        *,
        measure: int | None = None,
        status: str | None = None,
        label: str | None = None,
        child_key: object = "__first__",
        period_days: int = 0,
    ) -> dict:
        nonlocal counter
        if bridge is None:  # pragma: no cover - guarded by _witness_problems
            raise ValueError(
                "witness rows need a bridge table; the shape declared witnesses "
                "but joins nothing"
            )
        row = _mint_row(bridge, 200 + counter)
        counter += 1
        # Attach the row to ITS witness's anchor first; an explicit child-key
        # override (witness I's dangling value) may still overwrite it below.
        row[bridge_fk] = by_witness[witness][anchor_key]
        if measure is not None and roles.measure:
            row[roles.measure] = _coerce(bridge.column(roles.measure), measure)
        if status is not None and roles.predicate_column:
            row[roles.predicate_column] = status
        if label is not None and roles.label and roles.label in {
            c.name for c in bridge.columns
        }:
            row[roles.label] = label
        if (
            roles.child_key
            and roles.child_key in {c.name for c in bridge.columns}
            # When the "child" IS the anchor, child key and bridge FK are the
            # same column: only an EXPLICIT value (witness I's dangling key) may
            # write it, or the positional spread scatters one witness's rows
            # across two anchors.
            and not (
                roles.child_key == bridge_fk
                and child_key in ("__first__", "__second__")
            )
        ):
            if child_key == "__first__":
                row[roles.child_key] = child_keys[0] if child_keys else row[roles.child_key]
            elif child_key == "__second__":
                row[roles.child_key] = (
                    child_keys[1] if len(child_keys) > 1 else row[roles.child_key]
                )
            else:
                row[roles.child_key] = child_key
        if roles.period_column:
            row[roles.period_column] = _coerce(
                bridge.column(roles.period_column), period_days
            )
        return row

    passing = roles.predicate_pass[0] if roles.predicate_pass else None
    failing = roles.predicate_fail[0] if roles.predicate_fail else None
    nullable_control_label = _control_winner_label_is_nullable(
        tables, shape, relationships
    )
    argmax_label = _argmax_label_column(tables, shape)
    case_order_labels = _argmax_case_order_labels(argmax_label)
    null_order_label = (
        _argmax_null_order_label(argmax_label)
        if _argmax_null_order_is_legal(tables, relationships, shape)
        else None
    )

    if bridge is not None:
        for witness in ordered:
            if witness is WITNESS_CHILDLESS:
                continue
            if witness is WITNESS_CONTROL:
                # LOW first: an argmax that lost its ORDER BY takes this row.
                lower = mint_bridge(
                    witness, measure=_W_LOW, status=passing, label="alpha",
                    child_key="__first__", period_days=1,
                )
                winner = mint_bridge(
                    witness, measure=_W_HIGH, status=failing, label="beta",
                    child_key="__second__", period_days=2,
                )
                if nullable_control_label:
                    # Put NULL on the argmax winner to expose missing-label
                    # handling; keep the losing label non-NULL for contrast.
                    winner[roles.label] = None
                    controlled_label_rows.extend((lower, winner))
                bridge_rows.extend((lower, winner))
                control_group = (lower, winner)
            elif witness is WITNESS_DUPLICATE:
                template = mint_bridge(
                    witness, measure=_W_LOW, status=passing, label="alpha",
                    child_key="__first__", period_days=1,
                )
                bridge_rows.append(template)
                bridge_rows.append(dict(template))
                # Track the PAIR OBJECTS: later passes mutate rows in place, so
                # byte-identity is re-verified on exactly these dicts.
                duplicate_group = bridge_rows[-2:]
            elif witness is WITNESS_SAME_CHILD:
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_LOW, status=passing, label="alpha",
                        child_key="__first__", period_days=1,
                    )
                )
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_HIGH, status=passing, label="beta",
                        child_key="__first__", period_days=2,
                    )
                )
            elif witness is WITNESS_ALL_FAIL:
                # Both rows share a DAY on purpose: the only place a
                # COUNT(DISTINCT <day>) can diverge from a plain row count.
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_LOW, status=failing, label="alpha",
                        child_key="__first__", period_days=1,
                    )
                )
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_HIGH, status=failing, label="beta",
                        child_key="__second__", period_days=1,
                    )
                )
            elif witness is WITNESS_TIE:
                # WINNER FIRST — row F must MIRROR row A (whose winner is last),
                # or a `wrong_window` mutant taking a fixed within-partition
                # position reproduces gold everywhere and never dies.
                first = mint_bridge(
                    witness, measure=_W_TIE, status=passing, label="alpha",
                    child_key="__second__", period_days=2,
                )
                second = mint_bridge(
                    witness, measure=_W_TIE, status=passing, label="zulu",
                    child_key="__first__", period_days=1,
                )
                bridge_rows.extend((first, second))
                controlled_label_rows.extend((first, second))
            elif witness is WITNESS_OUT_OF_DOMAIN:
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_LOW, status=passing, label="alpha",
                        child_key="__first__", period_days=1,
                    )
                )
            elif witness is WITNESS_ON_THRESHOLD:
                boundary = shape.thresholds[0] if shape.thresholds else 2.0
                if boundary >= 1.0:
                    # Exactly the boundary COUNT of rows: '<= 2' vs '< 2'.
                    for i in range(int(boundary)):
                        bridge_rows.append(
                            mint_bridge(
                                witness, measure=_W_LOW, status=passing,
                                label=f"t{i}", child_key="__first__",
                                period_days=1 + i,
                            )
                        )
                else:
                    # A RATIO boundary: exactly half pass, so the ratio lands
                    # exactly on 0.5 and '>' and '>=' disagree.
                    bridge_rows.append(
                        mint_bridge(
                            witness, measure=_W_LOW, status=passing, label="alpha",
                            child_key="__first__", period_days=1,
                        )
                    )
                    bridge_rows.append(
                        mint_bridge(
                            witness, measure=_W_LOW, status=failing, label="beta",
                            child_key="__second__", period_days=2,
                        )
                    )
            elif witness is WITNESS_BRIDGE_NO_CHILD:
                dangling = _dangling_child_key(
                    bridge, child, relationships, roles, child_keys
                )
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_LOW, status=passing, label="alpha",
                        child_key=dangling, period_days=1,
                    )
                )
            elif witness is WITNESS_SECOND_PERIOD:
                # The SAME entity in THREE months, HIGH/LOW/LOW and OUT of period
                # order on purpose: without a second period a delta is constant,
                # without the decrease a dropped ELSE is invisible, and period
                # order would let an unordered running total agree by luck.
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_LOW, status=passing, label="beta",
                        child_key="__second__", period_days=70,
                    )
                )
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_HIGH, status=passing, label="alpha",
                        child_key="__first__", period_days=1,
                    )
                )
                bridge_rows.append(
                    mint_bridge(
                        witness, measure=_W_LOW, status=passing, label="gamma",
                        child_key="__second__", period_days=140,
                    )
                )
            elif witness is WITNESS_LATEST_FIRST:
                # LATER PERIOD FIRST — row P mirrors row A on the period axis
                # (A lists its later period last). A wrong_window mutant that
                # takes a fixed within-partition position cannot match both.
                latest = mint_bridge(
                    witness, measure=_W_HIGH, status=passing, label="beta",
                    child_key="__second__", period_days=2,
                )
                earlier = mint_bridge(
                    witness, measure=_W_LOW, status=passing, label="alpha",
                    child_key="__first__", period_days=1,
                )
                bridge_rows.extend((latest, earlier))
                latest_first_group = (latest, earlier)
            elif witness is WITNESS_BELOW_THRESHOLD:
                boundary = int(shape.thresholds[0]) if shape.thresholds else 2
                below = max(1, boundary - 1)
                for i in range(below):
                    bridge_rows.append(
                        mint_bridge(
                            witness,
                            measure=_W_LOW,
                            status=passing,
                            label=f"below{i}",
                            child_key="__first__",
                            period_days=1 + i,
                        )
                    )
            elif witness is WITNESS_NULL_MEASURE:
                # TWO genuine linked rows, not the synthetic LEFT-join row B
                # produces.  Both measures are NULL so the parent exercises an
                # all-NULL aggregate/argmax partition; distinct labels make the
                # argmax winner observable and prove that the no-rows default
                # does not apply to this non-empty group.
                first = mint_bridge(
                    witness, measure=_W_LOW, status=passing, label="alpha",
                    child_key="__first__", period_days=1,
                )
                second = mint_bridge(
                    witness, measure=_W_HIGH, status=passing, label="beta",
                    child_key="__second__", period_days=2,
                )
                first[roles.measure] = None
                second[roles.measure] = None
                bridge_rows.extend((first, second))
            elif witness is WITNESS_ARGMAX_CASE_ORDER:
                if case_order_labels is None:  # pragma: no cover - activation guards
                    raise ValueError(
                        "counterfactual: activated argmax case-order witness "
                        "without a schema-legal label pair"
                    )
                binary_winner, lower_winner = case_order_labels
                first = mint_bridge(
                    witness,
                    measure=_W_TIE,
                    status=passing,
                    label=binary_winner,
                    child_key="__first__",
                    period_days=1,
                )
                second = mint_bridge(
                    witness,
                    measure=_W_TIE,
                    status=passing,
                    label=lower_winner,
                    child_key="__second__",
                    period_days=2,
                )
                bridge_rows.extend((first, second))
                controlled_label_rows.extend((first, second))
                case_order_group = (first, second)
            elif witness is WITNESS_ARGMAX_NULL_ORDER:
                if null_order_label is None:  # pragma: no cover - activation guards
                    raise ValueError(
                        "counterfactual: activated argmax NULL-order witness "
                        "without a schema-legal non-NULL label"
                    )
                first = mint_bridge(
                    witness,
                    measure=_W_TIE,
                    status=passing,
                    label=null_order_label,
                    child_key="__first__",
                    period_days=1,
                )
                second = mint_bridge(
                    witness,
                    measure=_W_TIE,
                    status=passing,
                    label=null_order_label,
                    child_key="__second__",
                    period_days=2,
                )
                second[roles.label] = None
                bridge_rows.extend((first, second))
                controlled_label_rows.extend((first, second))
                null_order_group = (first, second)
            elif witness is WITNESS_DISTINCT_MEASURE:
                # Two different real links in the SAME anchor/state cell carry
                # one repeated non-NULL value. Unlike row D this needs no
                # second-hop child: it directly witnesses COUNT(DISTINCT
                # measure) versus COUNT(measure) on a one-hop rollup.
                first = mint_bridge(
                    witness,
                    measure=_W_TIE,
                    status=passing,
                    child_key="__first__",
                    period_days=1,
                )
                second = mint_bridge(
                    witness,
                    measure=_W_TIE,
                    status=passing,
                    child_key="__second__",
                    period_days=2,
                )
                bridge_rows.extend((first, second))
                distinct_measure_group = (first, second)
        if bridge.name == anchor_table:
            rows[anchor_table].extend(bridge_rows)
        else:
            rows[bridge.name] = bridge_rows

    for table in tables:
        rows.setdefault(table.name, [_mint_row(table, 0)])

    optional_owner_orphan = _append_optional_owner_orphan(
        by_name, relationships, rows, shape
    )

    # COLUMN-specific, never table-pair-wide: any relationship writing INTO a
    # column the witness construction controls must be skipped (those values ARE
    # the witnesses), but a bridge's SECOND FK into the anchor must still close
    # like any other link or it freezes a dangling key.
    witness_columns = set(shape.key_columns)
    if shape.fact == bridge_table:
        witness_columns.update(shape.fact_link_columns)
    witness_columns.update(b for b, _ in shape.child_link_pairs)
    if bridge_fk:
        witness_columns.add(bridge_fk)
    if roles.child_key:
        witness_columns.add(roles.child_key)
    if WITNESS_NULL_MEASURE in ordered:
        # The NULLs are the row-L discriminator.  Do not let an overlapping
        # optional relationship silently overwrite them during FK closure.
        witness_columns.add(roles.measure)

    def skip(rel: Relationship) -> bool:
        return rel.child_table == bridge_table and bool(
            witness_columns.intersection(rel.child_columns)
        )

    argmax_label_pins = _prepare_argmax_label_links(
        by_name,
        relationships,
        rows,
        bridge_table=bridge_table,
        label_column=roles.label,
        targets=controlled_label_rows,
    ) if controlled_label_rows and roles.label else {}

    _close_foreign_keys(
        by_name,
        relationships,
        rows,
        skip=skip,
        preserve=frozenset(
            {id(r) for r in duplicate_group}
            | {id(r) for r in controlled_label_rows}
            | {id(r) for r in distinct_measure_group or ()}
            | (
                {id(optional_owner_orphan[1])}
                if optional_owner_orphan is not None
                else set()
            )
        ),
        optional_dangling_edges=shape.join_edges,
        pinned_columns=argmax_label_pins,
    )
    _enforce_primary_keys(by_name, rows)
    _verify_optional_owner_orphan(optional_owner_orphan, rows, shape)
    _verify_window_order_witnesses(
        rows,
        bridge_table=(
            anchor_table
            if bridge is not None and bridge.name == anchor_table
            else bridge_table
        ),
        bridge_fk=bridge_fk,
        period_column=roles.period_column,
        control_group=control_group,
        latest_first_group=latest_first_group,
    )
    _verify_duplicate_witness(
        rows,
        anchor_table if (bridge is not None and bridge.name == anchor_table) else bridge_table,
        duplicate_group,
    )
    _verify_argmax_order_witnesses(
        rows,
        relationships=relationships,
        bridge_table=(
            anchor_table
            if bridge is not None and bridge.name == anchor_table
            else bridge_table
        ),
        bridge_fk=bridge_fk,
        measure_column=roles.measure,
        label_column=roles.label,
        case_order_group=case_order_group,
        case_order_labels=case_order_labels,
        null_order_group=null_order_group,
        null_order_label=null_order_label,
    )
    _verify_distinct_measure_witness(
        rows,
        bridge_table=(
            anchor_table
            if bridge is not None and bridge.name == anchor_table
            else bridge_table
        ),
        bridge_fk=bridge_fk,
        link_key=roles.link_key,
        measure_column=roles.measure,
        group=distinct_measure_group,
    )
    return {name: tuple(rows[name]) for name in sorted(rows)}


def _dangling_child_key(
    bridge: TableSpec,
    child: TableSpec | None,
    relationships: tuple[Relationship, ...],
    roles: FactRoles,
    child_keys: list[object],
) -> object:
    """A child-key value that matches NO child row — or a fail-closed refusal.

    Legal only when the bridge->child link is optional or the column nullable;
    otherwise the shape should never have declared the witness, so this RAISES.
    """
    if child is None or not roles.child_key:
        raise ValueError(
            "witness I (bridge row with no child) needs a second hop and a child "
            "key column"
        )
    rel = _rel_between(relationships, bridge.name, child.name, roles.child_key)
    column = bridge.column(roles.child_key)
    if rel is not None and rel.required:
        if not column.nullable:
            raise ValueError(
                f"witness I is unconstructible: {bridge.name}.{roles.child_key} is a "
                f"REQUIRED, non-nullable link to {child.name}, so no bridge row can "
                "point at a missing child. Do not declare the witness on this schema."
            )
        return None
    if column.type in (ColumnType.INTEGER, ColumnType.BIGINT):
        return max((int(k) for k in child_keys if isinstance(k, int)), default=0) + 777
    return f"{child.name}_missing_777"


def _relationship_key(
    rel: Relationship,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Stable identity for a relationship used by closure bookkeeping."""

    return (
        rel.child_table,
        rel.parent_table,
        tuple(rel.child_columns),
        tuple(rel.parent_columns),
    )


def _finite_key_domain(column: ColumnSpec) -> tuple[object, ...] | None:
    """The complete finite domain when the schema declares one."""

    if column.enum_values:
        return tuple(column.enum_values)
    if column.type is ColumnType.BOOLEAN:
        return (False, True)
    return None


def _fresh_optional_key(
    rel: Relationship,
    child: TableSpec,
    parent_rows: list[dict],
    child_rows: list[dict],
    row_index: int,
) -> tuple[object, ...] | None:
    """Return one type-valid child key absent from the finite parent row set.

    Optional relationships may use non-nullable columns. Search at most
    ``parents + children + 1`` values in finite domains or vary one open-domain
    column over that bound. Skip candidates that collide with child primary or
    business keys so later key enforcement does not erase a witness.
    """

    if rel.required or not parent_rows or len(child_rows) < 2:
        return None
    try:
        columns = tuple(child.column(name) for name in rel.child_columns)
    except KeyError:
        return None
    parent_keys = {
        tuple(row.get(name) for name in rel.parent_columns) for row in parent_rows
    }
    row = child_rows[row_index]

    def usable(candidate: tuple[object, ...]) -> bool:
        if any(value is None for value in candidate) or candidate in parent_keys:
            return False
        trial = dict(row)
        for name, value in zip(rel.child_columns, candidate, strict=True):
            trial[name] = value
        for identity in (child.primary_key, child.business_key):
            if not identity:
                continue
            key = tuple(trial.get(name) for name in identity)
            if any(value is None for value in key):
                continue
            for index, other in enumerate(child_rows):
                if index == row_index:
                    continue
                if key == tuple(other.get(name) for name in identity):
                    return False
        return True

    finite = tuple(_finite_key_domain(column) for column in columns)
    search_bound = len(parent_keys) + len(child_rows) + 1
    if all(domain is not None for domain in finite):
        candidates = product(*(domain for domain in finite if domain is not None))
        return next(
            (candidate for candidate in islice(candidates, search_bound) if usable(candidate)),
            None,
        )

    variable = next(index for index, domain in enumerate(finite) if domain is None)
    fixed = [
        domain[0] if domain is not None else _mint(column, child.name, 400)
        for column, domain in zip(columns, finite, strict=True)
    ]
    for offset in range(search_bound):
        candidate = list(fixed)
        candidate[variable] = _mint(columns[variable], child.name, 400 + offset)
        value = tuple(candidate)
        if usable(value):
            return value
    return None


def _shape_owner_relationship(
    shape: StarShape,
    relationships: tuple[Relationship, ...],
) -> Relationship | None:
    """Return the optional standard fact-to-parent edge owned by ``shape``.

    The discriminator below is intentionally narrower than a general optional
    FK policy.  It applies only when the shape names the complete relationship
    positionally and its ordinary witness orientation is parent -> fact.  A
    self-link, duplicate declaration, or column shared with another relationship
    is declined: breaking any of those could exercise a different semantic
    surface than the mart's retained-parent join.
    """

    if (
        not shape.fact
        or shape.fact == shape.parent
        or not shape.fact_link_columns
        or len(shape.fact_link_columns) != len(shape.parent_keys)
    ):
        return None
    if shape.witnesses:
        anchor_table, _anchor_key, bridge_table, _bridge_fk = shape.witness_anchor()
        if anchor_table != shape.parent or bridge_table != shape.fact:
            return None

    wanted = (
        shape.fact,
        shape.parent,
        tuple(shape.fact_link_columns),
        tuple(shape.parent_keys),
    )
    matches = [rel for rel in relationships if _relationship_key(rel) == wanted]
    if len(matches) != 1 or matches[0].required:
        return None
    owner = matches[0]
    owner_columns = set(owner.child_columns)
    if any(
        rel is not owner
        and rel.child_table == owner.child_table
        and bool(owner_columns.intersection(rel.child_columns))
        for rel in relationships
    ):
        return None
    return owner


def _append_optional_owner_orphan(
    by_name: Mapping[str, TableSpec],
    relationships: tuple[Relationship, ...],
    rows: dict[str, list[dict]],
    shape: StarShape,
) -> tuple[Relationship, Row] | None:
    """Plant one non-NULL orphan for an optional standard owner edge.

    A nullable optional FK already receives a NULL row during ordinary closure,
    but NULL alone cannot distinguish logic that deliberately retains a non-NULL
    missing-parent child.  This dedicated row covers that second legal state.
    Existing realized orphans are reused so recovered shapes never multiply the
    same proof.
    """

    relationship = _shape_owner_relationship(shape, relationships)
    if relationship is None:
        return None
    if (
        relationship.child_table not in by_name
        or relationship.parent_table not in by_name
        or relationship.child_table not in rows
        or relationship.parent_table not in rows
    ):
        return None

    child_rows = rows[relationship.child_table]
    parent_rows = rows[relationship.parent_table]
    parent_keys = {
        tuple(row.get(column) for column in relationship.parent_columns)
        for row in parent_rows
    }
    for row in child_rows:
        key = tuple(row.get(column) for column in relationship.child_columns)
        if not any(value is None for value in key) and key not in parent_keys:
            return relationship, row

    child = by_name[relationship.child_table]
    orphan = _mint_row(child, _OPTIONAL_OWNER_ORPHAN_MINT_BASE)
    child_rows.append(orphan)
    fresh = _fresh_optional_key(
        relationship,
        child,
        parent_rows,
        child_rows,
        len(child_rows) - 1,
    )
    if fresh is None:
        child_rows.pop()
        return None
    for column, value in zip(
        relationship.child_columns, fresh, strict=True
    ):
        orphan[column] = value
    return relationship, orphan


def _verify_optional_owner_orphan(
    planted: tuple[Relationship, Row] | None,
    rows: Mapping[str, Sequence[Row]],
    shape: StarShape,
) -> None:
    """Fail closed if FK closure or key enforcement erased the discriminator."""

    if planted is None:
        return
    relationship, orphan = planted
    if not any(
        orphan is row for row in rows.get(relationship.child_table, ())
    ):
        raise ValueError(
            f"counterfactual: mart {shape.mart!r} optional owner orphan was "
            "lost during primary-key enforcement"
        )
    key = tuple(orphan.get(column) for column in relationship.child_columns)
    parent_keys = {
        tuple(row.get(column) for column in relationship.parent_columns)
        for row in rows.get(relationship.parent_table, ())
    }
    if any(value is None for value in key) or key in parent_keys:
        raise ValueError(
            f"counterfactual: mart {shape.mart!r} optional owner orphan no "
            "longer carries a complete non-NULL missing-parent key"
        )


def _optional_dangling_targets(
    by_name: dict[str, TableSpec],
    relationships: list[Relationship],
    rows: dict[str, list[dict]],
    preserve: frozenset[int],
    join_edges: Iterable[
        tuple[str, tuple[str, ...], str, tuple[str, ...]]
    ] = (),
) -> dict[
    tuple[str, str, tuple[str, ...], tuple[str, ...]],
    tuple[int, tuple[object, ...]],
]:
    """Choose one non-NULL orphan per eligible recovered optional join edge."""

    required_columns: dict[str, set[str]] = {}
    for rel in relationships:
        if rel.required:
            required_columns.setdefault(rel.child_table, set()).update(rel.child_columns)

    eligible = _join_edge_relationship_keys(join_edges)
    targets = {}
    for rel in sorted(
        (
            relationship
            for relationship in relationships
            if not relationship.required
            and _relationship_key(relationship) in eligible
        ),
        key=_relationship_key,
    ):
        child = by_name[rel.child_table]
        # A nullable optional link keeps the established NULL witness. Do not
        # replace it with a new non-NULL policy surface.
        nullable = {column.name for column in child.columns if column.nullable}
        if set(rel.child_columns) <= nullable:
            continue
        # One column cannot simultaneously carry a deliberately broken
        # optional link and a required link. Required integrity always wins.
        if set(rel.child_columns) & required_columns.get(rel.child_table, set()):
            continue
        child_rows = rows[rel.child_table]
        candidates = [
            index for index, row in enumerate(child_rows) if id(row) not in preserve
        ]
        if len(candidates) < 2:
            continue
        row_index = candidates[-1]
        fresh = _fresh_optional_key(
            rel,
            child,
            rows[rel.parent_table],
            child_rows,
            row_index,
        )
        if fresh is not None:
            targets[_relationship_key(rel)] = (row_index, fresh)
    return targets


def _close_foreign_keys(
    by_name: dict[str, TableSpec],
    relationships: tuple[Relationship, ...],
    rows: dict[str, list[dict]],
    *,
    skip: Callable[[Relationship], bool],
    preserve: frozenset[int] = frozenset(),
    optional_dangling_edges: Iterable[
        tuple[str, tuple[str, ...], str, tuple[str, ...]]
    ] = (),
    pinned_columns: Mapping[int, frozenset[str]] | None = None,
) -> None:
    """Close required links and retain one legal optional-link discriminator.

    ``skip`` preserves witness-owned links. For an optional link, null a fully
    nullable key or use a fresh dangling key only for an allowed recovered plan
    edge. Point other foreign keys to existing parents. Preserve pinned witness
    columns, resolve overlapping links in topological order, and leave a row
    unchanged when no compatible parent exists. For identity links, mint the
    missing parent instead of rewriting the child's identity.
    """
    live = [
        rel
        for rel in relationships
        if rel.child_table in rows
        and rel.parent_table in rows
        and not skip(rel)
        and _link_is_closable(rel, by_name)
    ]
    order = _topological_tables(by_name, live)
    rank = {name: i for i, name in enumerate(order)}
    #: (table, row index) -> columns already written by an earlier relationship.
    pinned: dict[tuple[str, int], set[str]] = {
        (table, index): set((pinned_columns or {}).get(id(row), ()))
        for table, table_rows in rows.items()
        for index, row in enumerate(table_rows)
        if (pinned_columns or {}).get(id(row))
    }
    dangling_targets = _optional_dangling_targets(
        by_name, live, rows, preserve, optional_dangling_edges
    )

    # Tables with a SKIPPED link must not receive minted rows (a minted row's
    # skipped link would stay unclosed), so links into them keep the repoint.
    guarded = {
        rel.child_table
        for rel in relationships
        if rel.child_table in rows and rel.parent_table in rows and skip(rel)
    }
    _extend_parents_for_identity_links(
        by_name,
        live,
        rows,
        rank,
        pinned,
        guarded,
        dangling_targets,
    )

    for rel in sorted(
        live,
        key=lambda r: (
            rank.get(r.parent_table, len(rank)),
            rank.get(r.child_table, len(rank)),
            not r.required,          # required links close first
            r.child_table,
            r.parent_table,
            r.child_columns,
        ),
    ):
        parent_rows = rows[rel.parent_table]
        if not parent_rows:
            continue
        child = by_name[rel.child_table]
        nullable = {c.name for c in child.columns if c.nullable}
        child_rows = rows[rel.child_table]
        # The optional-link NULL goes on the LAST row that is not a preserved
        # duplicate twin.
        null_candidates = [i for i, r in enumerate(child_rows) if id(r) not in preserve]
        null_index = null_candidates[-1] if null_candidates else -1
        for i, row in enumerate(child_rows):
            key = (rel.child_table, i)
            already = pinned.get(key, set())
            overlap = [c for c in rel.child_columns if c in already]
            dangling = dangling_targets.get(_relationship_key(rel))
            if not overlap and dangling is not None and i == dangling[0]:
                for col, value in zip(rel.child_columns, dangling[1], strict=True):
                    row[col] = value
                pinned.setdefault(key, set()).update(rel.child_columns)
                continue
            if (
                not overlap
                and not rel.required
                and set(rel.child_columns) <= nullable
                and i == null_index
                and len(child_rows) > 1
            ):
                for col in rel.child_columns:
                    row[col] = None
                pinned.setdefault(key, set()).update(rel.child_columns)
                continue
            anchor = _consistent_parent(rel, row, overlap, parent_rows)
            if anchor is None:
                # No parent row agrees with what an earlier relationship already
                # pinned. Leave the row alone rather than break that link too.
                continue
            for child_col, parent_col in zip(rel.child_columns, rel.parent_columns):
                row[child_col] = anchor[parent_col]
            pinned.setdefault(key, set()).update(rel.child_columns)


#: Minting index base for parent rows the identity-link pre-pass adds — below
#: the 500-wide per-shape namespace stride (anchors 0.., children 100.., 200..).
_IDENTITY_PARENT_MINT_BASE = 300


def _extend_parents_for_identity_links(
    by_name: dict[str, TableSpec],
    live: list[Relationship],
    rows: dict[str, list[dict]],
    rank: dict[str, int],
    pinned: dict[tuple[str, int], set[str]],
    guarded: set[str] = frozenset(),
    dangling_targets: Mapping[
        tuple[str, str, tuple[str, ...], tuple[str, ...]],
        tuple[int, tuple[object, ...]],
    ] | None = None,
) -> None:
    """Pre-pass of `_close_foreign_keys` for links whose child columns overlap
    the child's PRIMARY KEY: mint the missing PARENT rows (children first) and
    pin the child's columns. Links into a `guarded` parent are left to the main
    pass."""
    targets = dangling_targets or {}
    identity_links = [
        rel
        for rel in live
        if rel.parent_table not in guarded
        and set(rel.child_columns) & set(by_name[rel.child_table].primary_key)
    ]
    counter = 0
    for rel in sorted(
        identity_links,
        key=lambda r: (
            -rank.get(r.child_table, len(rank)),
            r.child_table,
            r.parent_table,
            r.child_columns,
        ),
    ):
        parent = by_name[rel.parent_table]
        parent_rows = rows[rel.parent_table]
        present = {
            tuple(p.get(c) for c in rel.parent_columns) for p in parent_rows
        }
        for i, row in enumerate(rows[rel.child_table]):
            dangling = targets.get(_relationship_key(rel))
            if dangling is not None and i == dangling[0]:
                # The optional relationship deliberately leaves this identity
                # value unmatched. Minting a parent here would erase the proof.
                continue
            key = tuple(row.get(c) for c in rel.child_columns)
            if any(v is None for v in key):
                continue
            if key not in present:
                minted = _mint_row(parent, _IDENTITY_PARENT_MINT_BASE + counter)
                counter += 1
                for child_col, parent_col in zip(rel.child_columns, rel.parent_columns, strict=True):
                    minted[parent_col] = row[child_col]
                parent_rows.append(minted)
                present.add(key)
            pinned.setdefault((rel.child_table, i), set()).update(rel.child_columns)


def _topological_tables(
    by_name: dict[str, TableSpec], relationships: list[Relationship]
) -> list[str]:
    """Table names with every parent before its children (Kahn, name-stable).

    Cycles are ordinary in vendored schemas, not an error: whatever remains
    after the queue drains is appended in name order.
    """
    names = sorted(by_name)
    children: dict[str, set[str]] = {n: set() for n in names}
    indegree: dict[str, int] = {n: 0 for n in names}
    for rel in relationships:
        if rel.parent_table == rel.child_table:
            continue
        if rel.child_table in children[rel.parent_table]:
            continue
        children[rel.parent_table].add(rel.child_table)
        indegree[rel.child_table] += 1

    queue = sorted(n for n in names if indegree[n] == 0)
    out: list[str] = []
    while queue:
        node = queue.pop(0)
        out.append(node)
        for nxt in sorted(children[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
                queue.sort()
    out += [n for n in names if n not in set(out)]
    return out


def _consistent_parent(
    rel: Relationship,
    child_row: dict,
    overlap: list[str],
    parent_rows: list[dict],
) -> dict | None:
    """The parent row this link may point at without disturbing pinned columns."""
    if not overlap:
        return parent_rows[0]
    for candidate in parent_rows:
        if all(
            candidate[parent_col] == child_row.get(child_col)
            for child_col, parent_col in zip(rel.child_columns, rel.parent_columns)
            if child_col in overlap
        ):
            return candidate
    return None


def _enforce_primary_keys(
    by_name: dict[str, TableSpec], rows: dict[str, list[dict]]
) -> None:
    """Drop rows whose primary key collides after FK closure (fail closed).

    Dropping beats re-minting: the survivor still carries the discrimination,
    and a new key value would silently break the link closure just repaired.
    """
    for name, table_rows in rows.items():
        pk = by_name[name].primary_key
        if not pk:
            continue
        seen: set[tuple] = set()
        kept: list[dict] = []
        for row in table_rows:
            key = tuple(row.get(c) for c in pk)
            if key in seen:
                continue
            seen.add(key)
            kept.append(row)
        rows[name] = kept


def _verify_window_order_witnesses(
    rows: Mapping[str, Sequence[dict]],
    *,
    bridge_table: str,
    bridge_fk: str,
    period_column: str,
    control_group: tuple[dict, dict] | None,
    latest_first_group: tuple[dict, dict] | None,
) -> None:
    """Fail closed if key/FK cleanup made an ordering-axis pair inert.

    Row A (winner listed LAST) and row P (winner listed FIRST) are the two
    parents a `wrong_window` mutant cannot both match. Each pair must survive
    by identity, stay on ONE parent, keep its listing order, and keep the
    period order it was minted with. Silent loss of either pair is how a
    claimed kill came to depend on the engine's scan order.
    """
    surviving = list(rows.get(bridge_table, ()))
    groups = (
        ("A control", control_group, "last"),
        ("P latest-first", latest_first_group, "first"),
    )
    for name, group, winner_position in groups:
        if group is None:
            continue
        positions = []
        for target in group:
            index = next((i for i, row in enumerate(surviving) if row is target), None)
            if index is None:
                raise ValueError(
                    f"counterfactual: row {name} witness lost one of its two rows "
                    "during primary-key enforcement; the wrong_window claim would "
                    "rest on the engine's scan order"
                )
            positions.append(index)
        first, second = group
        if first.get(bridge_fk) != second.get(bridge_fk):
            raise ValueError(
                f"counterfactual: row {name} witness was split across parents "
                "during foreign-key closure"
            )
        if positions[0] >= positions[1]:
            raise ValueError(
                f"counterfactual: row {name} witness lost its listing order; its "
                f"winner must be listed {winner_position}"
            )
        if period_column:
            earlier_row, later_row = (
                (first, second) if winner_position == "last" else (second, first)
            )
            earlier = earlier_row.get(period_column)
            later = later_row.get(period_column)
            try:
                out_of_order = (
                    earlier is not None and later is not None and not (earlier < later)
                )
            except TypeError:
                out_of_order = False
            if out_of_order:
                raise ValueError(
                    f"counterfactual: row {name} witness no longer orders its "
                    "periods as minted"
                )


def _verify_argmax_order_witnesses(
    rows: Mapping[str, Sequence[dict]],
    *,
    relationships: tuple[Relationship, ...],
    bridge_table: str,
    bridge_fk: str,
    measure_column: str,
    label_column: str,
    case_order_group: tuple[dict, dict] | None,
    case_order_labels: tuple[str, str] | None,
    null_order_group: tuple[dict, dict] | None,
    null_order_label: str | None,
) -> None:
    """Fail closed if key/FK cleanup made an advertised argmax pair inert."""

    groups: tuple[
        tuple[str, tuple[dict, dict] | None, tuple[object, object] | None], ...
    ] = (
        ("case-order", case_order_group, case_order_labels),
        (
            "NULL-order",
            null_order_group,
            (null_order_label, None) if null_order_label is not None else None,
        ),
    )
    surviving = rows.get(bridge_table, ())
    for name, group, expected_labels in groups:
        if group is None:
            continue
        if expected_labels is None:  # pragma: no cover - construction guards
            raise ValueError(
                f"counterfactual: argmax {name} witness has no expected labels"
            )
        if not all(any(target is row for row in surviving) for target in group):
            raise ValueError(
                f"counterfactual: argmax {name} witness lost one of its two "
                "rows during primary-key enforcement"
            )
        first, second = group
        if (
            first.get(bridge_fk) != second.get(bridge_fk)
            or first.get(measure_column) != second.get(measure_column)
            or (first.get(label_column), second.get(label_column))
            != expected_labels
        ):
            raise ValueError(
                f"counterfactual: argmax {name} witness was changed during "
                "foreign-key closure and no longer presents one same-parent, "
                "same-measure pair with the required labels"
            )
        for target in group:
            for relationship in relationships:
                if (
                    relationship.child_table != bridge_table
                    or label_column not in relationship.child_columns
                ):
                    continue
                key = tuple(
                    target.get(column)
                    for column in relationship.child_columns
                )
                if any(value is None for value in key):
                    if relationship.required:
                        raise ValueError(
                            f"counterfactual: argmax {name} witness placed NULL "
                            "through a required relationship"
                        )
                    continue
                parent_keys = {
                    tuple(row.get(column) for column in relationship.parent_columns)
                    for row in rows.get(relationship.parent_table, ())
                }
                if key not in parent_keys:
                    raise ValueError(
                        f"counterfactual: argmax {name} witness references "
                        f"missing {relationship.parent_table} key {key}"
                    )


def _verify_distinct_measure_witness(
    rows: Mapping[str, Sequence[dict]],
    *,
    bridge_table: str,
    bridge_fk: str,
    link_key: str,
    measure_column: str,
    group: tuple[dict, dict] | None,
) -> None:
    """Fail closed if cleanup made the repeated-value witness inert."""

    if group is None:
        return
    surviving = rows.get(bridge_table, ())
    if not all(any(target is row for row in surviving) for target in group):
        raise ValueError(
            "counterfactual: distinct-measure witness lost one of its two "
            "rows during primary-key enforcement"
        )
    first, second = group
    if (
        first.get(bridge_fk) != second.get(bridge_fk)
        or first.get(measure_column) is None
        or first.get(measure_column) != second.get(measure_column)
        or first.get(link_key) == second.get(link_key)
    ):
        raise ValueError(
            "counterfactual: distinct-measure witness was changed during "
            "foreign-key closure and no longer presents two distinct real "
            "links in one group with the same non-NULL measure"
        )


def _verify_duplicate_witness(
    rows: dict[str, list[dict]], table: str, group: list[dict]
) -> None:
    """FAIL CLOSED: the duplicate witness pair must survive byte-identical.

    If a post-construction pass changed a twin's bytes or dropped one, DISTINCT
    and no-dedupe agree and the witness is inert, so this raises instead.
    """
    if not group:
        return
    surviving = rows.get(table, [])
    present = [r for r in group if any(r is kept for kept in surviving)]
    if len(present) != len(group):
        raise InertDuplicateWitnessError(
            f"counterfactual: duplicate witness in table {table!r} lost "
            f"{len(group) - len(present)} of its {len(group)} rows to "
            "post-construction passes (primary-key enforcement collapsed the "
            "pair?) — the dedupe discrimination is inert. Do not declare the "
            "duplicate witness on a table whose primary key forbids duplicates."
        )
    first = group[0]
    for twin in group[1:]:
        differing = sorted(
            c for c in set(first) | set(twin) if first.get(c) != twin.get(c)
        )
        if differing:
            raise InertDuplicateWitnessError(
                f"counterfactual: duplicate witness rows in table {table!r} are "
                f"not byte-identical — columns {differing} differ "
                f"({ {c: (first.get(c), twin.get(c)) for c in differing} !r}). "
                "DISTINCT never collapses distinct rows, so DISTINCT-vs-no-"
                "dedupe is undiscriminated; the construction is inert and is "
                "refused instead of frozen."
            )


#: Which population POLICY a task's five populations follow — the catalogue's
#: expectations only hold under the policy that produced the data.
POLICY_CONSTRUCTED = "constructed"      # counterfactual_literal_rows built them
POLICY_PROVIDED_ROWS = "provided-rows"  # the pool ships real rows (WikiDBs)


#: One description per claimable ``<kind>[@<variant>]``, each naming WHICH
#: witness row makes the mutant observable — an unnamed witness is uncheckable.
_CLAIM_DESCRIPTIONS: dict[str, str] = {
    "inner_join": (
        "Every LEFT JOIN replaced by INNER JOIN: rows with no match on the other "
        "side disappear instead of being reported with their declared defaults. "
        "Witnessed by the childless anchor row (B) and by the link whose partner "
        "row is missing (I)."
    ),
    "inner_join@second_hop": (
        "Only the SECOND hop flipped to INNER. Separates 'the anchor has no bridge "
        "row' from 'the bridge row's child is missing' — a distinction the "
        "flip-every-join mutant cannot make, and the case that makes bridge "
        "fan-out attackable rather than merely present. Witnessed by row I."
    ),
    "no_dedup": (
        "DISTINCT dropped: two links pointing at the SAME child (or two rows "
        "sharing the counted value) are counted twice. Witnessed by row D, the "
        "same-child fan-out parent, and by the duplicate pair (C) where the "
        "bridge declares no upstream primary key, or by row O's two distinct "
        "links carrying one repeated non-missing measure. This surface exists "
        "on every pool, unlike the dedupe one — on synsql only 11 of 168,239 "
        "tables lack a declared primary key."
    ),
    "no_null_default": (
        "COALESCE dropped: a group with no matching rows reports NULL instead of "
        "the declared default. Witnessed by the childless anchor row (B)."
    ),
    "dropped_filter": (
        "The CASE predicate removed from inside its aggregate, so a filtered "
        "measure reports the unfiltered total. Witnessed by row E, whose links "
        "ALL fail the predicate, and by the control row (A), where exactly one of "
        "two links passes."
    ),
    "dropped_filter@filter_to_where": (
        "The predicate HOISTED into a WHERE on the joined relation — the "
        "realistic wrong implementation. A group whose rows all fail the "
        "predicate then VANISHES instead of reporting 0, which is a row-count "
        "error rather than a value error. Row E is the only row that can tell "
        "the two placements apart."
    ),
    "wrong_denominator": (
        "Every guarded division's denominator replaced by 1, so the ratio reports "
        "its raw numerator. Witnessed by the control row (A), whose numerator is "
        "strictly positive and strictly smaller than its denominator; a row with "
        "a zero numerator would score the same either way."
    ),
    "wrong_denominator@filtered_denominator": (
        "The FILTERED count used on BOTH sides of the ratio, so the share "
        "collapses to 1 wherever anything qualifies — the plausible misreading of "
        "'qualifying over total'. Witnessed by rows A and E."
    ),
    "wrong_grain": (
        "A non-grain column appended to the GROUP BY, splitting one output row "
        "into several. Witnessed by row D's fan-out (two links under one anchor) "
        "and by row A's two distinct children."
    ),
    "wrong_agg_stage@filter_before_aggregate": (
        "The grouped link-count threshold is removed after aggregation and "
        "replaced by a raw-row presence filter before aggregation. Row K has "
        "one linked row: it passes raw presence but is below the declared "
        "inclusive threshold of two, so the wrong implementation emits an "
        "extra mart row."
    ),
    "wrong_window": (
        "ORDER BY stripped from inside OVER(), then PARTITION BY from any window "
        "that had none: the ranked columns stop being functions of the input. "
        "Witnessed by a MIRRORED PAIR on the ordering axis: the control row (A) "
        "lists its winner LAST (its LOWER measure, or its EARLIER period, comes "
        "first), while a mirror row lists its winner FIRST — the tie row (F), "
        "which lists first the label that sorts first under the declared "
        "case-sensitive text order, or the period row (P), which lists its "
        "LATER period first. An unordered window has only a POSITION to go on, "
        "and no fixed position is right on both."
    ),
    "wrong_window@drop_frame": (
        "The explicit frame dropped, so the RANGE default lumps together every "
        "row with an equal ORDER BY key — a running total that is wrong exactly "
        "on ties and only on ties. Witnessed by tied ordering keys."
    ),
    "custom@wrong_boundary_else": (
        "The mandatory ELSE dropped from every CASE ladder: a value the mapping "
        "does not name becomes NULL where the specification demands a value. "
        "Witnessed by row G, which carries a legal domain value the ladder "
        "deliberately does not name."
    ),
    "custom@wrong_boundary_inclusive": (
        "Every threshold comparison flipped between strict and inclusive. "
        "Witnessed by row H, whose value lands EXACTLY on a declared boundary — "
        "the only place '>' and '>=' disagree."
    ),
}


def _claimed_cases(claims: tuple[str, ...]) -> list[AttackCase]:
    """One AttackCase per claim the SHAPE's witnesses justify.

    Each ships an explicit kind directive so an inert mutation RAISES instead of
    silently passing. The expectation is only ``{counterfactual: False}`` — the
    population CONSTRUCTED for this discrimination; claiming an effect on
    generated data would be a bet.
    """
    from elt_taskgen.verification.attacks import (  # local: avoid an import cycle
        KIND_DIRECTIVE_PREFIX,
        split_kind_directive,
    )

    cases: list[AttackCase] = []
    for claim in claims:
        kind, variant = split_kind_directive(claim)
        description = _CLAIM_DESCRIPTIONS.get(claim)
        if description is None:
            raise ValueError(
                f"attack claim {claim!r} has no description — a case nobody can "
                "read is a case nobody can check"
            )
        cases.append(
            AttackCase(
                name=claim.replace("@", "__"),
                kind=kind,
                description=description,
                mutation=KIND_DIRECTIVE_PREFIX + claim,
                expected_pass={PopulationName.COUNTERFACTUAL: False},
            )
        )
    return cases


def _skip_extraction_case(backends: int, policy: str) -> AttackCase:
    """The 'one backend never loaded' probe — a REQUIRED EL mutant.

    Carries ``directive:load:skip_backend`` so the backend's artifacts are served
    EMPTY and the real per-format readers run (a skip-list would bypass the
    extraction surface entirely). Required wherever a second backend exists.
    """
    populations = (
        _EL_ALL_POPULATIONS
        if policy == POLICY_CONSTRUCTED
        else _EL_GENERATED_POPULATIONS
    )
    return AttackCase(
        name="skip_extraction",
        kind=AttackKind.SKIP_EXTRACTION,
        description=(
            f"Loads only some of the {backends} source backends: every "
            "artifact of one backend is served empty, so its tables land 0 "
            "rows and both the strict-binary extract-load reward and the mart "
            "that needed those rows are lost."
        ),
        mutation=_LOAD_DIRECTIVE_PREFIX + "skip_backend",
        expected_pass=dict.fromkeys(populations, False),
        required=True,
    )


def _degenerate_cases(backends: int, policy: str = POLICY_CONSTRUCTED) -> list[AttackCase]:
    """The plan-level probes every task carries, whatever its shape."""
    P = PopulationName
    cases = [
        AttackCase(
            name="hardcoded_primary_outputs",
            kind=AttackKind.CONSTANTS,
            description=(
                "Emits the frozen primary-population outputs verbatim, computing "
                "nothing. The counterfactual's rows are literal and minted from a "
                "disjoint value base, so they cannot coincide with primary."
            ),
            mutation=_HARDCODE_PRIMARY_DIRECTIVE,
            expected_pass={
                P.PRIMARY: True,
                P.COUNTERFACTUAL: False,
                P.DEVELOPMENT: False,
            },
        ),
        AttackCase(
            name="keys_only",
            kind=AttackKind.KEYS_ONLY,
            description=(
                "Emits the grain columns with constant measures: the mart's whole "
                "information content is the measures."
            ),
            expected_pass={P.PRIMARY: False},
            required=False,
        ),
        AttackCase(
            name="no_op",
            kind=AttackKind.NO_OP,
            description="Submits nothing at all.",
            expected_pass={P.PRIMARY: False},
            required=False,
        ),
    ]
    if backends > 1:
        cases.append(_skip_extraction_case(backends, policy))
    return cases


# Extract/load attacks must change row counts because Stage 1 is count-only.

#: ``directive:load:<name>`` — kept as a constant so the declaration site and the
#: execution site in verification/attacks.py cannot drift apart.
_LOAD_DIRECTIVE_PREFIX = "directive:load:"

#: Every population an EL case may claim under the CONSTRUCTED policy (its
#: literal rows give every table a row); under provided-rows the vendor decides
#: the counts, so the counterfactual is left unasserted.
_EL_ALL_POPULATIONS = (
    PopulationName.DEVELOPMENT,
    PopulationName.PRIMARY,
    PopulationName.RESAMPLED,
    PopulationName.COUNTERFACTUAL,
    PopulationName.STRESS,
)
_EL_GENERATED_POPULATIONS = (
    PopulationName.DEVELOPMENT,
    PopulationName.PRIMARY,
    PopulationName.RESAMPLED,
    PopulationName.STRESS,
)


def _el_case(
    name: str,
    description: str,
    *,
    directive: str | None = None,
    expected_pass: dict[PopulationName, bool],
    required: bool,
    kind_name: str | None = None,
) -> AttackCase:
    """One EL AttackCase, carrying its FIRST-CLASS extraction kind.

    The case name IS the kind by default, because the required-mutants gate
    admits evidence BY KIND — a load case filed under CUSTOM is invisible to the
    EL battery. `kind_name` overrides it for the one kind with two cases, and an
    undeclared kind RAISES rather than falling back to CUSTOM."""
    return AttackCase(
        name=name,
        kind=AttackKind(kind_name or name),
        description=description,
        mutation=_LOAD_DIRECTIVE_PREFIX + (directive or name),
        expected_pass=expected_pass,
        required=required,
    )


def _fabricate_cases(
    populations: tuple[PopulationSpec, ...], constructed: bool
) -> list[AttackCase]:
    """The two `fabricate_counts` cases a task's POPULATION SPECS can justify.

    A loss is declared only where the realized-count divergence GUARANTEES it
    (2-7% off the hint, at or above the divergence floor); the primary-echo form
    declares its counterfactual kill only, never the pair it leaks on.
    """
    from elt_taskgen.generation.source_data import (
        REALIZED_DIVERGENCE_MAX_PCT,
        REALIZED_DIVERGENCE_MIN_SCALE,
    )

    by_name = {p.name: p for p in populations}
    cases: list[AttackCase] = []

    def _guaranteed_divergent(spec: PopulationSpec | None) -> bool:
        return (
            spec is not None
            and bool(spec.scale)
            and min(spec.scale.values()) >= REALIZED_DIVERGENCE_MIN_SCALE
        )

    if any(p.scale for p in populations):
        expected: dict[PopulationName, bool] = {}
        for pop_name in (
            PopulationName.PRIMARY,
            PopulationName.RESAMPLED,
            PopulationName.STRESS,
        ):
            if _guaranteed_divergent(by_name.get(pop_name)):
                expected[pop_name] = False
        cases.append(
            _el_case(
                "fabricate_counts",
                "The stage-1 answer submitted straight from the DECLARED scale "
                "hint — numbers read off the documentation, not off any "
                "artifact. The direct extract-load analogue of "
                "hardcode-population-outputs, and the mutant the realized-"
                "count divergence exists to kill: every graded population at "
                "or above the divergence floor is generated 2-7% away from "
                "its hint, so the documented number is never the realized "
                "one. If this mutant ever keeps full extract-load reward "
                "everywhere, the divergence has regressed and "
                "verification/attacks.py raises InertLoadMutationError "
                "rather than letting it pass.",
                expected_pass=expected,
                required=bool(expected),
            )
        )

    primary = by_name.get(PopulationName.PRIMARY)
    if primary is not None and len(by_name) > 1:
        echo_expected: dict[PopulationName, bool] = {}
        counterfactual = by_name.get(PopulationName.COUNTERFACTUAL)
        if (
            constructed
            and counterfactual is not None
            and counterfactual.literal_rows
            and primary.scale
        ):
            # Kill guaranteed iff SOME table's literal count sits outside the
            # primary hint's whole divergence band (literal rows are realized
            # verbatim; the primary realization can never leave the band).
            for table, hint in primary.scale.items():
                if table not in counterfactual.literal_rows:
                    continue
                literal = len(counterfactual.literal_rows[table])
                if hint < REALIZED_DIVERGENCE_MIN_SCALE:
                    low = high = hint
                else:
                    span = hint * REALIZED_DIVERGENCE_MAX_PCT // 100 + 1
                    low, high = hint - span, hint + span
                if not (low <= literal <= high):
                    echo_expected[PopulationName.COUNTERFACTUAL] = False
                    break
        cases.append(
            _el_case(
                "fabricate_counts_primary_echo",
                "The frozen PRIMARY count vector submitted while grading every "
                "OTHER population — the solver that solved primary honestly "
                "once and echoes the memorized answer. It keeps full reward "
                "on primary (it IS the answer there) and on resampled (the "
                "memorization pair shares a scale by construction, so the "
                "vectors are identical — that pair carries ZERO extract-load "
                "signal); what it measures is the kill on every population "
                "whose data actually differs, the counterfactual first.",
                directive="fabricate_counts:primary",
                kind_name="fabricate_counts",
                expected_pass=echo_expected,
                required=bool(echo_expected),
            )
        )
    return cases


def el_attack_cases(
    tables: tuple[TableSpec, ...] = (),
    *,
    backend_assignments: tuple[object, ...] = (),
    backends: int = 1,
    policy: str = POLICY_CONSTRUCTED,
    populations: tuple[PopulationSpec, ...] = (),
) -> tuple[AttackCase, ...]:
    """The extract-load mutants this task can HONESTLY declare.

    `partial_backend` and `duplicate_on_load` are REQUIRED everywhere (their kill
    needs no data assumption) and `skip_extraction` once a second backend exists.
    Everything else is data-dependent: declared informational (required=False)
    wherever the schema offers the surface, promoted only by MEASUREMENT.
    """
    constructed = policy == POLICY_CONSTRUCTED
    everywhere = dict.fromkeys(
        _EL_ALL_POPULATIONS if constructed else _EL_GENERATED_POPULATIONS, False
    )
    backend_of = {
        getattr(a, "table", ""): getattr(getattr(a, "backend", None), "value", "")
        for a in backend_assignments
    }

    cases: list[AttackCase] = [
        _el_case(
            "partial_backend",
            "One source table is never loaded at all — the connector that was "
            "never configured. compare_stage1 requires every expected table to "
            "be PRESENT in the warehouse, so the missing table reports 'table "
            "not found' and the strict-binary extract-load reward is 0.0 on "
            "every population, whatever the row counts are.",
            expected_pass=everywhere,
            required=True,
        ),
        _el_case(
            "duplicate_on_load",
            "The load is run twice without truncating first — the classic "
            "idempotency bug. Every table lands 2N rows where N was expected, "
            "and 2N != N for every N >= 1, so the count vector breaks on every "
            "table that has any rows.",
            expected_pass=everywhere,
            required=True,
        ),
    ]
    backend_values = set(backend_of.values())
    if backend_values & {"rest", "s3", "postgres"}:
        cases.append(
            _el_case(
                "truncate_table",
                "Only the FIRST unit of one table is read: page_0001.json of a "
                "paginated REST fixture, the first S3 part, the first INSERT "
                "batch of a postgres load script — the pagination cursor is "
                "never followed. INERT wherever the table fits in one unit "
                "(REST pages hold 100 rows, postgres batches 500, S3 parts "
                "5000), which is why this is declared as a probe with NO "
                "expectation at all: the schema cannot tell how many rows a "
                "population will hold, and a bet the schema cannot cover is "
                "the kind of claim this catalogue exists to remove. "
                "Measurement (tools/prove_el_attacks.py) is what may promote "
                "it, and the mutation itself fails closed when no table spans "
                "more than one unit.",
                expected_pass={},
                required=False,
            )
        )
    if "files" in backend_values:
        # REQUIRED only where a text-only CSV exists: there the header row LOADS
        # and N+1 != N for every N. With a typed column it dies in coercion
        # instead — a crash-kill, which the required-mutants gate rejects as
        # extract-load evidence — so the case stays informational.
        text_only_files = sorted(
            t.name
            for t in tables
            if backend_of.get(t.name) == "files"
            and all(c.type is ColumnType.TEXT for c in t.columns)
        )
        cases.append(
            _el_case(
                "header_as_row",
                "A FILES-backed CSV header row is ingested as data: the table "
                "lands N+1 rows. The cheapest possible proof that the "
                "extract-load reward is EXACT rather than tolerant — REQUIRED "
                "wherever a text-only CSV exists"
                + (
                    f" ({', '.join(text_only_files)}: every column is TEXT, so "
                    "the header row loads and N+1 != N holds for EVERY N, "
                    "including 0, with no assumption about the data)."
                    if text_only_files
                    else (
                        "; on this task every FILES table has a typed column, so "
                        "the header row cannot LOAD through the trusted loader "
                        "(the coercion crash is not a count kill) — declared as an "
                        "informational probe that records inapplicable."
                    )
                ),
                expected_pass=dict(everywhere) if text_only_files else {},
                required=bool(text_only_files),
            )
        )
    assigned = list(backend_of.values())
    if any(assigned.count(b) > 1 for b in set(assigned)):
        cases.append(
            _el_case(
                "wrong_source_file",
                "Two tables that share a backend have their load steps pointed "
                "at each other's artifact — the copy-paste path error. Only "
                "observable between tables whose row counts DIFFER: "
                "compare_stage1 grades counts, so swapping two equal-count "
                "tables scores 1.0 and is a leak, not a kill. The mutation "
                "picks the same-backend pair with the largest count gap and "
                "fails closed when no such pair exists.",
                expected_pass={PopulationName.PRIMARY: False},
                required=False,
            )
        )
    if any(c.nullable for t in tables for c in t.columns):
        cases.append(
            _el_case(
                "null_row_drop",
                "Rows containing any NULL are dropped during the load — the "
                "`dropna` bug. Kills on the populations whose declared "
                "conditions put NULLs in optional foreign keys (primary, "
                "resampled, stress) and is INERT on development, which "
                "declares 'no NULL foreign keys' — the first EL mutant with a "
                "NON-uniform expectation, which is what makes it evidence "
                "about population STRUCTURE rather than about counting.",
                expected_pass={PopulationName.PRIMARY: False},
                required=False,
            )
        )
    cases.extend(_fabricate_cases(populations, constructed))
    cases.append(
        _el_case(
            "stale_snapshot",
            "Population P is graded against a cached extract of a DIFFERENT "
            "population — the stale-snapshot bug. The mutation picks, per "
            "population, the first other population whose frozen count vector "
            "differs, and fails closed when none does. Note the primary/"
            "resampled pair can never serve: validate_population_coverage "
            "forces them to share a scale, so the memorization population "
            "carries ZERO extract-load signal.",
            expected_pass={PopulationName.PRIMARY: False},
            required=False,
        )
    )
    return tuple(cases)


def derive_attack_cases(
    shapes: tuple[StarShape, ...],
    *,
    backends: int = 1,
    policy: str = POLICY_CONSTRUCTED,
    tables: tuple[TableSpec, ...] = (),
    backend_assignments: tuple[object, ...] = (),
    populations: tuple[PopulationSpec, ...] = (),
) -> tuple[AttackCase, ...]:
    """The attack catalogue a schema-derived task can HONESTLY declare.

    One case per discrimination the counterfactual was constructed to make, plus
    the plan-level degenerates; nothing is declared the plan offers no surface
    for. `mutation` is empty on purpose — attacks.py compiles it from the case's
    kind over the task's own reference SQL, so adapters ship no SQL.
    """
    P = PopulationName
    constructed = policy == POLICY_CONSTRUCTED
    cases: list[AttackCase] = []

    claims: list[str] = []
    for shape in shapes:
        for claim in shape.attack_claims:
            if claim not in claims:
                claims.append(claim)
    el_cases = el_attack_cases(
        tables,
        backend_assignments=backend_assignments,
        backends=backends,
        policy=policy,
        populations=populations,
    )

    if claims:
        cases.extend(_claimed_cases(tuple(claims)))
        cases.extend(_degenerate_cases(backends, policy))
        cases.extend(el_cases)
        return tuple(cases)

    # Library shapes certify their childless-parent witness structurally.
    # Recovered plans instead record the exact LEFT edges they compiled; their
    # claim is admitted only when the FINAL counterfactual data actually holds
    # an unmatched left row on one of those edges. One edge is sufficient because
    # the mutant replaces every LEFT JOIN in the task in one pass.
    counterfactual = next(
        (
            population
            for population in populations
            if population.name is P.COUNTERFACTUAL
        ),
        None,
    )
    join_witnessed = not constructed
    if constructed:
        for shape in (shape for shape in shapes if shape.has_join):
            edges = tuple(getattr(shape, "join_edges", ()))
            if edges:
                if counterfactual is not None and any(
                    _edge_has_unmatched_left_row(edge, counterfactual.literal_rows)
                    for edge in edges
                ):
                    join_witnessed = True
                    break
            elif getattr(shape, "join_counterfactual_witness", True):
                join_witnessed = True
                break
    if any(s.has_join for s in shapes) and join_witnessed:
        cases.append(
            AttackCase(
                name="inner_join",
                kind=AttackKind.INNER_JOIN,
                description=(
                    "Every LEFT JOIN replaced by INNER JOIN: left-side rows with no "
                    "matching right-side rows disappear instead of being retained "
                    "under the mart's declared grain."
                    + (
                        " The counterfactual contains an unmatched left-side row "
                        "on a certified plan edge — the one population this case "
                        "claims."
                        if constructed
                        else " The counterfactual carves children away from a slice "
                        "of real parents, so unmatched parents genuinely occur there."
                    )
                ),
                # ONLY the counterfactual, under either policy: the generator
                # carves childless parents solely through a fact's FIRST-declared
                # optional link, which the star need not use, so a claim about
                # generated data would be a bet.
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )
    distinct_measures = [c for s in shapes for c in s.distinct_measures]
    if any(s.fact_dedupe for s in shapes) or distinct_measures:
        #: TWO surfaces, ONE mutant: `attacks._apply_kind(NO_DEDUP)` unwraps
        #: COUNT(DISTINCT) and strips SELECT DISTINCT in one pass, so two cases
        #: would compile two byte-identical mutants. The distinct surface is
        #: needed because PK-less tables (the dedupe one) are rare.
        surfaces: list[str] = []
        if any(s.fact_dedupe for s in shapes):
            surfaces.append(
                "DISTINCT dropped from the dedupe step, so byte-identical "
                "duplicate child rows are counted twice"
            )
        if distinct_measures:
            surfaces.append(
                "DISTINCT dropped from "
                + ", ".join(sorted(set(distinct_measures)))
                + ", so a repeated non-NULL value within one group counts twice"
            )
        # The counterfactual claim needs the constructed policy AND a shape whose
        # counterfactual really holds the duplicate witness; a RECOVERED plan is
        # constructed-policy but witness-less, so it must claim stress instead.
        cf_witnessed = constructed and all(
            s.dedupe_counterfactual_witness
            for s in shapes
            if s.fact_dedupe or s.distinct_measures
        )
        cases.append(
            AttackCase(
                name="no_dedup",
                kind=AttackKind.NO_DEDUP,
                description=(
                    "; ".join(surfaces)
                    + "."
                    + (
                        " Witnessed by the counterfactual's byte-identical duplicate "
                        "rows, same-child fan-out, or dedicated repeated-measure "
                        "group, according to the declared DISTINCT surface."
                        if cf_witnessed
                        else (
                            " This recovered plan's counterfactual constructs no "
                            "duplicate witness; the stress population carries "
                            "duplicated source rows by generation policy, so "
                            "stress is the population this case may claim."
                            if constructed
                            else " The stress population replicates a deterministic "
                            "slice of the real leaf rows; the counterfactual does "
                            "not, so stress is the population this case may claim."
                        )
                    )
                ),
                expected_pass=(
                    {P.COUNTERFACTUAL: False} if cf_witnessed else {P.STRESS: False}
                ),
            )
        )
    # `filtered_measures` reaches this arm only from RECOVERED plans (the library
    # declares claims and early-returns above). The default arm unwraps CASE-
    # inside-aggregate; `filter_to_where` is the row-count error on that surface.
    filtered_measures = sorted({c for s in shapes for c in s.filtered_measures})
    if filtered_measures:
        from elt_taskgen.verification.attacks import (  # local: avoid an import cycle
            KIND_DIRECTIVE_PREFIX,
        )

        listed = ", ".join(filtered_measures)
        cases.append(
            AttackCase(
                name="dropped_filter",
                kind=AttackKind.DROPPED_FILTER,
                description=(
                    "The CASE guard inside every filtered aggregate is unwrapped, "
                    f"so {listed} count EVERY row instead of only the rows their "
                    "predicate selects. Row counts are unchanged — the error is in "
                    "the VALUES, observable wherever rows outside the predicate "
                    "exist, and the counterfactual is constructed to contain them."
                ),
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )
        cases.append(
            AttackCase(
                name="dropped_filter__filter_to_where",
                kind=AttackKind.DROPPED_FILTER,
                description=(
                    "The filtered aggregates' shared predicate is hoisted into the "
                    f"WHERE clause, so rows outside it vanish from {listed} AND "
                    "from every unguarded measure beside them. The strictly "
                    "different error from the unwrap above: groups lose ROWS, not "
                    "just values, so grains thin out or disappear."
                ),
                mutation=KIND_DIRECTIVE_PREFIX + "dropped_filter@filter_to_where",
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )

    if any(s.null_capable_measures for s in shapes):
        cases.append(
            AttackCase(
                name="no_null_default",
                kind=AttackKind.NO_NULL_DEFAULT,
                description=(
                    "COALESCE dropped: a parent row with no matching child rows "
                    "reports NULL instead of the declared default. Observable on "
                    "the counterfactual's childless parent row (the constructed "
                    "witness, and the one population this case claims — a "
                    "childless parent on primary exists only through a fact's "
                    "first-declared optional link, which the star need not use)."
                ),
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )

    ratio_measures = [c for s in shapes for c in s.ratio_measures]
    if ratio_measures:
        cases.append(
            AttackCase(
                name="wrong_denominator",
                kind=AttackKind.WRONG_DENOMINATOR,
                description=(
                    "Every guarded division's denominator replaced by 1, so "
                    + ", ".join(sorted(set(ratio_measures)))
                    + " report the raw numerator instead of a share. The "
                    "counterfactual's control parent has a numerator that is "
                    "strictly positive and strictly smaller than its denominator, "
                    "which is what makes the substitution visible; a parent whose "
                    "numerator is 0 would score the same either way. WRONG_DENOMINATOR "
                    "has had a mutation rule and a surface declaration since round 1 "
                    "and ZERO construction sites — this is the construct that "
                    "instantiates it."
                ),
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )

    # A UNIT-CONVERSION divisor sits inside an AGGREGATE-classified expression,
    # so `ratio_measures` never sees it. Guarded on `not ratio_measures` because
    # the ratio branch declares the same case name; the surfaces are disjoint.
    constant_divisors = sorted(
        {c for s in shapes for c in s.constant_divisor_measures}
    )
    if constant_divisors and not ratio_measures:
        cases.append(
            AttackCase(
                name="wrong_denominator",
                kind=AttackKind.WRONG_DENOMINATOR,
                description=(
                    "Every division's denominator replaced by 1, so "
                    + ", ".join(constant_divisors)
                    + " report their raw unscaled numerator instead of the "
                    "converted unit. A constant divisor makes every non-zero "
                    "group wrong by the same factor, so any population with a "
                    "non-zero measure value observes it; the counterfactual's "
                    "constructed rows carry non-zero measures by design."
                ),
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )

    ranked = [c for s in shapes for c in (s.ranked_measures + s.window_measures)]
    if ranked:
        cases.append(
            AttackCase(
                name="wrong_window",
                kind=AttackKind.WRONG_WINDOW,
                description=(
                    "ORDER BY stripped from inside every OVER(), then PARTITION BY "
                    "from any window that had no order: "
                    + ", ".join(sorted(set(ranked)))
                    + " stop being functions of the input. The counterfactual's "
                    "control parent lists its winner LAST (its LOWER measure, or "
                    "its EARLIER period, comes first); a mirror parent lists its "
                    "winner FIRST (the label that sorts first under the declared "
                    "case-sensitive text order leads a measure tie, or the LATER "
                    "period is listed first). An unordered window has only a "
                    "POSITION to go on, and no fixed position is right on both. "
                    "WRONG_WINDOW also had a rule, a surface and zero "
                    "construction sites."
                ),
                expected_pass={P.COUNTERFACTUAL: False},
            )
        )

    cases.append(
        AttackCase(
            name="hardcoded_primary_outputs",
            kind=AttackKind.CONSTANTS,
            description=(
                "Emits the frozen primary-population outputs verbatim, computing "
                "nothing. The counterfactual's rows are literal and minted from a "
                "disjoint value base, so they cannot coincide with primary."
            ),
            mutation=_HARDCODE_PRIMARY_DIRECTIVE,
            expected_pass={
                P.PRIMARY: True,
                P.COUNTERFACTUAL: False,
                P.DEVELOPMENT: False,
            },
        )
    )
    cases.append(
        AttackCase(
            name="keys_only",
            kind=AttackKind.KEYS_ONLY,
            description=(
                "Emits the grain columns with constant measures: the mart's whole "
                "information content is the measures."
            ),
            expected_pass={P.PRIMARY: False},
            required=False,
        )
    )
    cases.append(
        AttackCase(
            name="no_op",
            kind=AttackKind.NO_OP,
            description="Submits nothing at all.",
            expected_pass={P.PRIMARY: False},
            required=False,
        )
    )
    if backends > 1:
        cases.append(_skip_extraction_case(backends, policy))
    cases.extend(el_cases)
    return tuple(cases)


#: One prose line per witness. The counterfactual's `conditions` are the ONLY
#: place the constructed rows are explained, so every witness needs a line.
_WITNESS_PROSE: dict[str, str] = {
    WITNESS_CONTROL: (
        "row A (control): two distinct linked rows, one passing the filter "
        "predicate and one failing it, with the LOWER measure listed first."
    ),
    WITNESS_CHILDLESS: (
        "row B: NO linked rows at all — separates LEFT from INNER, and a present "
        "null-default from a missing one."
    ),
    WITNESS_DUPLICATE: (
        "row C: byte-identical duplicate linked rows — separates DISTINCT from no "
        "dedupe."
    ),
    WITNESS_SAME_CHILD: (
        "row D: TWO links pointing at the SAME child — separates COUNT from "
        "COUNT(DISTINCT), and is the fan-out a wrong grain is observable on."
    ),
    WITNESS_ALL_FAIL: (
        "row E: every linked row FAILS the predicate, so the filtered measure is 0 "
        "while the total is positive — separates a CASE inside the aggregate from "
        "a WHERE, which would delete the row instead of reporting 0."
    ),
    WITNESS_TIE: (
        "row F: two linked rows with an EXACT tie in the measure and different "
        "labels; the label that sorts first "
        + TEXT_ORDER_PROSE
        + " is listed first — the only row on which a dropped tie-break is "
        "observable, and the MIRROR of row A (whose winner is listed last), so "
        "an unordered window has no fixed position it can guess right on both."
    ),
    WITNESS_OUT_OF_DOMAIN: (
        "row G: a legal value of the domain column that the CASE ladder "
        "deliberately does not name — falsifies a missing ELSE branch."
    ),
    WITNESS_LATEST_FIRST: (
        "row P: the same entity in two periods with the LATER period listed "
        "FIRST — the MIRROR of row A on the period axis (whose later period is "
        "listed last), so an unordered window has no fixed position it can "
        "guess right on both."
    ),
    WITNESS_ON_THRESHOLD: (
        "row H: a value landing EXACTLY on a declared threshold — separates '>' "
        "from '>=' at that boundary."
    ),
    WITNESS_BRIDGE_NO_CHILD: (
        "row I: a link whose child row is MISSING — kills a second-hop inner "
        "join, which is a strictly different error from row B's first-hop one."
    ),
    WITNESS_SECOND_PERIOD: (
        "row J: the same entity in TWO different periods — without it a "
        "period-over-period delta is a constant and a running total is the total."
    ),
    WITNESS_BELOW_THRESHOLD: (
        "row K: exactly one fewer linked row than the declared grouped "
        "threshold — the row is present before aggregation but must be absent "
        "after the inclusive aggregate filter."
    ),
    WITNESS_NULL_MEASURE: (
        "row L: TWO real linked rows whose measure values are BOTH NULL — "
        "separates a non-empty all-NULL group from row B, which has no linked "
        "rows at all."
    ),
    WITNESS_ARGMAX_CASE_ORDER: (
        "ARGMAX-H case-order witness: two linked rows tie exactly on the ordering "
        "measure, while their text labels make plain case-sensitive stored-text "
        "ordering choose a different winner from LOWER(label) ordering."
    ),
    WITNESS_ARGMAX_NULL_ORDER: (
        "ARGMAX-J NULL-order witness: two linked rows tie exactly on the ordering "
        "measure, one with a label and one with NULL, so NULLS LAST and NULLS "
        "FIRST choose different winners."
    ),
    WITNESS_DISTINCT_MEASURE: (
        "row O: TWO distinct real linked rows in one group carry the SAME "
        "non-missing measure value — separates COUNT(DISTINCT measure) from "
        "COUNT(measure)."
    ),
}

# Legacy ``build_star`` shapes may have no nullable aggregate at all.  Their
# row B proves only retained-parent join semantics, so reusing the modern
# catalogue sentence would falsely claim a null-default discriminator too.
_LEGACY_CHILDLESS_PROSE = (
    "row B: at least one anchor row has NO matching linked rows; replacing "
    "this mart edge's retained-parent join with an inner-only match drops "
    "that output row."
)


def _witness_scope_prefix(shape: StarShape) -> str:
    """Stable public scope for every condition emitted for one shape.

    A task can contain several marts whose witness catalogues use the same row
    letters.  Keeping the mart, shape, anchor and both possible join hops on
    *every* line makes ``row B`` (and the nullable adjuncts) a claim about one
    exact mart edge instead of an apparently task-global row.
    """

    anchor, _key, bridge, _fk = shape.witness_anchor()
    return (
        f"WITNESS SCOPE [mart={shape.mart}; shape={shape.shape_name}; "
        f"anchor={anchor}; bridge={bridge or '(none)'}; "
        f"child={shape.child or '(none)'}]: "
    )


def witness_conditions(
    shape: StarShape,
    *,
    tables: tuple[TableSpec, ...] = (),
    relationships: tuple[Relationship, ...] = (),
) -> tuple[str, ...]:
    """The counterfactual's conditions for a plan-library shape: one line per
    constructed witness row, in construction order.

    ``tables`` lets the prose record the nullable-label arm of row A exactly
    when construction planted it. It is optional for compatibility with
    callers that only render the shape-level witness catalogue.
    """
    effective_tables = _narrow_for_minting(tables, relationships)
    anchor, _key, bridge, _fk = shape.witness_anchor()
    scope = _witness_scope_prefix(shape)
    lines = [
        scope + "CONSTRUCTED literal rows, derived from the shape by "
        "generation/populations.py::witness_literal_rows.",
        scope
        + f"Anchor rows live in {anchor}; their linked rows live in {bridge or '(none)'}"
        + (f", fanning out onto {shape.child}." if shape.child else "."),
    ]
    for witness in _effective_witnesses(
        effective_tables, shape, relationships
    ):
        lines.append(scope + _WITNESS_PROSE[witness])
        if witness == WITNESS_CONTROL and _control_winner_label_is_nullable(
            effective_tables, shape, relationships
        ):
            lines.append(
                scope
                + "row A nullable-label boundary: the HIGHER-measure winning "
                f"row carries NULL in {shape.roles.label}; the lower-measure "
                "alternative carries a non-NULL label."
            )
        if witness == WITNESS_NULL_MEASURE:
            _anchor, _key, bridge, _fk = shape.witness_anchor()
            lines.append(
                scope
                + "row L nullable-measure boundary: every linked "
                f"{bridge}.{shape.roles.measure} value is NULL; its real "
                f"{shape.roles.link_key} values distinguish those rows from "
                "a no-link placeholder."
            )
        if witness == WITNESS_ARGMAX_CASE_ORDER:
            labels = _argmax_case_order_labels(
                _argmax_label_column(effective_tables, shape)
            )
            if labels is not None:
                binary_winner, lower_winner = labels
                lines.append(
                    scope
                    + f"ARGMAX-H case-order values: tied {bridge}."
                    f"{shape.roles.label} values are {binary_winner!r} and "
                    f"{lower_winner!r}; plain case-sensitive stored-text ASC "
                    f"selects {binary_winner!r}, while LOWER("
                    f"{shape.roles.label}) ASC selects {lower_winner!r}."
                )
        if witness == WITNESS_ARGMAX_NULL_ORDER:
            kept = _argmax_null_order_label(
                _argmax_label_column(effective_tables, shape)
            )
            if kept is not None:
                lines.append(
                    scope
                    + f"ARGMAX-J NULL-order values: tied {bridge}."
                    f"{shape.roles.label} values are {kept!r} and NULL; "
                    f"{shape.roles.label} ASC NULLS LAST selects {kept!r}, "
                    "while ASC NULLS FIRST selects the NULL-labelled row."
                )
    return tuple(lines)


def _legacy_witness_conditions(
    shape: StarShape,
    literal_rows: Mapping[str, Sequence[Row]],
) -> tuple[str, ...]:
    """Scoped evidence for the legacy ``build_star`` row constructor.

    Legacy stars predate the explicit witness catalogue, so ``witnesses`` and
    ``join_edges`` are both empty even though ``counterfactual_literal_rows``
    deterministically constructs row B (and, for a deduping fact, row C).  A
    mixed task used to omit those facts because any catalogue-backed shape made
    the task-global fallback unreachable.  Render the legacy facts per shape,
    and advertise row B only after the FINAL merged literals prove the exact
    parent-to-fact edge really has an unmatched left row.
    """

    if (
        shape.witnesses
        or shape.join_edges
        or not shape.has_join
        or shape.shape_name != "star"
    ):
        return ()

    anchor, _anchor_key, bridge, _bridge_fk = shape.witness_anchor()
    if not anchor or not bridge:
        return ()

    scope = _witness_scope_prefix(shape)
    lines = [
        scope
        + "CONSTRUCTED literal rows, derived from the legacy star by "
        "generation/populations.py::counterfactual_literal_rows.",
        scope
        + f"Anchor rows live in {anchor}; their linked rows live in {bridge}.",
    ]
    edge = (
        shape.parent,
        tuple(shape.parent_keys),
        shape.fact,
        tuple(shape.fact_link_columns),
    )
    if shape.join_counterfactual_witness and _edge_has_unmatched_left_row(
        edge, literal_rows
    ):
        lines.append(scope + _LEGACY_CHILDLESS_PROSE)

    # The legacy constructor verifies this pair byte-for-byte before returning.
    # Honor an explicit recovered-adapter opt-out just as attack derivation does.
    if shape.fact_dedupe and shape.dedupe_counterfactual_witness:
        lines.append(scope + _WITNESS_PROSE[WITNESS_DUPLICATE])
    return tuple(lines)


def derive_populations_and_attacks(
    *,
    task_id: str,
    tables: tuple[TableSpec, ...],
    relationships: tuple[Relationship, ...],
    shapes: tuple[StarShape, ...],
    scale_hint: dict[str, int] | None = None,
    policy_conditions: tuple[str, ...] = (),
    backends: int = 1,
    backend_assignments: tuple[object, ...] = (),
    policy_conditions_by_population: Mapping[PopulationName, tuple[str, ...]] | None = None,
) -> tuple[tuple[PopulationSpec, ...], tuple[AttackCase, ...]]:
    """The ONE call an adapter makes to become generateable.

    Returns the five populations (counterfactual replaced by CONSTRUCTED literal
    rows) and the attack catalogue those rows target. `policy_conditions` prepend
    to EVERY population, `policy_conditions_by_population` to the named one only,
    so a claim true of some populations is never asserted of all.
    """
    if not shapes:
        raise ValueError(f"task {task_id!r}: no mart shapes — nothing to build a counterfactual around")
    hint = scale_hint if scale_hint is not None else schema_scale_hint(tables, relationships)
    # EVERY mart's star gets its witnesses: a later mart declaring counterfactual
    # kills against unplanted witnesses fails the required-mutants gate later.
    literal = counterfactual_rows_for_shapes(tables, relationships, shapes)
    dangling_condition_lines = _optional_dangling_conditions(
        relationships,
        literal,
        (edge for shape in shapes for edge in shape.join_edges),
        tuple(
            relationship
            for shape in shapes
            if (
                relationship := _shape_owner_relationship(shape, relationships)
            )
            is not None
        ),
    )
    all_null_condition_lines = _all_null_aggregate_conditions(
        tables, relationships, shapes, literal
    )
    round_before_sum_condition_lines = _round_before_sum_conditions(
        tables, relationships, shapes, literal
    )
    witness_condition_lines: list[str] = []
    for shape in shapes:
        if shape.witnesses:
            # Conditions are shape-scoped, so preserve the complete ordered
            # catalogue for every mart.  Task-global text deduplication used to
            # erase (for example) row B for every shape after the first one.
            witness_condition_lines.extend(
                witness_conditions(
                    shape,
                    tables=tables,
                    relationships=relationships,
                )
            )
        else:
            witness_condition_lines.extend(
                _legacy_witness_conditions(shape, literal)
            )
    by_pop = dict(policy_conditions_by_population or {})
    stress_dup_lines = stress_duplicate_conditions(
        tables,
        deduped_tables={s.fact for s in shapes if s.fact_dedupe and s.fact},
        read_tables={x for s in shapes for x in (s.parent, s.fact, s.child) if x},
    )
    populations = []
    for spec in default_populations(
        task_id, hint, tables=tables, relationships=relationships
    ):
        if spec.name is PopulationName.STRESS:
            spec = spec.model_copy(
                update={
                    "conditions": tuple(
                        line
                        for cond in spec.conditions
                        for line in (
                            stress_dup_lines
                            if cond == _STRESS_DUPLICATE_NEUTRAL
                            else (cond,)
                        )
                    )
                }
            )
        if spec.name is PopulationName.COUNTERFACTUAL:
            base_conditions = (
                tuple(witness_condition_lines)
                if witness_condition_lines
                else (
                    "CONSTRUCTED literal rows, derived from the schema by "
                    "generation/populations.py::counterfactual_literal_rows.",
                    f"{shapes[0].parent} row 1: matched child rows — the control.",
                    f"{shapes[0].parent} row 2: NO child rows — distinguishes "
                    "LEFT from INNER, and a missing null-default from a present one.",
                    f"{shapes[0].parent} row 3: byte-identical duplicate child "
                    "rows — distinguishes DISTINCT from no dedupe."
                    if shapes[0].fact_dedupe
                    else f"{shapes[0].parent} row 3: a second matched parent.",
                )
            )
            spec = spec.model_copy(
                update={
                    "literal_rows": literal,
                    "conditions": (
                        base_conditions
                        + dangling_condition_lines
                        + all_null_condition_lines
                        + round_before_sum_condition_lines
                    ),
                }
            )
        prefix = tuple(policy_conditions) + tuple(by_pop.get(spec.name, ()))
        if prefix:
            spec = spec.model_copy(update={"conditions": prefix + spec.conditions})
        populations.append(spec)
    return tuple(populations), derive_attack_cases(
        shapes,
        backends=backends,
        policy=POLICY_CONSTRUCTED,
        tables=tables,
        backend_assignments=backend_assignments,
        populations=tuple(populations),
    )

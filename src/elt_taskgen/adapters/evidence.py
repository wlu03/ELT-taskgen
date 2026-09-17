"""Derive plan-library ``ChainEvidence`` from declared schema facts.

Evidence may use FK edges, keys, types, and value domains, but not published
queries. Partial evidence and shapes that fail to build are rejected.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from elt_taskgen.generation.mart_plan import (
    BuiltPlan,
    ChainEvidence,
    registered_shape_builders,
)
from elt_taskgen.models import (
    ColumnSpec,
    ColumnType,
    MartSpec,
    Relationship,
    TableSpec,
    canonical_json,
    sha256_hex,
)

NUMERIC_TYPES = frozenset(
    {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL}
)
TEMPORAL_TYPES = frozenset({ColumnType.DATE, ColumnType.TIMESTAMP})

#: Two NAMED branches plus one value the ladder deliberately does not name (the
#: out-of-domain witness); fewer leaves the CASE ELSE unfalsifiable.
MIN_LADDER_DOMAIN = 3

#: A filtered aggregate needs values on BOTH sides of its predicate.
MIN_STATUS_DOMAIN = 2

#: Minimum MEASURED hop-1 fan-out for a real-row pool. Three, not two: the
#: plan's DEDUPE projects a DISTINCT, which can collapse two bridge rows into
#: one and leave the fan-out mutants inert.
MIN_OBSERVED_FANOUT = 3


def schema_shape_cluster_id(
    pool: str,
    tables: Sequence[TableSpec],
    rels: Sequence[Relationship],
) -> str:
    """Return the versioned schema cluster from type multisets and FK count."""
    shape = {
        "tables": sorted(
            sorted(column.type.value for column in table.columns)
            for table in tables
        ),
        "fk_count": len(rels),
    }
    return f"{pool}__cluster_{sha256_hex(canonical_json(shape))[:12]}"


# Column role helpers

def _fk_columns(table: str, rels: Sequence[Relationship]) -> set[str]:
    return {c for r in rels if r.child_table == table for c in r.child_columns}


def _ordinary(
    table: TableSpec, rels: Sequence[Relationship]
) -> list[ColumnSpec]:
    """Columns that carry data rather than identity: not PK, not FK."""
    fks = _fk_columns(table.name, rels)
    return [
        c
        for c in table.columns
        if c.name not in fks and c.name not in table.primary_key
    ]


def declared_key_parents(
    tables: Sequence[TableSpec],
) -> frozenset[tuple[str, str]]:
    """Return declared single-column primary and business keys.

    Composite keys are excluded because one component does not preserve grain.
    """
    out: set[tuple[str, str]] = set()
    for t in tables:
        if len(t.primary_key) == 1:
            out.add((t.name, t.primary_key[0]))
        if len(t.business_key) == 1:
            out.add((t.name, t.business_key[0]))
    return frozenset(out)


def _first(cols: Sequence[ColumnSpec], pred: Callable[[ColumnSpec], bool]) -> str:
    for c in cols:
        if pred(c):
            return c.name
    return ""


#: An OBSERVED domain counts only if the column is genuinely categorical (a few
#: values repeated), which is what separates `status` from `description`.
MAX_OBSERVED_DOMAIN = 8
MIN_OBSERVED_REPEAT = 3


def observed_domains(
    tables: Sequence[TableSpec],
    rows: dict[str, Sequence[dict]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Return solver-visible value domains observed in shipped source rows."""
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for table in tables:
        table_rows = rows.get(table.name) or ()
        if len(table_rows) < MIN_OBSERVED_REPEAT * 2:
            continue
        for column in table.columns:
            if column.type is not ColumnType.TEXT:
                continue
            values: dict[str, int] = {}
            for row in table_rows:
                value = row.get(column.name)
                if value is None or not isinstance(value, str) or not value:
                    continue
                values[value] = values.get(value, 0) + 1
                if len(values) > MAX_OBSERVED_DOMAIN:
                    break
            if not 2 <= len(values) <= MAX_OBSERVED_DOMAIN:
                continue
            if min(values.values()) < MIN_OBSERVED_REPEAT:
                continue
            out[(table.name, column.name)] = tuple(sorted(values))
    return out


def link_statistics(
    rels: Sequence[Relationship], rows: dict[str, Sequence[dict]]
) -> dict[tuple[str, str, str, str], tuple[int, int]]:
    """Return maximum fan-out and childless-parent count for each FK edge."""
    stats: dict[tuple[str, str, str, str], tuple[int, int]] = {}
    for rel in rels:
        if len(rel.child_columns) != 1 or len(rel.parent_columns) != 1:
            continue
        child_col, parent_col = rel.child_columns[0], rel.parent_columns[0]
        parent_rows = rows.get(rel.parent_table) or ()
        child_rows = rows.get(rel.child_table) or ()
        counts: dict[object, int] = {
            r.get(parent_col): 0 for r in parent_rows if r.get(parent_col) is not None
        }
        for r in child_rows:
            key = r.get(child_col)
            if key in counts:
                counts[key] += 1
        if not counts:
            continue
        stats[(rel.child_table, child_col, rel.parent_table, parent_col)] = (
            max(counts.values()),
            sum(1 for v in counts.values() if v == 0),
        )
    return stats


def _domain_of(
    column: ColumnSpec, table: str, observed: dict[tuple[str, str], tuple[str, ...]]
) -> tuple[str, ...]:
    """The column's DECLARED domain, else its OBSERVED one, else empty."""
    return tuple(column.enum_values or ()) or observed.get((table, column.name), ())


def _domain_column(
    cols: Sequence[ColumnSpec],
    minimum: int,
    *,
    table: str = "",
    observed: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> tuple[ColumnSpec, tuple[str, ...]] | None:
    """First column with a domain of at least `minimum` values, and that domain."""
    for c in cols:
        domain = _domain_of(c, table, observed or {})
        if len(domain) >= minimum:
            return c, domain
    return None


def _single_key(table: TableSpec) -> str:
    """The table's single-column primary key, or '' when none/composite.

    Silently taking one component of a composite key would change the grain.
    """
    return table.primary_key[0] if len(table.primary_key) == 1 else ""


def _countable_key(
    table: TableSpec,
    rels: Sequence[Relationship] = (),
    *,
    exclude: frozenset[str] = frozenset(),
    prefer: frozenset[str] = frozenset(),
) -> str:
    """Choose a non-null column that counts one value per table row.

    Precedence is numeric single-column PK, any single-column PK, preferred
    numeric non-FK, then numeric FK. The last case sets
    ``bridge_key_is_parent_fk``. ``exclude`` columns are never selected.
    """
    pk = _single_key(table)
    if pk:
        return pk
    fks = _fk_columns(table.name, rels)
    numeric_not_null = [
        c
        for c in table.columns
        if not c.nullable and c.type in NUMERIC_TYPES and c.name not in exclude
    ]
    for column in numeric_not_null:
        if column.name not in fks and column.name in prefer:
            return column.name
    for column in numeric_not_null:
        if column.name not in fks:
            return column.name
    for column in numeric_not_null:
        if column.name in fks:
            return column.name
    return ""


# Chain selection

#: Shape requirements used only for scoring; each shape still validates itself.
_SHAPE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "rollup": (
        "parent", "parent_key", "parent_attr", "bridge", "bridge_key",
        "bridge_parent_fk", "bridge_child_fk", "bridge_status",
        "bridge_status_pass", "bridge_status_fail", "bridge_amount",
        "child", "child_key", "child_label",
    ),
    "top": (
        "parent", "parent_key", "parent_attr", "bridge", "bridge_key",
        "bridge_parent_fk", "bridge_amount", "bridge_label",
    ),
    "bands": (
        "parent", "parent_key", "parent_attr", "parent_domain_column", "domain",
        "out_of_domain", "bridge", "bridge_key", "bridge_parent_fk",
        "bridge_status", "bridge_status_pass", "bridge_status_fail",
    ),
    "by_period": (
        "bridge", "bridge_key", "bridge_parent_fk", "bridge_timestamp",
        "bridge_amount", "bridge_status", "bridge_status_pass",
        "bridge_status_fail", "parent", "parent_key", "parent_attr",
    ),
    "cohorts": (
        "parent", "parent_key", "parent_attr", "bridge", "bridge_key",
        "bridge_parent_fk", "bridge_status", "bridge_status_pass",
        "bridge_status_fail", "bridge_amount",
    ),
    "snapshot": (
        "parent", "parent_key", "parent_attr", "bridge", "bridge_key",
        "bridge_key_is_unique", "bridge_parent_fk", "bridge_amount",
        "bridge_label", "bridge_timestamp",
    ),
    "distribution": (
        "parent", "parent_key", "parent_attr", "bridge", "bridge_key",
        "bridge_parent_fk", "bridge_amount",
    ),
}


def _chain_score(evidence: ChainEvidence) -> tuple:
    """Score a chain by its number of buildable library shapes."""
    funded = tuple(
        int(all(getattr(evidence, f) for f in fields))
        for _, fields in sorted(_SHAPE_REQUIREMENTS.items())
    )
    return (
        sum(funded),
        funded,
        # Deterministic tiebreak, and only a tiebreak.
        evidence.parent,
        evidence.bridge,
    )


def owner_key_nullable(bridge: TableSpec, hop1: Relationship) -> bool:
    """Return whether shipped data can null the hop-1 link column."""
    return (not hop1.required) and all(
        bridge.column(c).nullable for c in hop1.child_columns
    )


def chain_candidates(
    tables: Sequence[TableSpec],
    rels: Sequence[Relationship],
    *,
    observed: dict[tuple[str, str], tuple[str, ...]] | None = None,
    links: dict[tuple[str, str, str, str], tuple[int, int]] | None = None,
    key_parents: frozenset[tuple[str, str]] | None = None,
    exclude_measures: Mapping[str, frozenset[str]] | None = None,
) -> tuple[ChainEvidence, ...]:
    """Return deterministic ``P <- B (<- C)`` chains, best first.

    ``key_parents`` must prove each parent key; ``None`` derives declared keys.
    ``exclude_measures`` prevents identifiers from becoming measures. Column
    roles remain disjoint, and timestamps must be non-null.
    """
    by_name = {t.name: t for t in tables}
    if key_parents is None:
        key_parents = declared_key_parents(tables)
    excluded_by_table: Mapping[str, frozenset[str]] = exclude_measures or {}
    scored: list[tuple[tuple, ChainEvidence]] = []

    for r1 in sorted(
        rels, key=lambda r: (r.parent_table, r.child_table, r.child_columns)
    ):
        parent = by_name.get(r1.parent_table)
        bridge = by_name.get(r1.child_table)
        if parent is None or bridge is None or parent.name == bridge.name:
            continue
        if len(r1.parent_columns) != 1 or len(r1.child_columns) != 1:
            continue
        parent_key = r1.parent_columns[0]
        if (parent.name, parent_key) not in key_parents:
            continue  # grain would not be one row per parent row: refuse
        bridge_parent_fk = r1.child_columns[0]
        excluded = excluded_by_table.get(bridge.name, frozenset())
        bridge_key = _countable_key(bridge, rels, prefer=excluded)
        if not bridge_key:
            continue
        if links is not None:
            # Real-row pool: the hop-1 edge must ACTUALLY fan out and leave a
            # parent childless, else the mutants depending on either are inert.
            fanout, childless = links.get(
                (bridge.name, bridge_parent_fk, parent.name, parent_key), (0, 0)
            )
            if fanout < MIN_OBSERVED_FANOUT or childless < 1:
                continue

        # The referenced parent key is identity for THIS chain even with no
        # declared PK, so it is never the parent attribute or ladder column.
        parent_cols = [c for c in _ordinary(parent, rels) if c.name != parent_key]
        bridge_cols = [c for c in _ordinary(bridge, rels) if c.name != bridge_key]
        parent_attr = _first(parent_cols, lambda c: c.type is ColumnType.TEXT)
        if not parent_attr:
            continue

        # Hop 2: another declared FK out of the SAME bridge, to a different
        # table. Without it COUNT and COUNT(DISTINCT) can never disagree.
        hop2 = [
            r
            for r in sorted(rels, key=lambda r: (r.parent_table, r.child_columns))
            if r.child_table == bridge.name
            and r.parent_table not in (parent.name, bridge.name)
            and len(r.parent_columns) == 1
            and len(r.child_columns) == 1
            and r.child_columns[0] != bridge_parent_fk
            # Same gate as hop 1: a non-key parent here fans the bridge out and
            # inflates every hop-1 measure as well as the DISTINCT count.
            and (r.parent_table, r.parent_columns[0]) in key_parents
        ]
        child = None
        child_key = child_label = bridge_child_fk = ""
        for r2 in hop2:
            candidate = by_name.get(r2.parent_table)
            if candidate is None:
                continue
            label = _first(
                [
                    c
                    for c in _ordinary(candidate, rels)
                    if c.name != r2.parent_columns[0]
                ],
                lambda c: c.type is ColumnType.TEXT,
            )
            if not label:
                continue
            child, child_key, child_label = candidate, r2.parent_columns[0], label
            bridge_child_fk = r2.child_columns[0]
            break

        status = _domain_column(
            bridge_cols, MIN_STATUS_DOMAIN, table=bridge.name, observed=observed
        )
        ladder = _domain_column(
            parent_cols, MIN_LADDER_DOMAIN, table=parent.name, observed=observed
        )
        status_col = status[0] if status is not None else None
        ladder_col = ladder[0] if ladder is not None else None
        # The measure is never the countable key nor a measured identifier.
        amount = _first(
            bridge_cols,
            lambda c, _ex=excluded: c.type in NUMERIC_TYPES and c.name not in _ex,
        )
        # Prefer a label that is NOT the status column: an argmax labelled by
        # the filter's own categorical makes the tie-break prose circular.
        status_name = status_col.name if status_col is not None else ""
        bridge_label = _first(
            bridge_cols,
            lambda c, _s=status_name: c.type is ColumnType.TEXT and c.name != _s,
        ) or _first(bridge_cols, lambda c: c.type is ColumnType.TEXT)
        # Nullable timestamps fund latest_snapshot, but not temporal_grid grains.
        timestamp = _first(
            bridge_cols,
            lambda c: c.type in TEMPORAL_TYPES and not c.nullable,
        ) or _first(bridge_cols, lambda c: c.type in TEMPORAL_TYPES)

        pass_values: tuple[str, ...] = ()
        fail_values: tuple[str, ...] = ()
        if status is not None:
            values = status[1]
            # Split the DECLARED domain, never invent a boundary; both sides
            # then occur, since the generator draws from this same domain.
            pass_values, fail_values = values[:1], values[1:]

        ladder_domain: tuple[str, ...] = ()
        out_of_domain = ""
        if ladder is not None:
            values = ladder[1]
            # The LAST value is the one the ladder does not name, so a dropped
            # CASE ELSE has something to fall through on.
            ladder_domain, out_of_domain = values, values[-1]

        candidate = ChainEvidence(
            parent=parent.name,
            parent_key=parent_key,
            parent_key_type=parent.column(parent_key).type,
            parent_attr=parent_attr,
            parent_attr_type=parent.column(parent_attr).type,
            parent_domain_column=ladder_col.name if ladder_col is not None else "",
            domain=ladder_domain,
            out_of_domain=out_of_domain,
            bridge=bridge.name,
            bridge_key=bridge_key,
            # The key's OWN type travels with it: `_countable_key` may fall
            # back to a text PK, so a shape projecting the key must read it.
            bridge_key_type=bridge.column(bridge_key).type,
            bridge_parent_fk=bridge_parent_fk,
            bridge_child_fk=bridge_child_fk,
            bridge_status=status_col.name if status_col is not None else "",
            bridge_status_pass=pass_values,
            bridge_status_fail=fail_values,
            bridge_amount=amount,
            bridge_amount_type=(
                bridge.column(amount).type if amount else ColumnType.INTEGER
            ),
            bridge_label=bridge_label,
            bridge_timestamp=timestamp,
            child=child.name if child is not None else "",
            child_key=child_key,
            child_label=child_label,
            bridge_needs_dedupe=not bridge.primary_key,
            # Edge statistics gate reverse LEFT-join attacks on realizable unmatched rows.
            child_link_optional=(r2 is not None and not r2.required)
            if child is not None
            else False,
            owner_link_optional=not r1.required,
            # Nullability/role-collision facts the shapes use to DECLINE, not
            # to build: `temporal_grid` refuses a nullable owner link, and
            # `argmax_profile` drops its "which row won" claim on a parent link.
            owner_key_nullable=owner_key_nullable(bridge, r1),
            bridge_key_is_parent_fk=bridge_key == bridge_parent_fk,
            bridge_key_is_unique=(
                (len(bridge.primary_key) == 1 and bridge.primary_key[0] == bridge_key)
                or (
                    len(bridge.business_key) == 1
                    and bridge.business_key[0] == bridge_key
                )
                # Real-row adapters pass their independently measured unique
                # identifier columns here.  They are preferred as countable
                # keys and are the only non-declared keys allowed to identify
                # an extremal row.
                or bridge_key in excluded
            ),
            # `None` when the role is absent: an unselected column's
            # nullability is not a measurement, and False would overstate it.
            bridge_amount_nullable=(
                bridge.column(amount).nullable if amount else None
            ),
            bridge_timestamp_nullable=(
                bridge.column(timestamp).nullable if timestamp else None
            ),
            bridge_label_nullable=(
                bridge.column(bridge_label).nullable if bridge_label else None
            ),
            parent_attr_nullable=parent.column(parent_attr).nullable,
        )
        scored.append((_chain_score(candidate), candidate))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(c for _, c in scored)


def chain_evidence(
    tables: Sequence[TableSpec],
    rels: Sequence[Relationship],
    *,
    observed: dict[tuple[str, str], tuple[str, ...]] | None = None,
    key_parents: frozenset[tuple[str, str]] | None = None,
) -> ChainEvidence | None:
    """The single best chain, or None. Convenience over `chain_candidates`.

    `key_parents=None` means the declared keys, exactly as in
    `chain_candidates` — never a disarmed grain gate.
    """
    candidates = chain_candidates(
        tables, rels, observed=observed, key_parents=key_parents
    )
    return candidates[0] if candidates else None


# Shape selection

#: Auto-selected shapes in registry order; orphan_coverage requires explicit opt-in.
SHAPE_ORDER: tuple[tuple[str, Callable[..., BuiltPlan]], ...] = (
    registered_shape_builders(adapter_auto_select=True)
)


def selectable_shapes(
    evidence: ChainEvidence, *, allow: Sequence[str] | None = None
) -> tuple[tuple[str, Callable[..., BuiltPlan]], ...]:
    """Return preferred shapes that successfully build from this evidence."""
    allowed = set(allow) if allow is not None else None
    out: list[tuple[str, Callable[..., BuiltPlan]]] = []
    for name, fn in SHAPE_ORDER:
        if allowed is not None and name not in allowed:
            continue
        try:
            fn(evidence, mart="probe")
        except ValueError:
            continue
        out.append((name, fn))
    return tuple(out)


def build_marts(
    candidates: Sequence[ChainEvidence],
    *,
    prefix: Callable[[ChainEvidence, str], str],
    max_marts: int = 2,
    allow: Sequence[str] | None = None,
    description: Callable[[str, ChainEvidence], str] | None = None,
    notes: str = "",
    spread_grains: bool = False,
    max_per_shape: int = 1,
) -> tuple[tuple[MartSpec, ...], tuple, tuple[str, ...]]:
    """Build marts in shape order, using the first funding chain per shape.

    Mart columns come only from the plan library contract.
    """
    allowed = set(allow) if allow is not None else None
    marts: list[MartSpec] = []
    shapes: list = []
    names: list[str] = []
    taken: set[str] = set()
    used_parents: set[str] = set()

    # Round-robin `max_per_shape` times so ONE shape may sit on several grains:
    # a pool with a rich FK graph but no declared value domains reaches the
    # column budget only that way.
    rounds = [
        (suffix, fn)
        for _ in range(max(1, max_per_shape))
        for suffix, fn in SHAPE_ORDER
    ]
    for _round_index, (suffix, fn) in enumerate(rounds):
        if len(marts) >= max_marts:
            break
        if allowed is not None and suffix not in allowed:
            continue
        # `spread_grains` prefers an UNUSED parent table: on a wide FK graph,
        # reading several corners is depth, reading one corner is not.
        ordered = (
            sorted(candidates, key=lambda e: e.parent in used_parents)
            if spread_grains
            else list(candidates)
        )
        for evidence in ordered:
            mart_name = prefix(evidence, suffix)
            if mart_name in taken:
                continue
            try:
                built = fn(evidence, mart=mart_name, notes=notes)
            except ValueError:
                continue
            used_parents.add(evidence.parent)
            taken.add(mart_name)
            marts.append(
                MartSpec(
                    name=mart_name,
                    description=(
                        description(suffix, evidence)
                        if description is not None
                        else f"{suffix.replace('_', ' ')} over {evidence.bridge}."
                    ),
                    grain=_grain_prose(suffix, evidence),
                    key_columns=built.shape.key_columns,
                    columns=built.columns,
                    plan=built.plan,
                )
            )
            shapes.append(built.shape)
            names.append(suffix)
            break
    return tuple(marts), tuple(shapes), tuple(names)


def _grain_prose(suffix: str, e: ChainEvidence) -> str:
    if suffix == "by_period":
        return (
            f"One row per ({e.bridge_parent_fk}, calendar month of "
            f"{e.bridge_timestamp}) pair present in {e.bridge}."
        )
    if suffix == "cohorts":
        # State which parents appear when nullable statuses omit groups.
        text = (
            f"One row per ({e.parent_key}, status cohort) pair represented "
            f"among linked {e.bridge} rows, plus one no-activity row for a "
            f"{e.parent} row with no linked {e.bridge} row at all."
        )
        if getattr(e, "bridge_status_nullable", None) is not False:
            text += (
                f" A {e.parent} row whose linked {e.bridge} rows all lack a "
                f"{e.bridge_status} value is in no cohort and gets no "
                "no-activity row, so it has no row in this mart."
            )
        return text
    if suffix == "distribution":
        if e.bridge_amount_nullable is False:
            return (
                f"One row per ({e.parent_key}, measure state) pair represented "
                f"by linked {e.bridge} rows, plus one absent no-activity row for "
                f"a {e.parent} row with no links. Because {e.bridge_amount} is "
                f"required, no linked {e.bridge} row belongs to the absent state."
            )
        return (
            f"One row per ({e.parent_key}, measure state) pair represented "
            f"among linked {e.bridge} rows; the absent state includes missing "
            f"{e.bridge_amount} values and a no-activity row for a {e.parent} "
            f"row with no links. A linked {e.bridge} row whose "
            f"{e.bridge_amount} has a value belongs only to the present state "
            "and never to the absent state."
        )
    return (
        f"One row per {e.parent} ({e.parent_key}), INCLUDING {e.parent} rows "
        f"with no linked {e.bridge} rows."
    )

"""Build the latest-snapshot mart-plan shape."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elt_taskgen.generation.witness import (
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_DUPLICATE,
    WITNESS_LATEST_FIRST,
    WITNESS_SECOND_PERIOD,
)

if TYPE_CHECKING:
    from elt_taskgen.generation.mart_plan import BuiltPlan, ChainEvidence

__all__ = ["latest_snapshot"]


def latest_snapshot(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """One deterministic as-of row per parent, plus lifetime measures.

    This is intentionally not another numeric argmax.  The winning row is the
    latest row by a schema-declared timestamp, with a genuine unique row key as
    the total-order tie-break.  A keyless source is deduplicated byte-for-byte
    before ranking, but only when the schema says exact duplicates are possible.
    """
    e = evidence
    required = [
        "parent",
        "parent_key",
        "parent_attr",
        "bridge",
        "bridge_key",
        "bridge_key_is_unique",
        "bridge_parent_fk",
        "bridge_amount",
        "bridge_label",
        "bridge_timestamp",
    ]
    # Status is a useful payload when the schema declares one, but it is not
    # part of the as-of ordering. Keep it role-distinct when present without
    # inventing it as an admission requirement when absent.
    if e.bridge_status:
        required.append("bridge_status")
    _require(
        e,
        "latest_snapshot",
        *required,
    )
    amount_may_be_missing = e.bridge_amount_nullable is not False
    label_may_be_missing = e.bridge_label_nullable is not False
    order = (
        '"snapshot_at" DESC NULLS LAST, '
        '"snapshot_row_id" ASC NULLS LAST'
    )
    tie_break_prose = f"the smallest {e.bridge_key}" + (
        f" {TEXT_ORDER_PROSE}"
        if e.bridge_key_type is ColumnType.TEXT
        else ""
    )
    dedupe = (e.bridge, ()) if e.bridge_needs_dedupe else ()
    status_carry = (
        ((e.bridge_status, "snapshot_status"),) if e.bridge_status else ()
    )
    status_extrema = (
        (
            Extremum(
                column="latest_status",
                source="snapshot_status",
                type=ColumnType.TEXT,
                description=(
                    f"{e.bridge_status} from that same latest row; '(none)' when "
                    "there are no rows or when the winning value is missing."
                ),
            ),
        )
        if e.bridge_status
        else ()
    )
    return build_rollup(
        mart=mart,
        shape_name="latest_snapshot",
        carried_per_key=True,
        parent=e.parent,
        keys=(
            KeyColumn(
                column="parent_key",
                type=e.parent_key_type,
                description=f"Identifier of the {e.parent} row. One row per value.",
                source=e.parent_key,
            ),
        ),
        passthrough=(
            Passthrough(
                column="parent_name",
                type=e.parent_attr_type,
                description=f"{e.parent_attr} of the {e.parent} row, copied unchanged.",
                source=e.parent_attr,
            ),
        ),
        hops=(
            StarJoin(
                table=e.bridge,
                on_pairs=(("parent_key", e.bridge_parent_fk),),
                carry=(
                    (e.bridge_key, "snapshot_row_id"),
                    (e.bridge_timestamp, "snapshot_at"),
                )
                + status_carry
                + (
                    (e.bridge_amount, "snapshot_amount"),
                    (e.bridge_label, "snapshot_label"),
                ),
                rel_columns=(e.bridge_parent_fk, e.parent_key),
                description=(
                    f"Bring in {e.bridge}; a {e.parent} row with no matching "
                    f"{e.bridge} row is retained and receives the stated empty "
                    "snapshot values."
                ),
            ),
        ),
        measures=(
            Measure(
                column="event_count",
                expr='COUNT("snapshot_row_id")',
                type=ColumnType.BIGINT,
                description=(
                    # The independent implementer counted the LEFT-join
                    # placeholder itself (COUNT(*) = 1 for a childless jobs
                    # row; dlt__workable, batch10 run K, 2026-09-11).
                    f"Number of {e.bridge} rows for this {e.parent} row; "
                    "0 when there are none. "
                    # With reasoning the witness moved from COUNT(*) to
                    # COUNT(*) FILTER (WHERE measure IS NOT NULL): a linked
                    # row with no measure value is still a row (dlt__workable
                    # probe, 2026-09-11).
                    + (
                        f"Every linked {e.bridge} row counts, whether or not it "
                        f"carries {_article(e.bridge_amount)} {e.bridge_amount} value. "
                        if amount_may_be_missing
                        else ""
                    )
                    + f"{_article(e.parent, capitalized=True)} {e.parent} row kept with no "
                    f"{e.bridge} row reports 0 here, never 1: its placeholder "
                    f"holds no {e.bridge} row to count."
                ),
            ),
            Measure(
                column="lifetime_amount",
                expr='SUM("snapshot_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over all matching {e.bridge} rows; "
                    "0 when there are no rows"
                    + (
                        # A MIXED group was unstated: "over all matching rows"
                        # read as NULL propagating to the default (lefty02w,
                        # batch10 run G, 2026-09-11). The sibling argmax
                        # already says rows with no value add nothing.
                        f" and when none of those rows carries an "
                        f"{e.bridge_amount} value; a row with no "
                        f"{e.bridge_amount} value adds nothing, so a group "
                        "with some values sums the values it has"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
                input_nullable=e.bridge_amount_nullable,
            ),
        ),
        extrema=(
            Extremum(
                column="latest_row_id",
                source="snapshot_row_id",
                type=e.bridge_key_type,
                description=(
                    f"{e.bridge_key} of the row with the latest {e.bridge_timestamp}; "
                    f"ties take {tie_break_prose}. It is "
                    f"{_default_prose(e.bridge_key_type)} when there are no rows."
                    + (
                        # Rank every row by timestamp; the amount does not filter
                        # candidates for the latest-row calculation.
                        f" Every {e.bridge} row of the {e.parent} row ranks, "
                        f"whether or not it carries a {e.bridge_amount} value: "
                        f"the latest {e.bridge_timestamp} wins even when that "
                        f"row's {e.bridge_amount} is missing."
                        if amount_may_be_missing
                        else ""
                    )
                ),
            ),
        )
        + status_extrema
        + (
            Extremum(
                column="latest_amount",
                source="snapshot_amount",
                type=e.bridge_amount_type,
                description=(
                    f"{e.bridge_amount} from that same latest row; 0 when there "
                    "are no rows"
                    + (
                        " or when the winning value is missing"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
            Extremum(
                column="latest_label",
                source="snapshot_label",
                type=ColumnType.TEXT,
                description=(
                    f"{e.bridge_label} from that same latest row; '(none)' when "
                    "there are no rows"
                    + (
                        " or when the winning value is missing"
                        if label_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
        ),
        extrema_order_by=order,
        extrema_tie_break="snapshot_row_id",
        extrema_tie_break_prose=tie_break_prose,
        extrema_order_nullable=e.bridge_timestamp_nullable,
        derived=(
            Derived(
                column="latest_amount_share",
                expr=(
                    "COALESCE(ROUND(CAST({latest_amount} AS DOUBLE) / "
                    "NULLIF({lifetime_amount}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "latest_amount divided by lifetime_amount as a fraction, "
                    "rounded to 4 decimal places; 0.0 when lifetime_amount is 0."
                    + (
                        # The numerator is the REPORTED latest_amount, after
                        # its default: a winning row with no value gives 0
                        # over a positive lifetime_amount, hence 0.0 — not a
                        # missing share (lefty02w, batch10 run E).
                        " The division uses latest_amount as this mart reports "
                        "it, after its default of 0 for a winning row whose "
                        f"{e.bridge_amount} is missing, so such a row gives 0.0."
                        if amount_may_be_missing
                        else ""
                    )
                ),
            ),
        ),
        dedupe=dedupe,
        attack_claims=(
            "inner_join",
            "no_null_default",
            "wrong_denominator",
            "wrong_window",
        )
        + (("no_dedup",) if e.bridge_needs_dedupe else ()),
        roles=FactRoles(
            link_key=e.bridge_key,
            measure=e.bridge_amount,
            label=e.bridge_label,
            predicate_column=e.bridge_status,
            predicate_pass=e.bridge_status_pass if e.bridge_status else (),
            predicate_fail=e.bridge_status_fail if e.bridge_status else (),
            period_column=e.bridge_timestamp,
        ),
        # Row P mirrors row A on the period axis: the latest row listed FIRST
        # for one parent and LAST for another, so the wrong_window claim does
        # not rest on which row an unordered window happens to emit first.
        witnesses=(
            WITNESS_CONTROL,
            WITNESS_CHILDLESS,
            WITNESS_SECOND_PERIOD,
            WITNESS_LATEST_FIRST,
        )
        + ((WITNESS_DUPLICATE,) if e.bridge_needs_dedupe else ()),
        grain_description=f"One row per {e.parent} row, keyed by {e.parent_key}.",
        notes=notes,
    )


# Resolve the builder's globals only after its definition so importing this
# module first cannot create an import-time cycle with ``mart_plan``.
from elt_taskgen.generation.mart_plan import (  # noqa: E402
    BuiltPlan,
    ChainEvidence,
    ColumnType,
    Derived,
    Extremum,
    FactRoles,
    KeyColumn,
    Measure,
    Passthrough,
    StarJoin,
    TEXT_ORDER_PROSE,
    _article,
    _default_prose,
    _require,
    build_rollup,
)

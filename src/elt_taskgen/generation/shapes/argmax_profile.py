"""Build the argmax-profile mart-plan shape."""

from __future__ import annotations

from elt_taskgen.generation.witness import (
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_TIE,
)

__all__ = ["argmax_profile"]


def argmax_profile(evidence: ChainEvidence, *, mart: str, notes: str = "") -> BuiltPlan:
    """ARGMAX, not MAX: the LABEL of the row at which a measure is extremal.

    The ORDER BY must carry the tie-break and the shipped description must NAME
    it, or the gold is a function of DuckDB's row order and a correct solver can
    score below 1.0. `top_row_id` ships only when the bridge key is a GENUINE row
    identifier: on a PK-less bridge it is constant per parent, so it would name
    no row and is dropped along with its order term.
    """
    e = evidence
    amount_may_be_missing = e.bridge_amount_nullable is not False
    label_may_be_missing = e.bridge_label_nullable is not False
    key_is_row_id = (
        bool(e.bridge_key)
        and e.bridge_key != e.bridge_parent_fk
        and not e.bridge_key_is_parent_fk
        and e.bridge_key_is_unique
    )
    row_id_tie_break_prose = f"the smallest {e.bridge_key}" + (
        f" {TEXT_ORDER_PROSE}"
        if e.bridge_key_type is ColumnType.TEXT
        else ""
    )
    _require(
        e, "argmax_profile",
        "parent", "parent_key", "parent_attr",
        "bridge", "bridge_key", "bridge_parent_fk", "bridge_amount", "bridge_label",
        allow_shared=(("bridge_key", "bridge_parent_fk"),),
    )
    # "f_id" makes the order TOTAL (two rows can share measure AND label);
    # dropped when it is the parent fk, since it then names no row. NULLS LAST is
    # written out on EVERY term, never inherited: null placement is a session
    # setting, so an unstated order is not a deterministic one.
    order = '"f_measure" DESC NULLS LAST, "f_label" ASC NULLS LAST' + (
        ', "f_id" ASC NULLS LAST' if key_is_row_id else ""
    )
    frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
    row_id_extremum = (
        Extremum(
            column="top_row_id",
            source="f_id",
            # The bridge key's OWN type, read off the schema: a hard-coded
            # BIGINT emits `0` for a TEXT key, which is a bind error. The
            # description derives from the same value, so it cannot drift.
            type=e.bridge_key_type,
            description=(
                f"The {e.bridge_key} of that same extremal row — the winner "
                "under the SAME total order, so it is the identifier of a "
                f"real {e.bridge} row whenever the parent has any. "
                + (
                    f"This includes when none of them carries a "
                    f"{e.bridge_amount} value. "
                    if amount_may_be_missing
                    else ""
                )
                + "It is "
                f"{_default_prose(e.bridge_key_type)} when there are "
                "no rows, and only then. It identifies WHICH row won, so a tie resolved the "
                "wrong way is visible even when two rows share a label."
            ),
        ),
    ) if key_is_row_id else ()
    return build_rollup(
        mart=mart,
        shape_name="argmax_profile",
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
                    (e.bridge_key, "f_id"),
                    (e.bridge_amount, "f_measure"),
                    (e.bridge_label, "f_label"),
                ),
                rel_columns=(e.bridge_parent_fk, e.parent_key),
                description=(
                    f"Bring in {e.bridge}: a {e.parent} row with no {e.bridge} rows "
                    "still appears, with the declared defaults."
                ),
            ),
        ),
        windows=(
            WindowExpr(
                alias="part_max",
                expr=(
                    f'MAX("f_measure") OVER (PARTITION BY "parent_key" '
                    f'ORDER BY "f_measure" DESC {frame})'
                ),
            ),
        ),
        extrema=(
            Extremum(
                column="top_label",
                source="f_label",
                type=ColumnType.TEXT,
                # State every schema-permitted NULL outcome. Omit impossible
                # real-row cases for required sources, but keep empty defaults.
                description=(
                    f"The {e.bridge_label} of the {e.bridge} row with the LARGEST "
                    f"{e.bridge_amount} for this {e.parent} row. Ties in "
                    f"{e.bridge_amount} are broken by taking the SMALLEST "
                    f"{e.bridge_label} {TEXT_ORDER_PROSE}"
                    + (
                        f" — a row with no {e.bridge_label} value sorts after "
                        "every labelled row"
                        if label_may_be_missing
                        else ""
                    )
                    + (
                        f"; rows tied on both are resolved by "
                        f"{row_id_tie_break_prose}. "
                        if key_is_row_id
                        else ". "
                    )
                    + (
                        f"A row with no {e.bridge_amount} value still ranks, after "
                        f"every row that has one, so a parent holding at least one "
                        f"{e.bridge} row always has a winning row — when NONE of "
                        f"its rows carries a {e.bridge_amount} value the winner is "
                        "the one the tie-break alone selects, not the no-rows "
                        "default. "
                        if amount_may_be_missing
                        else ""
                    )
                    + f"The literal '(none)' when the parent has no {e.bridge} "
                    "rows at all"
                    + (
                        f", and '(none)' when the winning row has no "
                        f"{e.bridge_label} value"
                        if label_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
        )
        + row_id_extremum,
        extrema_order_by=order,
        extrema_tie_break="f_label",
        extrema_order_nullable=e.bridge_amount_nullable,
        # The description names the REAL columns; 'f_label'/'f_id' are the
        # compiler's internal aliases and appear only in details.
        extrema_tie_break_prose=(
            f"the smallest {e.bridge_label} {TEXT_ORDER_PROSE}"
            # The winner-selection rule is the one the critic reads for the
            # tie-break; leaving the missing-label placement to the column
            # description alone left it "silent on NULL placement"
            # (lavestima, batch10 run K, 2026-09-11).
            + (
                f" (a row with no {e.bridge_label} value sorts after every "
                "row that has one)"
                if label_may_be_missing
                else ""
            )
            + (f", then {row_id_tie_break_prose}" if key_is_row_id else "")
        ),
        measures=(
            # State the all-null arm when the source permits it: "0 when there
            # are no rows" is silent about a parent whose rows all lack a value,
            # and gold coalesces those to 0 where a literal reading keeps NULL.
            Measure(
                column="top_measure",
                expr='MAX("f_measure")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"The largest {e.bridge_amount} itself; 0 when the parent "
                    f"has no {e.bridge} rows"
                    + (
                        f", and 0 when none of its rows carries a "
                        f"{e.bridge_amount} value"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
                input_nullable=e.bridge_amount_nullable,
            ),
            Measure(
                column="tied_count",
                expr='COUNT(CASE WHEN "f_measure" = "part_max" THEN "f_id" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"How many {e.bridge} rows are tied at that largest "
                    f"{e.bridge_amount}. 1 when exactly one row carries that "
                    f"largest {e.bridge_amount}; 0 when there are no rows"
                    + (
                        f" or when none of the rows carries a "
                        f"{e.bridge_amount} value; a row with no "
                        f"{e.bridge_amount} value never ties: only a row whose "
                        f"{e.bridge_amount} value equals the largest value among "
                        "the parent's rows holds the maximum, so the winning row "
                        "of a parent whose rows all lack a value — the row the "
                        "tie-break alone selects — is not counted here"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
            Measure(
                column="child_count",
                expr='COUNT("f_id")',
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
                column="total_measure",
                expr='SUM("f_measure")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over all of them; 0 when the "
                    f"parent has no {e.bridge} rows"
                    + (
                        f", and 0 when none of its rows carries a "
                        f"{e.bridge_amount} value (rows with no "
                        f"{e.bridge_amount} value add nothing)"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
                input_nullable=e.bridge_amount_nullable,
            ),
        ),
        aggregate_predicate=(
            "a row counts toward tied_count when its measure equals the per-parent "
            "maximum measure; when no row carries a measure there is no maximum "
            "and tied_count is 0"
            if amount_may_be_missing
            else "a row counts toward tied_count when its measure equals the "
            "per-parent maximum measure"
        ),
        derived=(
            Derived(
                column="top_measure_share",
                expr=(
                    "COALESCE(ROUND(CAST({top_measure} AS DOUBLE) / "
                    "NULLIF({total_measure}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "top_measure divided by total_measure as a FRACTION between 0 "
                    "and 1 (not a percentage), rounded to 4 decimals, 0.0 when "
                    "total_measure is 0."
                ),
            ),
            Derived(
                column="tie_state",
                expr=(
                    "CASE WHEN {tied_count} = 0 THEN 'empty' "
                    "WHEN {tied_count} = 1 THEN 'unique' ELSE 'tied' END"
                ),
                type=ColumnType.TEXT,
                # 'empty' is derived from tied_count, so it must be described in
                # tied_count's terms: a parent whose rows all lack a value has
                # tied_count 0 and reports 'empty' while child_count is nonzero.
                description=(
                    "'empty' when no row holds a maximum at all — the parent has "
                    f"no {e.bridge} rows"
                    + (
                        f", or none of its rows carries a {e.bridge_amount} value"
                        if amount_may_be_missing
                        else ""
                    )
                    + " — 'unique' when exactly one row "
                    "holds the maximum, 'tied' when two or more do. Equivalently, "
                    "tie_state follows tied_count: 'empty' when tied_count is 0, "
                    "'unique' when it is 1, 'tied' when it is 2 or more."
                    + (
                        # `empty` describes the measure maximum, not whether
                        # ranking finds a winner. Name only emitted columns and
                        # define the maximum over non-NULL values.
                        f" A row holds the maximum only when it carries a "
                        f"{e.bridge_amount} value equal to the largest "
                        f"{e.bridge_amount} value among the parent's rows; a row "
                        f"with no {e.bridge_amount} value never holds the maximum. "
                        f"So a parent whose {e.bridge} rows all lack a "
                        f"{e.bridge_amount} value has no row holding the maximum "
                        "and is 'empty', with tied_count 0, even though the "
                        "ranking still selects a winning row for it by the "
                        "tie-break alone, which "
                        + ("top_label and top_row_id name" if key_is_row_id else "top_label names")
                        + "."
                        if amount_may_be_missing
                        else ""
                    )
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
        ),
        attack_claims=(
            "inner_join", "no_null_default",
            "dropped_filter", "dropped_filter@filter_to_where",
            "wrong_denominator", "wrong_grain", "wrong_window",
            "custom@wrong_boundary_else",
        ),
        roles=FactRoles(
            link_key=e.bridge_key,
            measure=e.bridge_amount,
            label=e.bridge_label,
        ),
        witnesses=(WITNESS_CONTROL, WITNESS_CHILDLESS, WITNESS_TIE),
        grain_description=f"One row per {e.parent} row, keyed by {e.parent_key}.",
        notes=notes,
    )


# See fan_out_rollup: dependency binding follows the builder definition to keep
# imports safe from either side of the compatibility boundary.
from elt_taskgen.generation.mart_plan import (  # noqa: E402
    BuiltPlan,
    ChainEvidence,
    ColumnType,
    Derived,
    Extremum,
    FactRoles,
    KeyColumn,
    MartColumnKind,
    Measure,
    Passthrough,
    StarJoin,
    TEXT_ORDER_PROSE,
    WindowExpr,
    _article,
    _default_prose,
    _require,
    build_rollup,
)

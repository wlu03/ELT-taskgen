"""Stable identifiers for generated witness scenarios.

The identifiers name input conditions. Population generation creates the rows
that implement each condition.
"""

from __future__ import annotations

WITNESS_CONTROL = "A_control"
WITNESS_CHILDLESS = "B_childless"
WITNESS_DUPLICATE = "C_duplicate"
WITNESS_SAME_CHILD = "D_same_child"
WITNESS_ALL_FAIL = "E_all_fail"
WITNESS_TIE = "F_tie"
WITNESS_OUT_OF_DOMAIN = "G_out_of_domain"
WITNESS_ON_THRESHOLD = "H_on_threshold"
WITNESS_BRIDGE_NO_CHILD = "I_bridge_no_child"
WITNESS_SECOND_PERIOD = "J_second_period"
WITNESS_BELOW_THRESHOLD = "K_below_threshold"
WITNESS_NULL_MEASURE = "L_null_measure"
WITNESS_ARGMAX_CASE_ORDER = "M_argmax_case_order"
WITNESS_ARGMAX_NULL_ORDER = "N_argmax_null_order"
WITNESS_DISTINCT_MEASURE = "O_distinct_measure"
WITNESS_LATEST_FIRST = "P_latest_first"

# Population generation creates one parent row per enabled witness in this
# order. The letter prefixes are stable labels, not priority values.
WITNESS_ORDER: tuple[str, ...] = (
    WITNESS_CONTROL,
    WITNESS_CHILDLESS,
    WITNESS_DUPLICATE,
    WITNESS_SAME_CHILD,
    WITNESS_ALL_FAIL,
    WITNESS_TIE,
    WITNESS_OUT_OF_DOMAIN,
    WITNESS_ON_THRESHOLD,
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_SECOND_PERIOD,
    WITNESS_BELOW_THRESHOLD,
    WITNESS_NULL_MEASURE,
    WITNESS_ARGMAX_CASE_ORDER,
    WITNESS_ARGMAX_NULL_ORDER,
    WITNESS_DISTINCT_MEASURE,
    WITNESS_LATEST_FIRST,
)

# These witnesses place the selected row before its alternative. A
# wrong-window test needs one of them so input position cannot reproduce the
# correct ordered result for every constructed group.
WINDOW_MIRROR_WITNESSES: frozenset[str] = frozenset(
    {WITNESS_TIE, WITNESS_LATEST_FIRST}
)

__all__ = [
    "WITNESS_CONTROL",
    "WITNESS_CHILDLESS",
    "WITNESS_DUPLICATE",
    "WITNESS_SAME_CHILD",
    "WITNESS_ALL_FAIL",
    "WITNESS_TIE",
    "WITNESS_OUT_OF_DOMAIN",
    "WITNESS_ON_THRESHOLD",
    "WITNESS_BRIDGE_NO_CHILD",
    "WITNESS_SECOND_PERIOD",
    "WITNESS_BELOW_THRESHOLD",
    "WITNESS_NULL_MEASURE",
    "WITNESS_ARGMAX_CASE_ORDER",
    "WITNESS_ARGMAX_NULL_ORDER",
    "WITNESS_DISTINCT_MEASURE",
    "WITNESS_LATEST_FIRST",
    "WITNESS_ORDER",
    "WINDOW_MIRROR_WITNESSES",
]

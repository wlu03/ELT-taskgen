"""Counterexamples for choosing groups before or after a strict mean filter."""

from elt_taskgen.generation.recipe_evidence import AGGREGATE_RANK
from elt_taskgen.generation.recipe_witness import recipe_witness_rows


# Each parent uses at most three labels, including for a three-value source enum.
_GROUP_CASES = (
    # Group totals 9, 12 and 120 give two distinct choices below the mean.
    ((1, 3, 5), (2, 4, 6), (30, 40, 50)),
    # Totals 6, 60 and 72 give two distinct choices above the mean.
    ((1, 2, 3), (10, 20, 30), (12, 24, 36)),
    # Ties survive the threshold on either side, exercising label order.
    ((1, 3, 5), (2, 3, 4), (20, 30, 40)),
    ((1, 3, 5), (25, 30, 35), (20, 30, 40)),
    # Two eligible groups tie at their own mean. Including this low ineligible
    # group in the mean incorrectly puts both above it. The base witness also
    # includes a high ineligible group that puts the tied groups below it.
    ((9, 10, 11), (-100, -100), (10, 10, 10)),
)


def discovered_witness_rows(task, evidence, recipe):
    """Mint graph-wide cases with the source's key, type and enum constraints."""
    return recipe_witness_rows(task, evidence.source, AGGREGATE_RANK, extra_group_cases=_GROUP_CASES)

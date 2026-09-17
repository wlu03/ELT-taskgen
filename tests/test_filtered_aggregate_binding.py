"""The filtered-aggregate predicate must BIND, and it must select on values
the schema can actually produce.

WHY THIS EXISTS
Two defects, both reproduced on the dbt reddit_ads task and both systemic:

  1. THE PREDICATE WAS DOCUMENTATION ONLY. `compile_plan_sql` emits no WHERE
     for an aggregate-family op — the filter is the CASE inside each guarded
     measure — so an op-level predicate on an op whose measures are MIXED
     (twelve guarded, five not) is read as an op-WIDE filter and is wrong about
     every unguarded measure. Measured on primary: 96 of 1071 gold cells, and
     650 of 9758 on stress, differ between the reference and a faithful reader
     of the unscoped prose.

  2. THE PREDICATE SELECTED ON VALUES NOTHING COULD PRODUCE. `event_name` had
     no declared domain, so the generator filled it with `event_name_004217`
     and NOTHING equalled `'custom'`, `'lead'` or `'purchase'` in any of the
     five populations: twelve measures shipped a constant 0 behind prose that
     promised a computation.

Neither is repairable by re-authoring, so both are refused BEFORE any council
round: (1) at plan construction (`group_by_op`, `build_rollup`) and again in
the shared op contract (`op_problems`, which `validate_plan` reports and
`compile_plan_sql` raises on); (2) at `generate`, in the population contract.

The rule for (1) is SCOPE, not universality. Requiring every measure to be
guarded would refuse all four existing releases — `argmax_profile` ships
MAX/COUNT/SUM beside one guarded COUNT — and having the compiler apply the
predicate to every measure is undefined for the plan library's predicates
(which are English), fabricates answers no source model computes, and as a
WHERE would delete the childless group whose 0 the construct protects.
"""

from __future__ import annotations

import unittest

from elt_taskgen.adapters import dbt
from elt_taskgen.generation.mart_plan import (
    Measure,
    KeyColumn,
    build_rollup,
    filtered_sum_expr,
    group_by_op,
    guard_literals,
    op_problems,
    predicate_governs,
)
from elt_taskgen.generation.populations import _dead_predicate_problems
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartColumnKind,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    TableSpec,
    TaskIR,
    derive_seed,
)

T = ColumnType
PAID = "\"status\" = 'paid'"

MIXED_MEASURES = (
    ("m_0", "paid_amount", filtered_sum_expr("amount", PAID)),
    ("m_1", "total_amount", 'SUM("amount")'),
)
SCOPED = (
    f"paid_amount counts only rows where {PAID}; total_amount counts every row "
    "of the group"
)


class ScopeIsRequiredWhenMeasuresAreMixed(unittest.TestCase):
    """An op-level predicate that governs SOME measures must say which."""

    def test_group_by_op_refuses_an_unscoped_predicate_over_mixed_measures(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            group_by_op(
                source="joined",
                name="grouped",
                group_by=("customer_id",),
                measures=MIXED_MEASURES,
                predicate=PAID,
            )
        message = str(ctx.exception)
        self.assertIn("total_amount", message)      # the measure it is wrong about
        self.assertIn("paid_amount", message)       # the measure it must name

    def test_group_by_op_accepts_the_scoped_predicate(self) -> None:
        op = group_by_op(
            source="joined",
            name="grouped",
            group_by=("customer_id",),
            measures=MIXED_MEASURES,
            predicate=SCOPED,
        )
        self.assertIs(op.kind, MartOpKind.FILTERED_AGGREGATE)
        self.assertEqual([], op_problems(op))

    def test_all_guarded_needs_no_scope(self) -> None:
        """A predicate that governs EVERY measure is already op-wide and true."""
        op = group_by_op(
            source="joined",
            name="grouped",
            group_by=("customer_id",),
            measures=(MIXED_MEASURES[0],),
            predicate=PAID,
        )
        self.assertEqual([], op_problems(op))

    def test_op_problems_is_the_net_under_hand_built_plans(self) -> None:
        """The shared contract refuses it too, so no plan validates and misleads."""
        op = MartOp(
            kind=MartOpKind.FILTERED_AGGREGATE,
            description="One output row per customer_id.",
            tables=("joined",),
            columns=("customer_id", "paid_amount", "total_amount"),
            predicate=PAID,
            details={
                "group_by": '"customer_id"',
                "m_0": filtered_sum_expr("amount", PAID),
                "m_1": 'SUM("amount")',
            },
        )
        problems = op_problems(op)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("NOT guarded by the op predicate", problems[0])

    def test_build_rollup_names_every_governed_measure(self) -> None:
        """Naming ONE governed measure is not enough when it governs two."""
        keys = (
            KeyColumn(column="parent_key", type=T.TEXT, description="Parent.",
                      source="parent_key"),
        )
        measures = (
            Measure(column="paid_amount", expr=filtered_sum_expr("amount", PAID),
                    type=T.INTEGER, description="Paid amount.", null_default="0"),
            Measure(column="paid_count", expr=f'COUNT(CASE WHEN {PAID} THEN "id" END)',
                    type=T.BIGINT, description="Paid count."),
            Measure(column="total_amount", expr='SUM("amount")', type=T.INTEGER,
                    description="Total amount.", null_default="0"),
        )
        with self.assertRaises(ValueError) as ctx:
            build_rollup(
                mart="m", shape_name="test", parent="parent", keys=keys,
                measures=measures,
                aggregate_predicate=f"paid_amount counts only rows where {PAID}",
                enforce_budget=False,
            )
        self.assertIn("paid_count", str(ctx.exception))

    def test_two_different_guards_cannot_be_stated_by_one_role(self) -> None:
        """`FactRoles` carries ONE predicate_column and ONE predicate_pass, so
        a shape counting passing rows AND failing rows in the same op must say
        so itself rather than have the fallback describe both with the passing
        values."""
        from elt_taskgen.generation.mart_plan import FactRoles

        keys = (
            KeyColumn(column="parent_key", type=T.TEXT, description="Parent.",
                      source="parent_key"),
        )
        measures = (
            Measure(column="passing_count", expr=f'COUNT(CASE WHEN {PAID} THEN "id" END)',
                    type=T.BIGINT, description="Passing."),
            Measure(column="failing_count",
                    expr='COUNT(CASE WHEN "status" = \'failed\' THEN "id" END)',
                    type=T.BIGINT, description="Failing."),
        )
        with self.assertRaises(ValueError) as ctx:
            build_rollup(
                mart="m", shape_name="test", parent="parent", keys=keys,
                measures=measures,
                roles=FactRoles(predicate_column="status", predicate_pass=("paid",)),
                enforce_budget=False,
            )
        self.assertIn("DIFFERENT conditions", str(ctx.exception))

    def test_predicate_governs_is_whole_word(self) -> None:
        """`count` must not match `tied_count` — a substring test is a rubber stamp."""
        self.assertFalse(predicate_governs("a row counts toward tied_count", "count"))
        self.assertTrue(predicate_governs("a row counts toward tied_count", "tied_count"))


def _task_with_guard(literal: str, *, domain: tuple[str, ...] | None,
                     literal_rows: tuple[dict, ...] = ()) -> TaskIR:
    """A one-mart task whose single measure is guarded on `literal`."""
    table = TableSpec(
        name="payments",
        description="One row per payment.",
        columns=(
            ColumnSpec(name="id", type=T.TEXT, description="Key."),
            ColumnSpec(name="status", type=T.TEXT, description="Status.",
                       enum_values=domain),
            ColumnSpec(name="amount", type=T.INTEGER, description="Amount."),
        ),
        primary_key=("id",),
    )
    op = MartOp(
        kind=MartOpKind.FILTERED_AGGREGATE,
        description="One output row per id.",
        tables=("payments",),
        columns=("id", "paid_amount"),
        predicate=f"paid_amount counts only rows where status = '{literal}'",
        details={
            "group_by": '"id"',
            "m_0": f"SUM(CASE WHEN \"status\" = '{literal}' THEN \"amount\" ELSE 0 END)",
        },
    )
    mart = MartSpec(
        name="paid_by_id",
        description="Paid amount per id.",
        grain="One row per id.",
        key_columns=("id",),
        columns=(
            MartColumn(name="id", type=T.TEXT, description="Key.",
                       kind=MartColumnKind.PASSTHROUGH),
            MartColumn(name="paid_amount", type=T.INTEGER, description="Paid amount.",
                       kind=MartColumnKind.AGGREGATED),
        ),
        plan=MartPlan(mart="paid_by_id", ops=(op,)),
    )
    return TaskIR(
        task_id="deadpred__demo",
        family_id="demo__dead_predicate",
        cluster_id="demo__dead_predicate",
        origin=Origin.DEMO,
        license="CC0-1.0",
        title="Dead predicate fixture",
        tables=(table,),
        backends=(BackendAssignment(table="payments", backend=Backend.POSTGRES),),
        marts=(mart,),
        populations=tuple(
            PopulationSpec(
                name=name,
                seed=derive_seed("deadpred__demo", name.value),
                scale={"payments": 10},
                conditions=("synthetic",),
                literal_rows=(
                    {"payments": [dict(r) for r in literal_rows]}
                    if literal_rows and name is PopulationName.COUNTERFACTUAL
                    else {}
                ),
            )
            for name in PopulationName
        ),
    )


class ADeadPredicateIsRefusedBeforeItCostsAnything(unittest.TestCase):
    """A guard nothing can satisfy is a constant column, not a measure."""

    def test_a_literal_no_column_can_produce_is_refused(self) -> None:
        task = _task_with_guard("paid", domain=None)
        problems = _dead_predicate_problems(task)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("'paid'", problems[0])
        self.assertIn("NO column of this task can produce", problems[0])

    def test_a_declared_domain_makes_it_producible(self) -> None:
        task = _task_with_guard("paid", domain=("paid", "refunded"))
        self.assertEqual([], _dead_predicate_problems(task))

    def test_a_planted_literal_row_makes_it_producible(self) -> None:
        """The plan library plants its predicate values as witness rows rather
        than declaring a domain; that has to count as producible too."""
        task = _task_with_guard(
            "paid",
            domain=None,
            literal_rows=({"id": "P1", "status": "paid", "amount": 1},),
        )
        self.assertEqual([], _dead_predicate_problems(task))

    def test_guard_literals_reads_the_ast_not_the_prose(self) -> None:
        task = _task_with_guard("paid", domain=("paid",))
        op = task.marts[0].plan.ops[0]
        self.assertEqual([("m_0", "status", "paid")], guard_literals(op))


class TheDbtAdapterStatesBothHalves(unittest.TestCase):
    """The recovered dbt rollup declares its scope AND its domain."""

    def test_predicate_of_scopes_every_group_and_names_the_rest(self) -> None:
        measures = (
            Measure(column="custom_items",
                    expr="SUM(CASE WHEN event_name = 'custom' THEN total_items ELSE 0 END)",
                    type=T.BIGINT, description="Custom items."),
            Measure(column="lead_items",
                    expr="SUM(CASE WHEN event_name = 'lead' THEN total_items ELSE 0 END)",
                    type=T.BIGINT, description="Lead items."),
            Measure(column="total_items", expr="SUM(total_items)", type=T.BIGINT,
                    description="All items."),
        )
        predicate = dbt._predicate_of(measures)
        self.assertIn("custom_items count only rows where event_name = 'custom'", predicate)
        self.assertIn("lead_items count only rows where event_name = 'lead'", predicate)
        self.assertIn("total_items count every row of the group", predicate)

    def test_predicate_of_is_empty_when_nothing_filters(self) -> None:
        """A plain aggregate must not acquire a predicate it does not apply."""
        measures = (
            Measure(column="total_items", expr="SUM(total_items)", type=T.BIGINT,
                    description="All items."),
        )
        self.assertEqual("", dbt._predicate_of(measures))

    def test_required_domain_reads_equalities_only(self) -> None:
        self.assertEqual(
            [("event_name", "custom")],
            dbt._required_domain("SUM(CASE WHEN event_name = 'custom' THEN x ELSE 0 END)"),
        )
        self.assertEqual(
            [], dbt._required_domain("SUM(CASE WHEN event_name > 'custom' THEN x ELSE 0 END)")
        )

    def test_adopted_domain_puts_the_selected_literals_first(self) -> None:
        """The constructed counterfactual mints by cycling the domain over as
        few as three rows, so a residual sorted into the middle would evict a
        literal the plan selects on."""
        domain = dbt._adopted_domain("event_name", {"purchase", "custom", "lead"})
        self.assertEqual(("custom", "lead", "purchase", "event_name_other"), domain)

    def test_adopted_domain_inhabits_the_negative_class(self) -> None:
        """Without a non-selected value, no group has rows but no qualifying
        rows — and the op's own 'still appears, reporting 0' claim is never
        exercised."""
        domain = dbt._adopted_domain("status", {"paid"})
        self.assertEqual(2, len(domain))
        self.assertIn("paid", domain)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

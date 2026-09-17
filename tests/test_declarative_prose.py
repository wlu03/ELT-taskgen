"""Tests for review/declarative_prose.py — the recipe gate.

WHY THIS EXISTS
The gate exists because the live opus-5 author wrote a numbered SQL procedure
that the leak scan and the completeness gate both certified. Two properties
have to hold at once, and the SECOND one is the one that can quietly ruin the
factory:

  1. RECIPES FAIL. Operator vocabulary — LEFT JOIN, COALESCE, COUNT(DISTINCT,
     GROUP BY, ORDER BY, CASE WHEN, UNION ALL, window syntax, a dotted join
     predicate — is reported with the fragment quoted. The recorded author
     transcript is the primary specimen: it is the actual defect, not a
     constructed one.
  2. HONEST DECLARATIVE PROSE PASSES. Ordinary English collides with SQL
     keywords constantly ("customers who joined in 2020", "in ascending order
     by customer_id", "in the case when a customer has no orders", "the union
     of the two teams", "the count (of completed orders)"). A gate that fires
     on those sends good tasks into SPECIFICATION repair and burns live
     provider spend on repair proposals that cannot succeed. Every false
     positive this suite pins is one that a plausible author would have hit.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.review import declarative_prose as DP

FIXTURES = Path(__file__).parent / "fixtures"
DECLARATIVE_PROSE = (FIXTURES / "declarative_prose.txt").read_text(encoding="utf-8")


#: recipe_prose.txt is a frozen response to older rule and column wording.
#: These maps restore its prompt-era contract so fidelity is measured honestly;
#: never rewrite or re-key the historical fixture.
_RECIPE_ERA_RULES: dict[str, str] = {
    "Keep only orders with status": (
        "Keep only orders with status = 'completed'."
    ),
    "Deduplicate exact-duplicate": (
        "Deduplicate exact-duplicate completed order header rows: "
        "DISTINCT (order_id, customer_id)."
    ),
    "Per-order item total": (
        "Per-order item total: SUM(quantity * unit_price) grouped by order_id."
    ),
    "LEFT JOIN the orders in scope onto customers": (
        "LEFT JOIN completed orders onto customers so customers with "
        "no (completed) orders are retained."
    ),
    "LEFT JOIN per-order item totals": (
        "LEFT JOIN per-order item totals onto the completed orders."
    ),
    "Per customer:": (
        "Per customer: completed_order_count = COUNT(DISTINCT completed "
        "order_id); total_spend = SUM of item totals of completed orders."
    ),
    "COALESCE both measures": (
        "COALESCE both measures to 0 for customers without completed orders "
        "(never NULL)."
    ),
}

#: Mart output-column descriptions as they stood when the specimen was
#: recorded. Attempt 10 made them scope-neutral ("Count of DISTINCT orders in
#: scope") because council._critic_view republishes them under the rules,
#: which restated the stripped FILTER op and kept ambiguity-no-filter
#: resolvable.
_RECIPE_ERA_MART_COLUMNS: dict[str, str] = {
    "completed_order_count": "Count of DISTINCT completed orders; 0 if none.",
    "total_spend": (
        "Sum of quantity * unit_price over items of completed orders; "
        "0 if none."
    ),
}


def _recipe_era_task():
    """demo_task() with customer_summary's plan rolled back to recipe-era text."""
    task = demo_fixture.demo_task()
    mart = task.marts[0]
    ops = tuple(
        op.model_copy(update={"description": replacement})
        if (replacement := next(
            (v for k, v in _RECIPE_ERA_RULES.items() if op.description.startswith(k)),
            None,
        ))
        else op
        for op in mart.plan.ops
    )
    assert sum(
        op.description in _RECIPE_ERA_RULES.values() for op in ops
    ) == len(_RECIPE_ERA_RULES), "recipe-era rollback matched no op — rule text moved"
    plan = mart.plan.model_copy(update={"ops": ops})
    columns = tuple(
        col.model_copy(update={"description": _RECIPE_ERA_MART_COLUMNS[col.name]})
        if col.name in _RECIPE_ERA_MART_COLUMNS
        else col
        for col in mart.columns
    )
    assert sum(
        col.description in _RECIPE_ERA_MART_COLUMNS.values() for col in columns
    ) == len(_RECIPE_ERA_MART_COLUMNS), (
        "recipe-era rollback matched no mart column — column text moved"
    )
    return task.model_copy(
        update={
            "marts": (
                mart.model_copy(update={"plan": plan, "columns": columns}),
            )
            + task.marts[1:]
        }
    )


def _identifiers():
    return DP._task_identifiers(demo_fixture.demo_task())


def _problems(prose: str) -> list[str]:
    return DP.operator_problems(prose, _identifiers())


class HonestProseTest(unittest.TestCase):
    """The precision half. These must never fire."""

    def test_the_declarative_fixture_is_clean(self):
        self.assertEqual(_problems(DECLARATIVE_PROSE), [])

    def test_ordinary_english_that_collides_with_sql_keywords(self):
        for sentence in (
            "Customers who joined in 2020 are in scope like any other customer.",
            "Rows appear sorted in ascending order by customer_id.",
            "In the case when a customer has no completed orders, both "
            "measures read 0.",
            "The count (of completed orders) is an integer.",
            "Report the union of the two teams' customers as one list.",
            "Each order has one item total, and the total is in the same "
            "currency units as unit_price.",
            "Cancelled orders are out of scope, so they contribute to no "
            "customer's totals.",
            "An order whose customer_id is absent belongs to no customer.",
            "Every customer is included in the output, whether or not that "
            "customer has any completed orders.",
            "Count each completed order once, even if its header row repeats.",
            "Orders whose status is completed are the only ones in scope.",
            "The order in which rows appear is deterministic.",
            "Group discounts and partition keys are not part of this project.",
        ):
            self.assertEqual(_problems(sentence), [], sentence)

    def test_value_predicates_are_not_operators(self):
        """Naming the exact filter value is information the solver must have;
        the gate is scoped to relational OPERATORS on purpose."""
        self.assertEqual(
            _problems("Keep only orders with status = 'completed'."), []
        )

    def test_parenthetical_output_field_glosses_are_not_function_calls(self):
        """The live Pipedrive author used ``label (field, explanation)``.

        Although the prefix resembles a spaced COUNT call, the comma is
        followed by ordinary grammatical prose rather than another SQL
        expression.
        """
        identifiers = frozenset({"row_count"})
        for sentence in (
            "Each entity reports row count (row_count, the number of linked "
            "deals_participants rows in the cell).",
            "Each entity reports row count (row_count, which is 0 for a "
            "no-activity cell).",
        ):
            self.assertEqual(
                DP.operator_problems(sentence, identifiers), [], sentence
            )

    def test_distinct_output_noun_phrases_are_not_sql_operators(self):
        """Two TaskIR-derived noun shapes from the saved live failures pass."""
        identifiers = frozenset(
            {"status", "component_types", "distinct_status_count"}
        )
        for sentence in (
            "Each entity reports its distinct status count.",
            "The number of distinct component_types rows is reported.",
            "Report the number of distinct component_types values represented.",
        ):
            self.assertEqual(
                DP.operator_problems(sentence, identifiers), [], sentence
            )

    def test_empty_prose_is_not_this_gate_s_problem(self):
        """Vacuity belongs to prose_fidelity (every declared item is missing);
        this gate must not certify emptiness as a pass by reporting nothing
        that matters — it simply has nothing to say."""
        self.assertEqual(_problems(""), [])
        task = demo_fixture.demo_task().model_copy(update={"solver_prompt": ""})
        self.assertTrue(DP.declarative_prose_gate(task).passed)


class RecipeProseTest(unittest.TestCase):
    """The recall half."""

    def test_the_recorded_author_response_is_a_recipe(self):
        """THE measured evidence, frozen. tests/fixtures/recipe_prose.txt is
        the live claude-opus-5 semantic-author response recorded BEFORE this
        gate existed — verbatim, transcript key
        74723704d432c57f0b1d26ead5830ce837c883dabe3883c45307b3540190fa0e. It
        passed the leak scan (0 findings) and the completeness gate (green)
        and is a numbered SQL procedure. It is kept as a file rather than read
        out of the transcript store because re-recording the author replaces
        that store by design, and this specimen must outlive it."""
        prose = (FIXTURES / "recipe_prose.txt").read_text(encoding="utf-8")
        problems = _problems(prose)
        self.assertTrue(problems, "the recorded author prose was not a recipe")
        # The strengthened TaskIR contract gate also rejects this historical
        # response: it omitted relationship required/optional semantics and
        # structured MartOp requirements that the old description-only check
        # could not see.  Keep the frozen response as evidence for both
        # independent guards rather than preserving its obsolete GREEN label.
        from elt_taskgen.review import prose_fidelity
        task = _recipe_era_task().model_copy(
            update={"solver_prompt": prose}
        )
        completeness = [
            p for p in prose_fidelity.check_prose_fidelity(task)
            if "SQL mechanics" not in p
        ]
        self.assertTrue(any("relationship 1" in p and "optional" in p
                            for p in completeness), completeness)
        self.assertTrue(any("relationship 2" in p and "required" in p
                            for p in completeness), completeness)
        self.assertTrue(any("rule 6" in p and "customer_id" in p
                            for p in completeness), completeness)
        categories = {p.split(":", 1)[0] for p in problems}
        for expected in ("join-operator", "coalesce", "distinct-operator",
                         "function-call"):
            self.assertIn(expected, categories, sorted(categories))

    def test_every_operator_category_fires(self):
        for category, sentence in (
            ("join-operator", "LEFT JOIN completed orders onto customers."),
            ("join-operator", "Join the orders table on customer_id."),
            ("join-operator", "The join preserves the customers side."),
            ("function-call", "Use COALESCE(total_spend, 0) for the measure."),
            ("function-call", "Compute SUM(quantity * unit_price) per order."),
            ("coalesce", "Coalescing the measure to zero is required."),
            ("function-call",
             "completed_order_count = COUNT(DISTINCT order_id)."),
            ("distinct-operator", "Take DISTINCT (order_id, customer_id)."),
            ("distinct-operator", "Count distinct order_id per customer."),
            ("by-clause", "Aggregate the item totals GROUP BY order_id."),
            ("by-clause", "The per-order item total is grouped by order_id."),
            ("by-clause", "Emit the rows ORDER BY customer_id."),
            ("by-clause", "Rank the rows PARTITION BY customer_id."),
            ("case-expression",
             "Use CASE WHEN status = 'completed' THEN 1 ELSE 0 END."),
            ("set-operator", "Stack the two row sets with UNION ALL."),
            ("window-syntax",
             "Number the rows with row_number() OVER (customer_id)."),
            ("window-syntax", "Use ROWS BETWEEN UNBOUNDED PRECEDING and "
                              "CURRENT ROW."),
            ("join-predicate",
             "Match on orders.customer_id = customers.customer_id."),
            ("query-clause", "SELECT customer_id FROM customers."),
            ("query-clause", "Filter WHERE status = 'completed' first."),
        ):
            problems = _problems(sentence)
            self.assertTrue(problems, sentence)
            self.assertTrue(
                any(p.startswith(category) for p in problems),
                f"{sentence!r} -> {problems}",
            )

    def test_parenthetical_gloss_guard_preserves_real_function_calls(self):
        for sentence in (
            "Use COUNT(row_count) for the measure.",
            "Use COUNT (row_count) for the measure.",
            "Use COUNT(DISTINCT row_count) for the measure.",
            "Use COUNT (DISTINCT row_count) for the measure.",
            "Use COALESCE(row_count, 0) for the measure.",
            "Use COALESCE (row_count, 0) for the measure.",
            "Use SUM(row_count) for the measure.",
            "Use MAX (row_count) for the measure.",
            'Use GREATEST (row_count, "the number") for the measure.',
            "Use COUNT (row_count, other_count) for the measure.",
            "Use COUNT (row_count, which is 0) for the measure.",
            "Compute COUNT (row_count, the number of rows) per group.",
            "Each entity reports item count (row_count, the number of rows).",
        ):
            problems = DP.operator_problems(sentence, frozenset({"row_count"}))
            self.assertTrue(
                any(problem.startswith("function-call") for problem in problems),
                f"{sentence!r} -> {problems}",
            )

    def test_distinct_noun_guard_preserves_operator_directives(self):
        identifiers = frozenset(
            {"order_id", "status", "component_types", "distinct_status_count"}
        )
        for sentence in (
            "Use distinct order_id for the result.",
            "Take distinct component_types before reporting.",
            "Count distinct order_id per customer.",
            "SELECT DISTINCT order_id FROM orders.",
            "Take DISTINCT (order_id, status).",
        ):
            problems = DP.operator_problems(sentence, identifiers)
            self.assertTrue(
                any(problem.startswith("distinct-operator") for problem in problems),
                f"{sentence!r} -> {problems}",
            )

    def test_a_determiner_does_not_disguise_a_real_case_expression(self):
        """"Apply A CASE WHEN ... THEN ... END" is how a recipe actually
        writes it, and the English guard for "in THE CASE WHEN a customer
        has no orders" used to swallow the whole finding. THEN *and* END
        together overrule the guard; honest prose never has both."""
        for sentence in (
            "Apply a CASE WHEN status = 'completed' THEN 1 ELSE 0 END flag.",
            "Use the CASE WHEN status = 'completed' THEN 1 ELSE 0 END form.",
            "Each CASE WHEN order_id IS NULL THEN 0 END branch.",
        ):
            problems = _problems(sentence)
            self.assertTrue(problems, sentence)
            self.assertTrue(
                any(p.startswith("case-expression") for p in problems),
                f"{sentence!r} -> {problems}",
            )
        for sentence in (
            "In the case when a customer has no completed orders, both "
            "measures read 0.",
            "In the case when a customer has no orders, then the measures "
            "read 0.",
            "The edge case when an order ends up with no items is that it "
            "still counts.",
        ):
            self.assertEqual(_problems(sentence), [], sentence)

    def test_findings_quote_the_fragment_and_name_a_remedy(self):
        problems = _problems("LEFT JOIN completed orders onto customers.")
        self.assertEqual(len(problems), 1)
        self.assertIn("left join", problems[0])
        self.assertIn("still appear", problems[0])

    def test_backticked_and_uppercased_sql_is_normalized(self):
        """The recorded transcript dressed its join predicate in markdown
        backticks; quoting is not a disguise."""
        self.assertTrue(
            _problems("matching on `orders`.`customer_id` = "
                      "`customers`.`customer_id`")
        )
        self.assertTrue(_problems("LEFT OUTER JOIN the completed orders."))


class GateShapeTest(unittest.TestCase):
    def test_gate_result_is_deterministic_and_named(self):
        task = demo_fixture.demo_task().model_copy(
            update={"solver_prompt": DECLARATIVE_PROSE}
        )
        gate = DP.declarative_prose_gate(task)
        self.assertTrue(gate.passed)
        self.assertEqual(gate.gate, DP.GATE_NAME)
        self.assertEqual(
            gate.to_canonical_json(),
            DP.declarative_prose_gate(task).to_canonical_json(),
        )

    def test_recipe_task_fails_the_gate_with_every_fragment_named(self):
        from elt_taskgen.review import metrology

        task = demo_fixture.demo_task()
        recipe = task.model_copy(
            update={"solver_prompt": metrology.build_prose(task)}
        )
        gate = DP.declarative_prose_gate(recipe)
        self.assertFalse(gate.passed)
        # (An assert comparing evidence["problem_count"] to
        # str(len(check_declarative_prose(recipe))) was removed:
        # the gate BUILDS that field as exactly that expression, so the
        # comparison could not fail.)

    def test_identifier_set_covers_tables_columns_and_marts(self):
        idents = _identifiers()
        for name in ("customers", "orders", "order_items", "customer_id",
                     "unit_price", "customer_summary", "total_spend"):
            self.assertIn(name, idents)


if __name__ == "__main__":
    unittest.main()

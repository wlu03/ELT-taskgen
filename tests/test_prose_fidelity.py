"""Tests for review/prose_fidelity.py and its author-stage wiring.

WHY THIS EXISTS
The prose-fidelity gate is the deterministic guarantee that the solver prose
represents EVERY declared MartSpec item (mart, grain, key columns, output
columns with description substance, every plan rule) AND states them
declaratively (review/declarative_prose.py, composed into the same call).
These tests pin:
  * complete DECLARATIVE prose passes both halves;
  * each metrology ambiguity-injection (dropped tie-break / dropped
    null-default rule) FAILS with the missing rule NAMED;
  * missing marts / columns / grain are named individually;
  * empty prose never passes by vacuity;
  * a rule NEGATED rather than stated fails, while honest prose that adds a
    prohibition alongside the positive statement passes (Round 5, Item A);
  * the author stage runner fails closed on infidelity and routes the
    failure to SPECIFICATION repair;
  * the author stage consults the metrology live-admission marker exactly
    like the review stage (live-capable provider, no marker => fail closed).

NOTE ON THE FIXTURE. `metrology.build_prose` renders each plan-op description
verbatim, so it is a step-by-step SQL recipe and no longer a legal solver
prose. The complete-prose fixture is tests/fixtures/declarative_prose.txt —
hand-written, complete, and operator-free — which is also the executable proof
that the two halves of the gate are jointly satisfiable. Tamper cases that
need operator vocabulary (a dropped rule NAMED in the failure message) still
use build_prose and assert on the specific rule reported.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from elt_taskgen import cli, demo_fixture
from elt_taskgen.generation import mart_plan
from elt_taskgen.models import CouncilRole, MartOpKind, RepairRoute
from elt_taskgen.review import council, metrology, prose_fidelity

#: Complete + declarative: passes every item check without naming an operator.
DECLARATIVE_PROSE = (
    Path(__file__).parent / "fixtures" / "declarative_prose.txt"
).read_text(encoding="utf-8")


def _with_prose(task, prose: str):
    return task.model_copy(update={"solver_prompt": prose})


def _rule_problems(problems: list[str]) -> list[str]:
    """Only the MartSpec-completeness half (drops declarative-gate findings)."""
    return [p for p in problems if "SQL mechanics" not in p]


class CheckProseFidelityTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_complete_declarative_prose_passes(self):
        complete = _with_prose(self.task, DECLARATIVE_PROSE)
        self.assertEqual(prose_fidelity.check_prose_fidelity(complete), [])
        gate = prose_fidelity.prose_fidelity_gate(complete)
        self.assertTrue(gate.passed)
        self.assertEqual(gate.gate, prose_fidelity.GATE_NAME)

    def test_preserving_is_a_declarative_left_join_outcome(self):
        """The natural gerund must not become ``preserv`` and lose LEFT.

        A live semantic author used this exact construction for two valid
        preserved-parent joins.  ``preserve`` and ``preserved`` were accepted,
        but the equivalent gerund was deterministically rejected.
        """
        prose = DECLARATIVE_PROSE.replace(
            "an order in scope that has no line items is preserved, appearing ",
            "preserving an order in scope that has no line items means it appears ",
        )
        self.assertNotEqual(prose, DECLARATIVE_PROSE)
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(self.task, prose)), []
        )

    def test_keeps_measures_is_a_declarative_left_join_outcome(self):
        """Pin the natural preserved-measures wording used by a live author."""
        mart = self.task.marts[0]
        ops = list(mart.plan.ops)
        self.assertIs(ops[4].kind, MartOpKind.JOIN)
        ops[4] = ops[4].model_copy(
            update={
                "description": (
                    "Attach the extremal row's attributes to the grouped measures. "
                    "LEFT, so a group with no rows at all keeps its measures."
                )
            }
        )
        task = self.task.model_copy(
            update={
                "marts": (
                    mart.model_copy(
                        update={
                            "plan": mart.plan.model_copy(update={"ops": tuple(ops)})
                        }
                    ),
                )
            }
        )
        old = (
            "  5. Per-order item totals belong to the orders in scope; an order "
            "in scope that has no line items is preserved, appearing with no item "
            "total of its own rather than being removed."
        )
        new = (
            "  5. The winning extremal row's attributes are attached to the "
            "grouped measures from orders and order_items by order_id, so a group "
            "with no rows at all keeps its measures."
        )
        prose = DECLARATIVE_PROSE.replace(old, new)
        self.assertNotEqual(prose, DECLARATIVE_PROSE)
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(task, prose)), []
        )
        inverted = prose.replace("keeps its measures", "loses its measures")
        problems = _rule_problems(
            prose_fidelity.check_prose_fidelity(_with_prose(task, inverted))
        )
        self.assertTrue(any("rule 5 [join]" in problem for problem in problems))

    def test_carried_is_a_declarative_projection_outcome(self):
        """A grain carried through states projection without SQL vocabulary."""
        vocabulary = prose_fidelity._vocabulary(  # noqa: SLF001 - map regression
            "The grain of cycling_events is carried by event_label alone."
        )
        self.assertTrue(
            prose_fidelity._satisfied("project", vocabulary)  # noqa: SLF001
        )

    def test_generated_rule_verbs_accept_only_their_grammatical_outcomes(self):
        """Irregular forms from saved author drafts retain their exact meaning."""
        cases = {
            "carry": ("carried", "carrying"),
            "label": ("labeled", "labelled", "labeling", "labelling"),
            "bring": ("brought",),
        }
        for rule_term, outcomes in cases.items():
            for outcome in outcomes:
                with self.subTest(rule_term=rule_term, outcome=outcome):
                    vocabulary = prose_fidelity._vocabulary(outcome)  # noqa: SLF001
                    self.assertTrue(
                        prose_fidelity._satisfied(rule_term, vocabulary)  # noqa: SLF001
                    )
        unrelated = prose_fidelity._vocabulary("copied named associated")  # noqa: SLF001
        for rule_term in cases:
            self.assertFalse(
                prose_fidelity._satisfied(rule_term, unrelated),  # noqa: SLF001
                rule_term,
            )

    def test_attributed_rows_represent_a_linked_grain_but_not_when_negated(self):
        """The closed grain synonym repairs the saved COVID wording only."""
        mart = self.task.marts[0].model_copy(
            update={"grain": "One row per customer with linked orders."}
        )
        task = self.task.model_copy(update={"marts": (mart,) + self.task.marts[1:]})
        attributed = DECLARATIVE_PROSE.replace(
            "Grain: One row per customer, including customers with no orders.",
            "Grain: One row per customer with attributed orders.",
        )
        self.assertNotEqual(attributed, DECLARATIVE_PROSE)
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(task, attributed)), []
        )
        for wording in ("associated orders", "not attributed orders"):
            with self.subTest(wording=wording):
                prose = attributed.replace("attributed orders", wording)
                problems = prose_fidelity.check_prose_fidelity(
                    _with_prose(task, prose)
                )
                self.assertTrue(
                    any("grain" in problem and "linked" in problem for problem in problems),
                    problems,
                )

    def test_complete_but_operator_laden_prose_fails(self):
        """Round 5 (Item A): completeness alone is not enough. The canonical
        specimen prose states every item and still fails, because every rule
        is stated as the operator that implements it."""
        recipe = _with_prose(self.task, metrology.build_prose(self.task))
        problems = prose_fidelity.check_prose_fidelity(recipe)
        self.assertEqual(_rule_problems(problems), [])  # complete...
        self.assertTrue(problems)                       # ...but a recipe
        self.assertFalse(prose_fidelity.prose_fidelity_gate(recipe).passed)

    def test_deterministic(self):
        complete = _with_prose(self.task, DECLARATIVE_PROSE)
        self.assertEqual(
            prose_fidelity.prose_fidelity_gate(complete).to_canonical_json(),
            prose_fidelity.prose_fidelity_gate(complete).to_canonical_json(),
        )

    def test_dropped_tie_break_rule_is_named(self):
        stripped = _with_prose(
            self.task,
            metrology.build_prose(
                self.task, omit_op_kinds=frozenset({MartOpKind.TIE_BREAK})
            ),
        )
        problems = _rule_problems(prose_fidelity.check_prose_fidelity(stripped))
        self.assertEqual(len(problems), 1)
        self.assertIn("tie_break", problems[0])
        self.assertIn("Deterministic output order", problems[0])
        self.assertFalse(prose_fidelity.prose_fidelity_gate(stripped).passed)

    def test_dropped_null_default_rule_is_named(self):
        stripped = _with_prose(
            self.task,
            metrology.build_prose(
                self.task, omit_op_kinds=frozenset({MartOpKind.DERIVE})
            ),
        )
        problems = _rule_problems(prose_fidelity.check_prose_fidelity(stripped))
        self.assertEqual(len(problems), 1)
        self.assertIn("derive", problems[0])
        self.assertIn("COALESCE", problems[0])

    def test_empty_prose_fails_for_every_mart(self):
        problems = prose_fidelity.check_prose_fidelity(_with_prose(self.task, ""))
        self.assertTrue(problems)
        self.assertIn(demo_fixture.MART_NAME, problems[0])
        self.assertFalse(
            prose_fidelity.prose_fidelity_gate(_with_prose(self.task, "")).passed
        )

    def test_skeletal_prose_names_every_missing_item_class(self):
        skeletal = _with_prose(
            self.task, "Please build a warehouse summary of activity."
        )
        problems = "\n".join(prose_fidelity.check_prose_fidelity(skeletal))
        self.assertIn("mart name never mentioned", problems)
        self.assertIn("grain", problems)
        self.assertIn("output column", problems)
        self.assertIn("rule", problems)

    def test_missing_single_output_column_is_named(self):
        prose = DECLARATIVE_PROSE.replace("total_spend", "spend")
        problems = prose_fidelity.check_prose_fidelity(_with_prose(self.task, prose))
        self.assertTrue(any("total_spend" in p for p in problems))

    def test_one_mart_cannot_borrow_requirements_from_another_section(self):
        mart = self.task.marts[0]
        shadow_name = "customer_summary_shadow"
        shadow = mart.model_copy(
            update={
                "name": shadow_name,
                "plan": mart.plan.model_copy(
                    update={"mart": shadow_name, "ops": mart.plan.ops[:2]}
                ),
            }
        )
        task = self.task.model_copy(update={"marts": (mart, shadow)})
        prose = DECLARATIVE_PROSE + f"\n\nMart {shadow_name}:\n"

        problems = prose_fidelity.check_prose_fidelity(_with_prose(task, prose))

        shadow_problems = [problem for problem in problems if shadow_name in problem]
        self.assertTrue(shadow_problems, problems)
        self.assertTrue(any("grain" in problem for problem in shadow_problems))
        self.assertTrue(any("output column" in problem for problem in shadow_problems))
        self.assertTrue(any("rule" in problem for problem in shadow_problems))

    def test_each_complete_mart_section_is_checked_independently(self):
        mart = self.task.marts[0]
        shadow_name = "customer_summary_shadow"
        shadow = mart.model_copy(
            update={
                "name": shadow_name,
                "plan": mart.plan.model_copy(update={"mart": shadow_name}),
            }
        )
        task = self.task.model_copy(update={"marts": (mart, shadow)})
        shadow_prose = DECLARATIVE_PROSE.replace(
            "customer_summary", shadow_name
        )
        prose = DECLARATIVE_PROSE + "\n\n" + shadow_prose

        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(task, prose)), []
        )


class FullTaskContractCoverageTest(unittest.TestCase):
    """Deleting or flipping one public TaskIR requirement must turn RED."""

    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _problems(self, prose: str, task=None) -> list[str]:
        return _rule_problems(
            prose_fidelity.check_prose_fidelity(
                _with_prose(task or self.task, prose)
            )
        )

    def test_source_backend_must_be_paired_with_its_table(self):
        prose = DECLARATIVE_PROSE.replace(
            "- orders (mongodb):", "- orders:"
        ) + "\n\nImplementation note: mongodb is available."
        problems = self._problems(prose)
        self.assertTrue(
            any("source table 'orders'" in problem and "mongodb" in problem
                for problem in problems),
            problems,
        )

    def test_relationship_endpoint_and_key_must_share_one_passage(self):
        prose = DECLARATIVE_PROSE.replace(
            "relationship has child order_items order_id and parent orders order_id",
            "relationship has child order_items item_key and parent orders order_id",
        )
        problems = self._problems(prose)
        self.assertTrue(
            any("relationship 2" in problem and "endpoints and keys" in problem
                for problem in problems),
            problems,
        )

    def test_optional_relationship_cannot_be_flipped_to_required(self):
        prose = DECLARATIVE_PROSE.replace(
            "customers customer_id and is optional",
            "customers customer_id and is required",
        )
        problems = self._problems(prose)
        self.assertTrue(
            any("relationship 1" in problem and "optional" in problem
                for problem in problems),
            problems,
        )

    def test_required_relationship_cannot_be_flipped_to_optional(self):
        prose = DECLARATIVE_PROSE.replace(
            "orders order_id and is required",
            "orders order_id and is optional",
        )
        problems = self._problems(prose)
        self.assertTrue(
            any("relationship 2" in problem and "required" in problem
                for problem in problems),
            problems,
        )

    def test_mart_description_must_be_present_in_one_mart_local_passage(self):
        prose = DECLARATIVE_PROSE.replace(
            "Mart customer_summary: Per-customer order activity summary.",
            "Mart customer_summary: Requested output.",
        )
        problems = self._problems(prose)
        self.assertTrue(any("description" in problem for problem in problems), problems)

    def _structured_task_and_prose(self):
        mart = self.task.marts[0]
        ops = list(mart.plan.ops)
        ops[6] = ops[6].model_copy(
            update={
                "description": (
                    "Carry total_spend for qualified activity and label absent "
                    "activity no_activity under bankers_rounding."
                ),
                "tables": ("orders",),
                "columns": ("total_spend",),
                "predicate": (
                    "eligibility_marker = 'qualified' AND "
                    "private_alias.private_answer_key = total_spend"
                ),
                "details": {
                    "null_result": "no_activity",
                    "rounding": "bankers_rounding",
                },
            }
        )
        changed = self.task.model_copy(
            update={
                "marts": (
                    mart.model_copy(
                        update={"plan": mart.plan.model_copy(update={"ops": tuple(ops)})}
                    ),
                )
            }
        )
        prose = metrology.build_prose(changed)
        self.assertEqual(self._problems(prose, changed), [])
        return changed, prose

    def test_carried_projected_field_deletion_is_red(self):
        task, prose = self._structured_task_and_prose()
        target = next(line for line in prose.splitlines() if "Carry total_spend" in line)
        changed = target.replace("total_spend", "other_measure")
        problems = self._problems(prose.replace(target, changed), task)
        self.assertTrue(any("total_spend" in problem for problem in problems), problems)

    def test_structured_operation_table_deletion_is_red(self):
        task, prose = self._structured_task_and_prose()
        target = next(line for line in prose.splitlines() if "Carry total_spend" in line)
        changed = target.replace("orders", "source_rows")
        self.assertNotEqual(target, changed)
        problems = self._problems(prose.replace(target, changed), task)
        self.assertTrue(any("orders" in problem for problem in problems), problems)

    def test_relationship_list_form_is_accepted_and_neighbours_do_not_poison_it(self):
        """batch50 2026-09-10. Two defects on one check, both on prose that
        was CORRECT. (a) The candidate filter demanded the literal word
        'relationship' inside the passage, so an author who writes the word
        once in a heading and then one line each — 'a (k) -> b (id):
        required.' — failed every line; 3 schemapile tasks lost 50+ items
        apiece. (b) A block is one passage, so a 48-line relationship list
        carried BOTH labels and the polarity check rejected every line
        because a NEIGHBOUR declared the other one."""
        from elt_taskgen.models import Relationship

        task, _prose = self._structured_task_and_prose()
        task = task.model_copy(update={"relationships": (
            Relationship(child_table="orders", child_columns=("customer_id",),
                         parent_table="customers", parent_columns=("customer_id",),
                         required=True),
            Relationship(child_table="order_items", child_columns=("order_id",),
                         parent_table="orders", parent_columns=("order_id",),
                         required=False),
        )})
        # The arrow list form, with the two labels adjacent in one block.
        listed = (
            "RELATIONSHIPS. Each line states one relationship.\n"
            "- orders (customer_id) -> customers (customer_id): required.\n"
            "- order_items (order_id) -> orders (order_id): optional.\n"
        )
        problems = [
            p for p in self._problems(listed, task) if p.startswith("relationship")
        ]
        self.assertEqual(problems, [], problems)
        # The guard still bites: swap the labels and BOTH are reported.
        swapped = listed.replace("required.", "TMP").replace("optional.", "required.").replace("TMP", "optional.")
        wrong = [
            p for p in self._problems(swapped, task) if p.startswith("relationship")
        ]
        self.assertEqual(len(wrong), 2, wrong)
        # And a passage that names neither endpoint pair is still not a statement.
        bare = "The warehouse holds orders, customers, order_items and their keys.\n"
        self.assertTrue([p for p in self._problems(bare, task) if p.startswith("relationship")])

    def test_predicate_flip_is_red(self):
        task, prose = self._structured_task_and_prose()
        problems = self._problems(prose.replace("qualified", "rejected"), task)
        self.assertTrue(any("qualified" in problem for problem in problems), problems)

    def test_condition_projection_exposes_only_public_predicate_requirements(self):
        task, _prose = self._structured_task_and_prose()
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)

        self.assertIn("literal specification values: qualified", view)
        self.assertIn("public identifiers: total_spend", view)
        for private in (
            "eligibility_marker",
            "private_alias",
            "private_answer_key",
            "= 'qualified'",
        ):
            self.assertNotIn(private, view)

    def test_details_flip_is_red(self):
        task, prose = self._structured_task_and_prose()
        problems = self._problems(
            prose.replace("bankers_rounding", "ceiling_rounding"), task
        )
        self.assertTrue(
            any("bankers_rounding" in problem for problem in problems), problems
        )

    def test_no_activity_semantic_flip_is_red(self):
        task, prose = self._structured_task_and_prose()
        problems = self._problems(prose.replace("no_activity", "active"), task)
        self.assertTrue(any("no_activity" in problem for problem in problems), problems)

    def test_internal_aliases_and_compiler_sql_are_not_exposed_or_required(self):
        mart = self.task.marts[0]
        ops = list(mart.plan.ops)
        ops[6] = ops[6].model_copy(
            update={
                "tables": ("mart_base", "mart_joined_1", "orders"),
                "columns": ("m_0", "total_spend"),
                "details": {
                    "name": "cohort_rows",
                    "select": "m_0 AS private_projection",
                    "sql": "SELECT secret_answer FROM private_gold",
                    "tie_break": "f_label",
                },
            }
        )
        changed = self.task.model_copy(
            update={
                "marts": (
                    mart.model_copy(
                        update={"plan": mart.plan.model_copy(update={"ops": tuple(ops)})}
                    ),
                )
            }
        )
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(
                _with_prose(changed, DECLARATIVE_PROSE)
            ),
            [],
        )
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, changed)
        for private in (
            "mart_base", "mart_joined_1", "m_0", "cohort_rows",
            "private_projection", "secret_answer", "private_gold", "f_label",
        ):
            self.assertNotIn(private, view)
        self.assertIn("public source tables: orders", view)
        self.assertIn("public carried/output columns: total_spend", view)


class GateDemandsOnlyWhatTheViewShowsTest(unittest.TestCase):
    """D9 (batch-repair 2026-09-09): the rule-level structured requirements
    are read off the SAME public projection the author view is rendered from
    (`solver_safe_plan_requirements`), never off the raw predicate. In
    runs/authorized_batch_50_20260908 every one of 49/50 tasks carried a
    predicate such as ``"link_key" IS NOT NULL`` whose double-quoted compiler
    alias the gate demanded in the prose while the view withheld it — a red
    no revision could clear. Single-quoted literals, public identifiers,
    numbers and public semantic parameters stay demanded."""

    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _with_op(self, **update):
        mart = self.task.marts[0]
        ops = list(mart.plan.ops)
        ops[6] = ops[6].model_copy(update=update)
        return self.task.model_copy(update={"marts": (
            mart.model_copy(update={"plan": mart.plan.model_copy(update={"ops": tuple(ops)})}),
        )})

    def _requirements(self, task):
        mart = task.marts[0]
        step = mart_plan.solver_safe_plan_requirements(task, mart)["steps"][6]
        exact, semantic, _literals = prose_fidelity._structured_op_requirements(
            mart.plan.ops[6],
            source_tables=frozenset(t.name.lower() for t in task.tables),
            public_columns=frozenset(
                c.name.lower() for t in task.tables for c in t.columns
            ) | frozenset(c.name.lower() for c in mart.columns),
            step=step,
        )
        return exact, semantic

    def test_double_quoted_compiler_alias_in_predicate_is_not_demanded(self):
        task = self._with_op(predicate='"link_key" IS NOT NULL AND "link_amount" IS NOT NULL')
        exact, semantic = self._requirements(task)
        self.assertNotIn("link_key", exact + semantic)
        self.assertNotIn("link_amount", exact + semantic)
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
        self.assertNotIn("link_key", view)
        # The complete declarative prose is still green under the alias.
        self.assertEqual(
            _rule_problems(prose_fidelity.check_prose_fidelity(_with_prose(task, DECLARATIVE_PROSE))),
            [],
        )
        # A public column and a single-quoted literal beside the alias ARE
        # demanded — exactly what the view shows — and their absence from the
        # rule's passage is red, naming them.
        task = self._with_op(
            predicate='"link_key" IS NOT NULL AND status = \'completed\'',
        )
        exact, semantic = self._requirements(task)
        self.assertNotIn("link_key", exact + semantic)
        self.assertIn("status", exact)
        self.assertIn("completed", semantic)
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
        self.assertNotIn("link_key", view)
        self.assertIn("literal specification values: completed", view)
        self.assertIn("public identifiers: status", view)
        problems = _rule_problems(prose_fidelity.check_prose_fidelity(_with_prose(task, DECLARATIVE_PROSE)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("rule 7 [derive]", problems[0])
        self.assertIn("'status'", problems[0])
        self.assertNotIn("link_key", problems[0])

    def test_prose_shaped_predicate_is_covered_by_the_description_only(self):
        task = self._with_op(
            predicate="a row counts toward tied_count when its measure equals the per-parent maximum",
        )
        exact, semantic = self._requirements(task)
        for word in ("counts", "toward", "equals", "maximum", "tied_count"):
            self.assertNotIn(word, exact + semantic)
        self.assertEqual(
            _rule_problems(prose_fidelity.check_prose_fidelity(_with_prose(task, DECLARATIVE_PROSE))),
            [],
        )

    def test_public_projection_and_gate_demand_the_same_rule_words(self):
        """Every exact identifier and semantic term the gate demands for a
        rule occurs in the rendered author view (the barrier holds both ways
        for the demo task and for the alias-bearing variant)."""
        for task in (self.task, self._with_op(predicate='"f_flag" = 1 AND status = \'completed\'')):
            view_vocab = prose_fidelity._vocabulary(council.render_view(CouncilRole.SEMANTIC_AUTHOR, task))
            source_tables = frozenset(t.name.lower() for t in task.tables)
            for mart in task.marts:
                steps = mart_plan.solver_safe_plan_requirements(task, mart)["steps"]
                public_columns = frozenset(
                    c.name.lower() for t in task.tables for c in t.columns
                ) | frozenset(c.name.lower() for c in mart.columns)
                for index, op in enumerate(mart.plan.ops):
                    exact, semantic, _literals = prose_fidelity._structured_op_requirements(
                        op, source_tables=source_tables, public_columns=public_columns, step=steps[index],
                    )
                    for term in exact + semantic:
                        self.assertTrue(
                            term in view_vocab
                            or prose_fidelity._DECLARATIVE_EQUIVALENTS.get(term, frozenset()) & view_vocab,
                            (mart.name, index + 1, term),
                        )

    def test_every_gate_template_parses_to_a_named_locus(self):
        """`problem_locus` recognizes every sentence the gate emits (the
        codes-only diagnostic of the author session is built from it)."""
        seen: set[str] = set()
        for prose in ("This prose omits everything that matters.", metrology.build_prose(self.task), DECLARATIVE_PROSE.replace("Grain:", "Shape:")):
            for sentence in prose_fidelity.check_prose_fidelity(_with_prose(self.task, prose)):
                locus = prose_fidelity.problem_locus(sentence)
                self.assertNotEqual(locus.item, "other", sentence)
                seen.add(locus.item)
                if locus.item == "rule":
                    self.assertIn(locus.op_kind, {k.value for k in prose_fidelity.MartOpKind})
                    self.assertIsInstance(locus.rule, int)
                if locus.item in ("key_column", "output_column", "column_description"):
                    self.assertTrue(locus.column)
                if locus.item == "operator":
                    self.assertTrue(locus.category)
                if locus.mart:
                    self.assertIn(locus.mart, {m.name for m in self.task.marts})
        self.assertLessEqual(
            {"source_backend", "relationship", "mart_section", "description", "grain",
             "key_column", "output_column", "rule", "operator"},
            seen,
        )
        empty = prose_fidelity.problem_locus(prose_fidelity.check_prose_fidelity(_with_prose(self.task, ""))[0])
        self.assertEqual((empty.item, empty.mart), ("empty", self.task.marts[0].name))
        relationship = prose_fidelity.problem_locus(
            "relationship 2: order_items(order_id) -> orders(order_id) endpoints and keys are not stated together in one passage"
        )
        self.assertEqual((relationship.item, relationship.rule, relationship.identifiers),
                         ("relationship", 2, ("order_items", "order_id", "orders")))
        column = prose_fidelity.problem_locus(
            "mart 'customer_summary': output column 'total_spend' description substance not represented (missing terms: quantity, unit_price)"
        )
        self.assertEqual((column.item, column.column, column.identifiers),
                         ("column_description", "total_spend", ("total_spend", "quantity", "unit_price")))
        rule = prose_fidelity.problem_locus(
            "mart 'customer_summary': rule 6 [aggregate] ('x') not represented: no single passage states it (its identifiers ['customer_id', 'total_spend'] never co-occur)"
        )
        self.assertEqual((rule.item, rule.op_kind, rule.rule, rule.identifiers),
                         ("rule", "aggregate", 6, ("customer_id", "total_spend")))
        operator = prose_fidelity.problem_locus(
            "distinct-operator: prose states the SQL mechanics 'distinct' on 'order_id' in \"...count distinct order_id...\" — say once"
        )
        self.assertEqual((operator.item, operator.category, operator.column), ("operator", "distinct-operator", "order_id"))


class SentenceLocalityTest(unittest.TestCase):
    """N-crosscut-5: the sentence splitter's newline boundary was DEAD CODE
    (`_normalize` collapsed every newline before `_windows` ran), so an
    unpunctuated markdown list collapsed into one giant "sentence" and the
    RULE_SENTENCE_WINDOW locality promise was not delivered for list-formatted
    prose. These pin the repaired structure: blocks (list items / headings /
    paragraphs) are the unit of locality, soft-wrapped lines are not shattered,
    and the shipped fixture keeps its verdict."""

    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _rules(self, prose):
        return _rule_problems(
            prose_fidelity.check_prose_fidelity(_with_prose(self.task, prose))
        )

    def test_unpunctuated_bullets_are_separate_blocks(self):
        prose = prose_fidelity._normalize(
            "Sources:\n- alpha — files backend\n- beta — s3 backend\n- gamma — rest"
        )
        blocks = prose_fidelity._blocks(prose)
        # The label folds into the first item; the items never merge.
        self.assertEqual(
            blocks,
            [
                ["sources:", "- alpha — files backend"],
                ["- beta — s3 backend"],
                ["- gamma — rest"],
            ],
        )
        windows = [text for text, _ in prose_fidelity._windows(prose, 2)]
        self.assertNotIn("- alpha — files backend - beta — s3 backend", windows)
        self.assertTrue(all("beta" not in w or "gamma" not in w for w in windows))

    def test_normalize_keeps_line_structure(self):
        self.assertEqual(
            prose_fidelity._normalize("A  b\n\n\n- C\td\n"),
            "a b\n\n- c d",
        )

    def test_soft_wrapped_sentence_stays_one_sentence(self):
        prose = prose_fidelity._normalize(
            "The output row order is deterministic: rows are sorted\n"
            "by ascending customer_id.\nAnother sentence follows."
        )
        blocks = prose_fidelity._blocks(prose)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            blocks[0][0],
            "the output row order is deterministic: rows are sorted by ascending customer_id.",
        )

    def test_enumerator_is_glued_to_its_item(self):
        blocks = prose_fidelity._blocks(
            prose_fidelity._normalize("Rules:\n  1. First rule here.\n  2. Second rule.")
        )
        self.assertEqual(
            blocks, [["rules:", "1. first rule here."], ["2. second rule."]]
        )

    def test_windows_never_span_two_list_items(self):
        prose = prose_fidelity._normalize("- One. Two.\n- Three. Four.")
        windows = [text for text, _ in prose_fidelity._windows(prose, 2)]
        self.assertIn("- one. two.", windows)
        self.assertNotIn("two. - three.", windows)
        self.assertIn("- three. four.", windows)

    def test_rule_scattered_across_distant_bullets_is_not_represented(self):
        """Under the collapsed split these unpunctuated bullets were ONE
        sentence and the tie-break rule read as represented; block-local
        windows see two half-statements far apart."""
        lines = DECLARATIVE_PROSE.splitlines()
        tie_break = next(ln for ln in lines if ln.strip().startswith("8."))
        rest = [ln for ln in lines if ln is not tie_break]
        scattered = "\n".join(
            rest
            + [
                "",
                "Notes:",
                "- the output row order is deterministic",
                "- filler bullet one",
                "- filler bullet two",
                "- filler bullet three",
                "- rows are sorted by ascending customer_id",
            ]
        )
        problems = self._rules(scattered)
        self.assertTrue(any("tie_break" in p for p in problems), problems)
        # ...while the same two halves as ADJACENT sentences of one item pass.
        adjacent = "\n".join(
            rest
            + [
                "",
                "- the output row order is deterministic. rows are sorted by "
                "ascending customer_id.",
            ]
        )
        self.assertEqual(self._rules(adjacent), [])

    def test_hard_wrapped_fixture_keeps_its_verdict(self):
        """Soft wraps inside a sentence must not create boundaries: re-wrap
        every fixture line at 60 columns (continuation lines carry no list
        marker) and the complete prose still passes."""
        import textwrap

        wrapped = "\n".join(
            "\n".join(textwrap.wrap(ln, 60, subsequent_indent="   ")) if ln.strip() else ""
            for ln in DECLARATIVE_PROSE.splitlines()
        )
        self.assertNotEqual(wrapped, DECLARATIVE_PROSE)
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(self.task, wrapped)), []
        )


class SemanticInversionTest(unittest.TestCase):
    """Round 5 (Item A). `_EXCLUSIVE_TERM_GROUPS` catches a wholesale
    substitution; it never caught a NEGATION — "Do NOT deduplicate duplicate
    completed order header rows" carries every term the dedupe rule needs and
    passed GREEN. `_negation_problem` closes the sound part of that, and the
    other half of this class is the part that matters more: honest prose that
    merely CONTAINS a negation must keep passing."""

    def setUp(self):
        self.task = demo_fixture.demo_task()
        self.recipe = metrology.build_prose(self.task)

    def _rules(self, prose):
        return _rule_problems(
            prose_fidelity.check_prose_fidelity(_with_prose(self.task, prose))
        )

    @staticmethod
    def _rules_for(task, prose):
        return _rule_problems(
            prose_fidelity.check_prose_fidelity(_with_prose(task, prose))
        )

    def _project_rule_case(self):
        """A full-fidelity fixture whose DERIVE rule contains `project`."""
        mart = self.task.marts[0]
        ops = tuple(
            op.model_copy(
                update={
                    "description": (
                        "Project completed_order_count and total_spend into "
                        "customer_summary."
                    ),
                    "predicate": "",
                    "details": {},
                }
            )
            if op.kind is MartOpKind.DERIVE
            else op
            for op in mart.plan.ops
        )
        plan = mart.plan.model_copy(update={"ops": ops})
        task = self.task.model_copy(
            update={"marts": (mart.model_copy(update={"plan": plan}),)}
        )
        prose = DECLARATIVE_PROSE.replace(
            "7. Customers without orders in scope still appear, and both "
            "measures read 0 for them, never null.",
            "7. completed_order_count and total_spend are carried into "
            "customer_summary.",
        )
        return task, prose

    def _generated_verb_rule_case(
        self, rule_term: str, outcome: str, preposition: str
    ):
        """A full-fidelity DERIVE rule using one generated imperative verb."""
        mart = self.task.marts[0]
        ops = tuple(
            op.model_copy(
                update={
                    "description": (
                        f"{rule_term.title()} completed_order_count and total_spend "
                        f"{preposition} customer_summary."
                    ),
                    "predicate": "",
                    "details": {},
                }
            )
            if op.kind is MartOpKind.DERIVE
            else op
            for op in mart.plan.ops
        )
        task = self.task.model_copy(
            update={
                "marts": (
                    mart.model_copy(
                        update={"plan": mart.plan.model_copy(update={"ops": ops})}
                    ),
                )
                + self.task.marts[1:]
            }
        )
        prose = DECLARATIVE_PROSE.replace(
            "7. Customers without orders in scope still appear, and both "
            "measures read 0 for them, never null.",
            f"7. completed_order_count and total_spend are {outcome} "
            f"{preposition} customer_summary.",
        )
        return task, prose

    def test_generated_rule_verb_outcomes_pass_and_negations_stay_red(self):
        cases = (
            ("carry", "carried", "into"),
            ("label", "labelled", "in"),
            ("bring", "brought", "into"),
        )
        for rule_term, outcome, preposition in cases:
            with self.subTest(rule_term=rule_term):
                task, affirmative = self._generated_verb_rule_case(
                    rule_term, outcome, preposition
                )
                self.assertEqual(
                    prose_fidelity.check_prose_fidelity(
                        _with_prose(task, affirmative)
                    ),
                    [],
                )
                inverted = affirmative.replace(
                    f"are {outcome}", f"are not {outcome}"
                )
                problems = self._rules_for(task, inverted)
                self.assertTrue(
                    any(
                        "[derive]" in problem and "negation" in problem
                        for problem in problems
                    ),
                    problems,
                )

    def test_negated_dedupe_rule_is_red(self):
        inverted = self.recipe.replace(
            "Deduplicate exact-duplicate", "Do NOT deduplicate exact-duplicate"
        )
        problems = self._rules(inverted)
        self.assertTrue(problems)
        self.assertTrue(any("dedupe" in p and "negation" in p for p in problems),
                        problems)

    def test_negated_tie_break_rule_is_red(self):
        inverted = self.recipe.replace(
            "sort by customer_id.", "do not sort by customer_id."
        )
        problems = self._rules(inverted)
        self.assertTrue(any("tie_break" in p and "negation" in p for p in problems),
                        problems)

    def test_negated_null_default_rule_is_red(self):
        inverted = self.recipe.replace(
            "COALESCE both measures", "Never coalesce either of the two measures"
        )
        self.assertTrue(self._rules(inverted))

    def test_negated_preserving_left_equivalent_is_red(self):
        affirmative = DECLARATIVE_PROSE.replace(
            "an order in scope that has no line items is preserved, appearing ",
            "preserving an order in scope that has no line items means it appears ",
        )
        self.assertEqual(self._rules(affirmative), [])
        inverted = affirmative.replace("preserving an order", "not preserving an order")
        problems = self._rules(inverted)
        self.assertTrue(
            any("[join]" in problem and "negation" in problem for problem in problems),
            problems,
        )

    def test_negated_carried_project_equivalent_is_red(self):
        task, affirmative = self._project_rule_case()
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(task, affirmative)), []
        )
        inverted = affirmative.replace("are carried", "are not carried")
        problems = self._rules_for(task, inverted)
        self.assertTrue(
            any("[derive]" in problem and "negation" in problem for problem in problems),
            problems,
        )

    def test_literal_left_join_negation_forms_are_red(self):
        cases = (
            "do not LEFT JOIN",
            "don't LEFT JOIN",
            "never LEFT JOIN",
            "without a LEFT JOIN",
            "do not use a LEFT JOIN",
            "must not use a LEFT JOIN",
            "shouldn't use a LEFT JOIN",
            "mustn't use a LEFT JOIN",
            "can't use a LEFT JOIN",
            "won't use a LEFT JOIN",
            "cannot use a LEFT JOIN",
            "refrain from using a LEFT JOIN",
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                inverted = self.recipe.replace("LEFT JOIN", replacement)
                self.assertNotEqual(inverted, self.recipe)
                problems = self._rules(inverted)
                self.assertTrue(
                    any(
                        "[join]" in problem and "negation" in problem
                        for problem in problems
                    ),
                    problems,
                )

    def test_literal_project_negation_forms_are_red(self):
        task, _ = self._project_rule_case()
        recipe = metrology.build_prose(task)
        needle = "Project completed_order_count and total_spend"
        cases = (
            "Do not project completed_order_count and total_spend",
            "Don't project completed_order_count and total_spend",
            "Never project completed_order_count and total_spend",
            "Shouldn't project completed_order_count and total_spend",
            "Mustn't project completed_order_count and total_spend",
            "Can't project completed_order_count and total_spend",
            "Won't project completed_order_count and total_spend",
            "Refrain from projecting completed_order_count and total_spend",
        )
        self.assertIn(needle, recipe)
        for replacement in cases:
            with self.subTest(replacement=replacement):
                inverted = recipe.replace(needle, replacement)
                problems = self._rules_for(task, inverted)
                self.assertTrue(
                    any(
                        "[derive]" in problem and "negation" in problem
                        for problem in problems
                    ),
                    problems,
                )

    def test_contracted_negation_of_safe_equivalents_is_red(self):
        preserving = DECLARATIVE_PROSE.replace(
            "an order in scope that has no line items is preserved, appearing ",
            "preserving an order in scope that has no line items means it appears ",
        )
        project_task, carried = self._project_rule_case()
        cases = (
            (
                self.task,
                preserving.replace("preserving an order", "isn't preserving an order"),
                "[join]",
            ),
            (
                self.task,
                preserving.replace("preserving an order", "aren't preserving an order"),
                "[join]",
            ),
            (
                project_task,
                carried.replace("are carried", "aren't carried"),
                "[derive]",
            ),
            (
                project_task,
                carried.replace("are carried", "weren't carried"),
                "[derive]",
            ),
        )
        for task, inverted, kind in cases:
            with self.subTest(kind=kind, inverted=inverted):
                problems = self._rules_for(task, inverted)
                self.assertTrue(
                    any(
                        kind in problem and "negation" in problem
                        for problem in problems
                    ),
                    problems,
                )

    def test_equivalent_negation_cues_with_bounded_glue_are_red(self):
        preserving = DECLARATIVE_PROSE.replace(
            "an order in scope that has no line items is preserved, appearing ",
            "preserving an order in scope that has no line items means it appears ",
        )
        project_task, carried = self._project_rule_case()
        cases = (
            (
                self.task,
                preserving.replace(
                    "preserving an order", "refrain from preserving an order"
                ),
                "[join]",
            ),
            (
                project_task,
                carried.replace(
                    "are carried", "are to refrain from being carried"
                ),
                "[derive]",
            ),
            (
                self.task,
                preserving.replace(
                    "preserving an order", "skip the preserving of an order"
                ),
                "[join]",
            ),
            (
                self.task,
                preserving.replace(
                    "preserving an order", "omit any preserving of an order"
                ),
                "[join]",
            ),
        )
        for task, inverted, kind in cases:
            with self.subTest(inverted=inverted.split("\nRules:\n", 1)[-1]):
                problems = self._rules_for(task, inverted)
                self.assertTrue(
                    any(
                        kind in problem and "negation" in problem
                        for problem in problems
                    ),
                    problems,
                )

    def test_not_only_carried_is_affirmative(self):
        task, affirmative = self._project_rule_case()
        expanded = affirmative.replace("are carried", "are not only carried")
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(task, expanded)), []
        )

    def test_honest_prohibition_beside_a_positive_statement_passes(self):
        """The positive-evidence guard. A passage that ASSERTS the operation
        and then narrows it is normal specification writing; flagging it would
        send good tasks into repair."""
        honest = DECLARATIVE_PROSE.replace(
            "rows are sorted by ascending customer_id.",
            "rows are sorted by ascending customer_id, and you must not sort "
            "by any other column.",
        )
        self.assertEqual(self._rules(honest), [])

    def test_honest_negation_of_an_object_passes(self):
        """A positive retained-side outcome keeps an object exclusion honest."""
        honest = DECLARATIVE_PROSE.replace(
            "reaches no customer at all.", "is not retained by any customer at all."
        )
        self.assertEqual(self._rules(honest), [])

    def test_filter_excluded_object_is_not_an_operation_negation(self):
        """`keep` stays outside the equivalent-negation allowlist."""
        original = (
            "Only orders whose status is completed are in scope; an order with "
            "any other status, such as a cancelled one, is out of scope everywhere "
            "below, and nothing about it reaches the mart."
        )
        replacement = (
            "Only completed orders are in scope; cancelled orders are not kept. "
            "The status completed defines the orders in scope for every rule below."
        )
        honest = DECLARATIVE_PROSE.replace(original, replacement)
        self.assertNotEqual(honest, DECLARATIVE_PROSE)
        self.assertEqual(
            prose_fidelity.check_prose_fidelity(_with_prose(self.task, honest)), []
        )

    def test_duplicated_wording_is_not_read_as_contradicting_distinct(self):
        """Before the declarative equivalents, 'duplicated' in a dedupe
        passage was reported as contradicting the rule's 'distinct' — a false
        positive on the most natural way to describe de-duplication."""
        honest = DECLARATIVE_PROSE.replace(
            "rows that are exact duplicates on",
            "rows that are exact duplicated headers on",
        )
        self.assertEqual(self._rules(honest), [])

    def test_wholesale_left_to_inner_rewrite_is_still_red(self):
        problems = self._rules(self.recipe.replace("LEFT JOIN", "INNER JOIN"))
        self.assertTrue(any("mutually exclusive" in p for p in problems), problems)


class AuthorStageFidelityWiringTest(unittest.TestCase):
    """make_author_runner: incomplete prose fails the stage closed with the
    SPECIFICATION repair route and preserves the rejected draft for repair;
    complete prose passes and updates the task."""

    def _runner(self, prose: str):
        class CannedAuthor:
            def complete(self, role, prompt):
                return prose

        return cli.make_author_runner(CannedAuthor())

    def test_recipe_prose_fails_with_specification_route(self):
        """Round 5 (Item A): a COMPLETE prose that names the operators is
        rejected by the same stage, on the same route, as an incomplete one —
        the author stage is the only place a recipe can still be stopped
        before it becomes training data."""
        run_author = self._runner(metrology.build_prose(demo_fixture.demo_task()))
        outcome = run_author(None, demo_fixture.demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
        self.assertIn("SQL mechanics", outcome.payload.error)
        self.assertIsNotNone(outcome.task)
        self.assertEqual(
            outcome.task.solver_prompt,
            metrology.build_prose(demo_fixture.demo_task()),
        )

    def test_incomplete_prose_fails_with_specification_route(self):
        run_author = self._runner("This prose omits everything that matters.")
        outcome = run_author(None, demo_fixture.demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
        self.assertIn("prose fidelity", outcome.payload.error)
        self.assertIn(demo_fixture.MART_NAME, outcome.payload.error)
        self.assertIsNotNone(outcome.task)
        self.assertEqual(
            outcome.task.solver_prompt,
            "This prose omits everything that matters.",
        )

    def test_complete_prose_passes_and_updates_task(self):
        task = demo_fixture.demo_task()
        run_author = self._runner(DECLARATIVE_PROSE)
        outcome = run_author(None, task)
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
        self.assertIsNotNone(outcome.task)
        self.assertEqual(outcome.task.solver_prompt, DECLARATIVE_PROSE)

    def test_unchanged_complete_prose_is_a_pass_without_task_update(self):
        task = demo_fixture.demo_task()
        prose = DECLARATIVE_PROSE
        run_author = self._runner(prose)
        outcome = run_author(None, _with_prose(task, prose))
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
        self.assertIsNone(outcome.task)

    def test_unchanged_but_incomplete_prose_still_fails(self):
        """Resume path: recorded prose that predates the gate must not slide
        through just because the provider echoes it back unchanged."""
        task = demo_fixture.demo_task()
        prose = metrology.build_prose(
            task, omit_op_kinds=frozenset({MartOpKind.TIE_BREAK})
        )
        run_author = self._runner(prose)
        outcome = run_author(None, _with_prose(task, prose))
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)


class AuthorStageAdmissionGateTest(unittest.TestCase):
    """The author stage consults the same metrology admission marker as the
    review stage: a live-capable provider without an admission marker fails
    closed BEFORE any live authoring spend; replay-only stays exempt."""

    class LiveCapableAuthor:
        replay_only = False
        routing = None

        def complete(self, role, prompt):
            raise AssertionError("live call must not happen before admission")

    def test_live_provider_without_marker_fails_closed(self):
        run_author = cli.make_author_runner(self.LiveCapableAuthor())
        with tempfile.TemporaryDirectory() as tmp:
            engine = SimpleNamespace(workspace=Path(tmp))
            outcome = run_author(engine, demo_fixture.demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIn("council not admitted", outcome.payload.error)

    def test_replay_only_provider_is_exempt(self):
        task = demo_fixture.demo_task()

        class ReplayOnlyAuthor:
            replay_only = True
            routing = None

            def complete(self, role, prompt):
                return DECLARATIVE_PROSE

        run_author = cli.make_author_runner(ReplayOnlyAuthor())
        with tempfile.TemporaryDirectory() as tmp:
            engine = SimpleNamespace(workspace=Path(tmp))
            outcome = run_author(engine, task)
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)


if __name__ == "__main__":
    unittest.main()

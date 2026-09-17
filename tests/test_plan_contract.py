"""THE CONTRACT TEST: the validator and the compiler define the SAME plan.

WHY THIS FILE EXISTS
The design that justifies having ONE plan definition (rather than a
normalization pass from a descriptive plan into a compilable one) rests on a
single claim, stated in `generation/mart_plan.py` and again in
`reference/solution.py`: "a plan can never validate and then fail to compile".
That claim was FALSE, and the counterexample was the repo's own canonical
plan — ``validate_plan(demo_task(), customer_summary.plan)`` returned ``[]``
while ``compile_plan_sql`` raised ``PlanCompilationError``. Measured over the
21 on-disk mart plans plus the in-memory demo and the eight plan-library
shapes: 25 of 27 agreed, 2 were validate-OK-but-compile-fails (both the demo
plan, on disk and in memory). With two verdicts in practice there were two
definitions of a valid plan, whatever the docstrings said.

WHY A PER-OP CONTRACT COULD NEVER FIX IT
`op_problems` is a per-op contract, and not every compiler refusal is op-local:

  * ``_resolve`` — "op references unknown relation" — depends on what EARLIER
    ops bound into the plan's namespace.
  * "plan produced no relation" — a property of the op SEQUENCE, not of any op.
  * "order columns are not mart columns" — a property of the plan's tie_break
    op against the MART.

So the shared definition is `reference/solution.py::plan_compilation_problems`,
a dry run of the compiler over the same code path (`_compile_plan`), which
`validate_plan` consumes. This file is the proof, and it is deliberately
adversarial: it asserts the IF-AND-ONLY-IF over every real plan in the repo,
over all fourteen `MartOpKind` members, AND over a systematic mutation sweep
that blanks one field of one op at a time — the shape of plan an LLM proposer
actually emits when it gets a detail key wrong.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.models import (
    ColumnType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    TaskIR,
)
from elt_taskgen.reference import solution as ref

# Sibling-module import works under `discover -s tests` AND under
# `unittest tests.test_plan_contract` (package-style) — both are used here.
try:
    import test_plan_library as tpl
except ModuleNotFoundError:  # package-style invocation
    from tests import test_plan_library as tpl

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Op kinds that no plan in the repo currently uses. They are NOT unreachable —
#: `compile_plan_sql` has a live branch for each — so the corpus below
#: constructs one plan per kind rather than recording them as unexercised.
KINDS_NOT_IN_THE_ON_DISK_CORPUS = frozenset({MartOpKind.DISTINCT, MartOpKind.UNION})


def _on_disk_cases() -> list[tuple[str, TaskIR]]:
    """Every TaskIR serialized under the repo (the real, frozen corpus)."""
    cases: list[tuple[str, TaskIR]] = []
    for path in sorted(REPO_ROOT.rglob("task_ir.json")):
        if ".venv" in path.parts:
            continue
        cases.append((str(path.relative_to(REPO_ROOT)), TaskIR.model_validate_json(
            path.read_text(encoding="utf-8")
        )))
    return cases


def _library_cases() -> list[tuple[str, TaskIR]]:
    """The eight plan-library shapes — the source of the richer op vocabulary."""
    out: list[tuple[str, TaskIR]] = []
    for name, builder in tpl.SHAPES:
        built = builder(tpl.EVIDENCE, mart=f"{name}_mart")
        out.append((f"library::{name}", tpl.build_task(built, f"proof__{name}")))
    return out


def _distinct_and_union_case() -> tuple[str, TaskIR]:
    """A hand-built task exercising the two kinds no repo plan uses.

    DISTINCT (``COUNT(DISTINCT ...)`` as its own op kind) and UNION are live
    compiler branches with zero corpus coverage; without this the iff-property
    would be unproven for 2 of the 14 kinds.
    """
    base = demo_task()
    mart_name = "union_distinct_mart"
    plan = MartPlan(
        mart=mart_name,
        ops=(
            MartOp(
                kind=MartOpKind.DISTINCT,
                description="Distinct completed orders per customer.",
                tables=("orders",),
                columns=("customer_id", "order_count"),
                details={
                    "group_by": "customer_id",
                    "order_count": "COUNT(DISTINCT order_id)",
                    "name": "per_customer",
                },
            ),
            MartOp(
                kind=MartOpKind.UNION,
                description="Union the rollup with itself (mode: distinct).",
                tables=("per_customer", "per_customer"),
                columns=("customer_id", "order_count"),
                details={"mode": "distinct", "name": "unioned"},
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic order.",
                columns=("customer_id",),
            ),
        ),
    )
    mart = MartSpec(
        name=mart_name,
        grain="One row per customer.",
        key_columns=("customer_id",),
        columns=(
            MartColumn(name="customer_id", type=ColumnType.INTEGER,
                       description="Customer id."),
            MartColumn(name="order_count", type=ColumnType.INTEGER,
                       description="Distinct order count."),
        ),
        plan=plan,
    )
    task = base.model_copy(
        update={"marts": (mart,), "reference": None, "attack_cases": ()}
    )
    return ("synthetic::distinct_union", task)


def corpus() -> list[tuple[str, TaskIR, MartSpec]]:
    """(label, task, mart) for every plan this contract is proven over."""
    cases = _on_disk_cases() + [("demo_task()", demo_task())] + _library_cases()
    cases.append(_distinct_and_union_case())
    return [(f"{label}::{m.name}", task, m) for label, task in cases for m in task.marts]


def _compiles(task: TaskIR, mart: MartSpec) -> bool:
    try:
        ref.compile_plan_sql(task, mart)
    except ref.PlanCompilationError:
        return False
    return True


class PlanCorpus(unittest.TestCase):
    """The corpus itself has to be big enough for the proof to mean anything."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = corpus()

    def test_the_corpus_contains_the_real_on_disk_plans_and_the_demo(self) -> None:
        """FLOORS, re-baselined when runs/ was pruned to the five
        canonical releases (was 21/9 against a 30-workspace tree).

        Both floors deliberately EXCLUDE the demo workspace, which is scratch
        and may be absent or mid-rebuild: 11 mart plans come from the five
        released tasks (dbt 6, dlt 2, schemapile 1, synsql 1, wikidbs 1), plus
        demo_task(), 5 library plans and 1 synthetic = 18. Raise these when a
        pool is added; never lower them to make a prune go green.
        """
        labels = [label for label, _, _ in self.cases]
        self.assertGreaterEqual(len(labels), 18, labels)
        self.assertTrue(
            any(lbl.startswith("demo_task()") for lbl in labels),
            "the demo fixture — the plan that exposed the defect — must be in the corpus",
        )
        self.assertGreaterEqual(
            sum(1 for lbl in labels if lbl.startswith("runs/")),
            11,
            "on-disk task_ir.json plans went missing from the corpus",
        )

    def test_every_one_of_the_fourteen_op_kinds_is_covered(self) -> None:
        """All fourteen — no kind gets to sit outside the contract unmeasured."""
        self.assertEqual(len(list(MartOpKind)), 14)
        seen = {
            op.kind
            for _, _, mart in self.cases
            for op in mart.plan.ops
        }
        missing = sorted(k.value for k in MartOpKind if k not in seen)
        self.assertEqual(missing, [], f"op kinds with no plan in the corpus: {missing}")


class ValidatorCompilerAgreement(unittest.TestCase):
    """validate_plan(p) == []  IF AND ONLY IF  compile_plan_sql(p) succeeds."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = corpus()

    def test_the_dry_run_is_faithful_to_the_compiler(self) -> None:
        """`plan_compilation_problems` is empty exactly when compilation works.

        Guaranteed by construction (one code path) — asserted anyway, because
        the whole agreement rests on it.
        """
        for label, task, mart in self.cases:
            with self.subTest(plan=label):
                problems = ref.plan_compilation_problems(task, mart)
                self.assertEqual(problems == [], _compiles(task, mart), problems)

    def test_validator_and_compiler_agree_on_every_plan_in_the_repo(self) -> None:
        for label, task, mart in self.cases:
            with self.subTest(plan=label):
                problems = mp.validate_plan(task, mart.plan)
                self.assertEqual(
                    problems == [],
                    _compiles(task, mart),
                    f"{label}: validator says {problems!r} but the compiler "
                    f"{'accepts' if _compiles(task, mart) else 'refuses'} it",
                )

    def test_the_demo_plan_is_the_regression_case(self) -> None:
        """The exact counterexample: descriptive aggregate, no structured detail.

        The demo TASK is unaffected — it ships `ReferenceSolution.sql_by_mart`,
        so nothing ever compiles this plan in production and the pinned demo
        content hash does not move. What changed is that the VALIDATOR no
        longer claims the plan is fine while the compiler refuses it.
        """
        task = demo_task()
        mart = task.mart("customer_summary")
        with self.assertRaises(ref.PlanCompilationError):
            ref.compile_plan_sql(task, mart)
        problems = mp.validate_plan(task, mart.plan)
        self.assertNotEqual(problems, [])
        self.assertTrue(
            any("group_by" in p for p in problems),
            f"expected the descriptive aggregate op to be named: {problems}",
        )


class AgreementUnderMutation(unittest.TestCase):
    """The contract must survive BROKEN plans, not just the ones that work.

    A contract that only holds on the happy path proves nothing: the defect was
    precisely a plan the validator waved through. This sweep blanks one field
    of one op at a time across every plan in the corpus — the same shape of
    damage a proposer does when it omits a detail key.

    WHICH DIRECTION IS ASSERTED, AND WHY NOT BOTH. Off the corpus of real
    plans the two verdicts are NOT symmetric by design: `validate_plan` also
    owns TASK COHERENCE (declared-FK backing, column resolution, every mart
    column produced) that the compiler cannot see, so a mutant can be
    perfectly compilable and still incoherent — dropping an op's `columns`
    leaves SQL that compiles while a mart column goes unproduced. The
    dangerous direction is the one that was broken, and it is asserted here
    for every mutant: **a clean validation must imply a successful compile.**
    The exact biconditional is asserted on the real corpus above, and on
    `plan_compilation_problems` (which is the compiler) everywhere.
    """

    #: Fields blanked one at a time. `details` keys are dropped individually so
    #: each compiler branch's own requirement (select / group_by / partition_by
    #: / order_by / mode / name) gets its own mutant.
    _SCALAR_FIELDS = ("predicate", "tables", "columns")

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = corpus()

    def _mutants(self, mart: MartSpec):
        for idx, op in enumerate(mart.plan.ops):
            variants: list[tuple[str, MartOp]] = []
            for field in self._SCALAR_FIELDS:
                blank = "" if field == "predicate" else ()
                if getattr(op, field) in ("", ()):
                    continue
                variants.append((field, op.model_copy(update={field: blank})))
            for key in sorted(op.details):
                details = {k: v for k, v in op.details.items() if k != key}
                variants.append((f"details[{key}]", op.model_copy(update={"details": details})))
            for label, mutant in variants:
                ops = tuple(
                    mutant if i == idx else other
                    for i, other in enumerate(mart.plan.ops)
                )
                plan = mart.plan.model_copy(update={"ops": ops})
                yield f"op[{idx}].{label}", mart.model_copy(update={"plan": plan})

    def test_blanking_any_single_op_field_keeps_the_two_verdicts_in_step(self) -> None:
        checked = 0
        accepted = 0
        for label, task, mart in self.cases:
            for mutation, mutated in self._mutants(mart):
                checked += 1
                with self.subTest(plan=label, mutation=mutation):
                    marts = tuple(
                        mutated if m.name == mutated.name else m for m in task.marts
                    )
                    mutant_task = task.model_copy(update={"marts": marts})
                    problems = mp.validate_plan(mutant_task, mutated.plan)
                    # The compiler is the authority in BOTH directions.
                    self.assertEqual(
                        ref.plan_compilation_problems(mutant_task, mutated) == [],
                        _compiles(mutant_task, mutated),
                    )
                    if problems:
                        continue
                    accepted += 1
                    self.assertTrue(
                        _compiles(mutant_task, mutated),
                        f"{label} / {mutation}: validate_plan returned [] but "
                        "compile_plan_sql refused the plan",
                    )
        self.assertGreater(checked, 200, "the mutation sweep degenerated")
        self.assertGreater(
            accepted, 0, "no mutant validated clean — the implication is vacuous"
        )


class PlanLevelFailuresAreCovered(unittest.TestCase):
    """The three refusals a PER-OP contract structurally cannot express.

    Each one is a plan whose every op is individually well-formed. Before the
    fix `validate_plan` returned [] for all three while `compile_plan_sql`
    raised; the assertions below are what makes the iff hold at plan level.
    """

    def setUp(self) -> None:
        self.base = demo_task()

    def _task_with(self, mart: MartSpec) -> TaskIR:
        return self.base.model_copy(
            update={"marts": (mart,), "reference": None, "attack_cases": ()}
        )

    def _mart(self, plan: MartPlan) -> MartSpec:
        return MartSpec(
            name=plan.mart,
            grain="One row per customer.",
            key_columns=("customer_id",),
            columns=(
                MartColumn(name="customer_id", type=ColumnType.INTEGER,
                           description="Customer id."),
            ),
            plan=plan,
        )

    def test_unknown_relation_is_a_plan_level_refusal(self) -> None:
        plan = MartPlan(
            mart="unknown_relation_mart",
            ops=(
                MartOp(
                    kind=MartOpKind.DERIVE,
                    description="Project from a relation nothing ever bound.",
                    tables=("never_bound",),
                    columns=("customer_id",),
                    details={"select": "customer_id", "name": "out"},
                ),
                MartOp(kind=MartOpKind.TIE_BREAK, description="Order.",
                       columns=("customer_id",)),
            ),
        )
        mart = self._mart(plan)
        task = self._task_with(mart)
        # The op ITSELF is well formed: op_problems has nothing to say.
        self.assertEqual(mp.op_problems(plan.ops[0]), [])
        problems = mp.validate_plan(task, plan)
        self.assertTrue(any("unknown relation" in p for p in problems), problems)
        self.assertFalse(_compiles(task, mart))

    def test_a_plan_that_produces_no_relation_is_a_plan_level_refusal(self) -> None:
        plan = MartPlan(
            mart="empty_mart",
            ops=(
                MartOp(kind=MartOpKind.TIE_BREAK, description="Order only.",
                       columns=("customer_id",)),
            ),
        )
        mart = self._mart(plan)
        task = self._task_with(mart)
        self.assertEqual(mp.op_problems(plan.ops[0]), [])
        problems = mp.validate_plan(task, plan)
        self.assertTrue(any("produced no relation" in p for p in problems), problems)
        self.assertFalse(_compiles(task, mart))

    def test_a_tie_break_on_a_non_mart_column_is_a_plan_level_refusal(self) -> None:
        plan = MartPlan(
            mart="bad_order_mart",
            ops=(
                MartOp(
                    kind=MartOpKind.DERIVE,
                    description="Project the key.",
                    tables=("customers",),
                    columns=("customer_id",),
                    details={"select": "customer_id", "name": "out"},
                ),
                MartOp(kind=MartOpKind.TIE_BREAK, description="Order by a non-column.",
                       columns=("status",)),
            ),
        )
        mart = self._mart(plan)
        task = self._task_with(mart)
        self.assertEqual(mp.op_problems(plan.ops[1]), [])
        problems = mp.validate_plan(task, plan)
        self.assertTrue(any("order columns" in p for p in problems), problems)
        self.assertFalse(_compiles(task, mart))


class NoRawSqlEscapeHatch(unittest.TestCase):
    """G7-A: ``details['sql']`` is not a plan construct. The validator and the
    compiler refuse it alike, and the producer (`build_sql_plan`) is gone."""

    def test_raw_sql_detail_is_refused_by_validator_and_compiler(self) -> None:
        base = demo_task()
        mart_name = "raw_sql_mart"
        plan = MartPlan(
            mart=mart_name,
            ops=(
                MartOp(
                    kind=MartOpKind.SOURCE, description="orders", tables=("orders",),
                ),
                MartOp(
                    kind=MartOpKind.UNION,
                    description="Raw SQL smuggled through a union op.",
                    tables=("orders", "orders"),
                    columns=("customer_id",),
                    details={"sql": "SELECT 1 AS customer_id", "name": "x"},
                ),
                MartOp(
                    kind=MartOpKind.TIE_BREAK, description="order", columns=("customer_id",),
                ),
            ),
        )
        mart = MartSpec(
            name=mart_name,
            grain="One row per customer.",
            key_columns=("customer_id",),
            columns=(
                MartColumn(name="customer_id", type=ColumnType.INTEGER,
                           description="Customer id."),
            ),
            plan=plan,
        )
        task = base.model_copy(
            update={"marts": (mart,), "reference": None, "attack_cases": ()}
        )
        op = plan.ops[1]
        problems = mp.op_problems(op)
        self.assertEqual(1, len(problems))
        self.assertIn("details['sql']", problems[0])
        self.assertTrue(any("details['sql']" in p for p in mp.validate_plan(task, plan)))
        with self.assertRaises(ref.PlanCompilationError):
            ref.compile_plan_sql(task, mart)
        self.assertFalse(hasattr(mp, "build_sql_plan"))
        # 'sql' stays a reserved key so an aggregate cannot mint a measure of
        # that name and slip past as text.
        self.assertIn("sql", mp.RESERVED_AGGREGATE_DETAIL_KEYS)


class ValidatorAgreesWithDuckDB(unittest.TestCase):
    """The compiler quotes identifiers before DuckDB sees them.

    Reserved source/relation names are valid public identifiers and therefore
    must compile safely.  Genuine semantic errors, such as a predicate over a
    column that does not exist, still fail the dry run.  In both cases
    ``validate_plan == []`` remains equivalent to DuckDB accepting the SQL.
    """

    def _task_with(self, ops: tuple[MartOp, ...]) -> tuple[TaskIR, MartSpec]:
        base = demo_task()
        mart_name = "dry_run_mart"
        plan = MartPlan(mart=mart_name, ops=ops)
        mart = MartSpec(
            name=mart_name,
            grain="One row per order.",
            key_columns=("order_id",),
            columns=(
                MartColumn(name="order_id", type=ColumnType.INTEGER, description="Order id."),
                MartColumn(name="customer_id", type=ColumnType.INTEGER,
                           description="Customer id."),
            ),
            plan=plan,
        )
        task = base.model_copy(
            update={"marts": (mart,), "reference": None, "attack_cases": ()}
        )
        return task, mart

    def _both_refuse(self, task: TaskIR, mart: MartSpec) -> None:
        problems = ref.plan_compilation_problems(task, mart)
        self.assertTrue(any("rejected by DuckDB" in p for p in problems), problems)
        with self.assertRaises(ref.PlanCompilationError):
            ref.compile_plan_sql(task, mart)
        validated = mp.validate_plan(task, mart.plan)
        self.assertTrue(any("rejected by DuckDB" in p for p in validated), validated)

    def test_a_reserved_relation_alias_is_safely_quoted_by_the_compiler(self) -> None:
        # A FILTER may bind its output as the reserved word `order`.  The
        # compiler must retain that public name and quote it consistently; it
        # must neither reject the task nor silently rename the relation.
        ops = (
            MartOp(
                kind=MartOpKind.FILTER,
                description="Completed orders.",
                tables=("orders",),
                predicate="status = 'completed'",
                details={"name": "order"},
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description="Project.",
                tables=("order",),
                columns=("order_id", "customer_id"),
                details={"select": "order_id, customer_id", "name": "final"},
            ),
            MartOp(kind=MartOpKind.TIE_BREAK, description="Order.", columns=("order_id",)),
        )
        task, mart = self._task_with(ops)
        self.assertEqual([], ref.plan_compilation_problems(task, mart))
        self.assertEqual([], mp.validate_plan(task, mart.plan))
        sql = ref.compile_plan_sql(task, mart)
        self.assertIn('FROM step_0 AS "order"', sql)
        self.assertNotIn("FROM step_0 AS order", sql)
        # The same plan with a non-reserved binding is accepted by both.
        good_ops = (
            ops[0].model_copy(update={"details": {"name": "completed"}}),
            ops[1].model_copy(update={"tables": ("completed",)}),
            ops[2],
        )
        task, mart = self._task_with(good_ops)
        self.assertEqual([], mp.validate_plan(task, mart.plan))
        self.assertIn("SELECT", ref.compile_plan_sql(task, mart))

    def test_a_predicate_over_a_missing_column_is_refused_by_the_dry_run(self) -> None:
        # Predicates are prose to the validator (never parsed for columns), so
        # only the executor can see that `nope` binds to nothing.
        ops = (
            MartOp(
                kind=MartOpKind.FILTER,
                description="Filter on a column that does not exist.",
                tables=("orders",),
                predicate="nope = 'completed'",
                details={"name": "completed"},
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description="Project.",
                tables=("completed",),
                columns=("order_id", "customer_id"),
                details={"select": "order_id, customer_id", "name": "final"},
            ),
            MartOp(kind=MartOpKind.TIE_BREAK, description="Order.", columns=("order_id",)),
        )
        task, mart = self._task_with(ops)
        self._both_refuse(task, mart)


class ExtremaOrderByShadowing(unittest.TestCase):
    """An extrema op must not clobber the plan's tie-break accumulator.

    `compile_plan_sql` read ``details['order_by']`` into a local named
    ``order_by`` — the same name as the plan-level list the tie_break op fills.
    Every extrema plan in the library happens to end with a tie_break op, which
    re-assigned the list and hid the bug; a plan WITHOUT that trailing op
    compiled its extrema ORDER BY string character by character and died with
    "order columns ['\\"', 'f', '_', 'm', ...] are not mart columns". It is a
    validate-OK-but-compile-fails defect of exactly the class this file exists
    to eliminate, so it gets its own regression.
    """

    def test_extrema_without_a_trailing_tie_break_still_compiles(self) -> None:
        built = mp.argmax_profile(tpl.EVIDENCE, mart="argmax_profile_mart")
        task = tpl.build_task(built, "proof__argmax_no_tiebreak")
        mart = task.marts[0]
        ops = tuple(op for op in mart.plan.ops if op.kind is not MartOpKind.TIE_BREAK)
        self.assertTrue(any(op.kind is MartOpKind.EXTREMA for op in ops))
        stripped = mart.model_copy(
            update={"plan": mart.plan.model_copy(update={"ops": ops})}
        )
        task = task.model_copy(update={"marts": (stripped,)})
        sql = ref.compile_plan_sql(task, stripped)
        # Falls back to the mart key columns, then every remaining mart column.
        self.assertIn(f'ORDER BY "{stripped.key_columns[0]}"', sql)
        self.assertEqual(mp.validate_plan(task, stripped.plan), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The plan library: registered shapes, twelve witness rows, and EXECUTED kills.

WHY THIS FILE EXISTS
`build_star` emitted ONE shape everywhere, and the measured consequence across
the 46-task corpus was 2-5 target columns against an anchor median of 14, 1-3
computed against 10, zero extrema/window/ratio/filtered-aggregate/CASE
constructs, and 44 of 46 tasks that are shape-duplicates under a plan-template
signature. This file is the acceptance bar for the replacement.

THE BAR IS EXECUTION, NOT STRUCTURE. For every shape it compiles the reference
through the ONE compiler, mutates it through the ONE mutation engine, runs both
on the CONSTRUCTED counterfactual and scores both with the ONE reward — and it
asserts that every attack the shape CLAIMS actually loses reward there. A shape
whose wrong implementation scores 1.0 on every population is width without
discrimination; `test_every_claimed_attack_is_killed_by_execution` is what
stops one shipping.
"""

from __future__ import annotations

import unittest

import duckdb

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation.coverage import (
    SemanticCoveragePolicy,
    measure_semantic_coverage,
)
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import (
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumnKind,
    MartOpKind,
    MartSpec,
    Origin,
    PopulationName,
    Relationship,
    SemanticPattern,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference import solution as ref
from elt_taskgen.verification import attacks, upstream_eval

ACCOUNT_DOMAIN = ("active", "trial", "churned", "archived")
SUB_STATUS = ("paid", "pending", "failed")

TABLES = (
    TableSpec(
        name="accounts",
        description="Customer accounts.",
        columns=(
            ColumnSpec(name="account_id", type=ColumnType.BIGINT),
            ColumnSpec(name="account_name", type=ColumnType.TEXT),
            ColumnSpec(
                name="account_status", type=ColumnType.TEXT, enum_values=ACCOUNT_DOMAIN
            ),
        ),
        primary_key=("account_id",),
    ),
    TableSpec(
        name="subscriptions",
        description="One row per subscription of an account to a plan.",
        columns=(
            ColumnSpec(name="subscription_id", type=ColumnType.BIGINT),
            ColumnSpec(name="account_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(name="plan_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(name="sub_status", type=ColumnType.TEXT, enum_values=SUB_STATUS),
            ColumnSpec(name="sub_label", type=ColumnType.TEXT),
            ColumnSpec(name="amount", type=ColumnType.INTEGER),
            ColumnSpec(name="started_at", type=ColumnType.DATE),
        ),
        primary_key=("subscription_id",),
    ),
    TableSpec(
        name="plans",
        description="Billing plans.",
        columns=(
            ColumnSpec(name="plan_id", type=ColumnType.BIGINT),
            ColumnSpec(name="plan_name", type=ColumnType.TEXT),
        ),
        primary_key=("plan_id",),
    ),
)

RELATIONSHIPS = (
    Relationship(
        child_table="subscriptions",
        child_columns=("account_id",),
        parent_table="accounts",
        parent_columns=("account_id",),
        required=False,
    ),
    Relationship(
        child_table="subscriptions",
        child_columns=("plan_id",),
        parent_table="plans",
        parent_columns=("plan_id",),
        required=False,
    ),
)

BACKENDS = (
    BackendAssignment(table="accounts", backend=Backend.POSTGRES),
    BackendAssignment(table="subscriptions", backend=Backend.POSTGRES),
    BackendAssignment(table="plans", backend=Backend.FILES),
)

EVIDENCE = mp.ChainEvidence(
    parent="accounts",
    parent_key="account_id",
    parent_attr="account_name",
    parent_domain_column="account_status",
    domain=ACCOUNT_DOMAIN,
    out_of_domain="archived",
    bridge="subscriptions",
    bridge_key="subscription_id",
    bridge_key_is_unique=True,
    bridge_parent_fk="account_id",
    bridge_child_fk="plan_id",
    bridge_status="sub_status",
    bridge_status_pass=("paid",),
    bridge_status_fail=("pending", "failed"),
    bridge_amount="amount",
    bridge_label="sub_label",
    bridge_timestamp="started_at",
    child="plans",
    child_key="plan_id",
    child_label="plan_name",
    child_link_optional=True,
    owner_link_optional=True,
)

SCALE = {"accounts": 12, "subscriptions": 40, "plans": 4}

SHAPES = mp.registered_shape_builders()


def build_task(
    built: mp.BuiltPlan, task_id: str, *, tables: tuple = TABLES
) -> TaskIR:
    """`tables` override exists for the DEDUPE variant: adapters guarantee
    `bridge_needs_dedupe = not bridge.primary_key` (adapters/evidence.py), so
    a dedupe-claiming plan over a PK-bearing bridge is a contradiction the
    witness planter correctly REFUSES (`InertDuplicateWitnessError` — a
    byte-identical duplicate pair cannot survive primary-key enforcement).
    A dedupe fixture must therefore drop the bridge's PK, as the real pools
    do, rather than expect the planter to honor an impossible claim."""
    mart = MartSpec(
        name=built.plan.mart,
        description=f"{built.shape.shape_name} mart.",
        grain=built.shape.shape_name + " grain",
        key_columns=built.shape.key_columns,
        columns=built.columns,
        plan=built.plan,
    )
    populations, cases = pops.derive_populations_and_attacks(
        task_id=task_id,
        tables=tables,
        relationships=RELATIONSHIPS,
        shapes=(built.shape,),
        scale_hint=SCALE,
        backends=2,
    )
    return TaskIR(
        task_id=task_id,
        family_id=f"proof__{built.shape.shape_name}",
        cluster_id=task_id,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=tables,
        relationships=RELATIONSHIPS,
        backends=BACKENDS,
        marts=(mart,),
        populations=populations,
        attack_cases=cases,
    )


def _load(con: duckdb.DuckDBPyConnection, task: TaskIR, rows: dict) -> None:
    for table in task.tables:
        ref.create_table(con, table)
        payload = rows.get(table.name) or []
        if payload:
            ref._insert_rows(con, table, payload)


def _fetch(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class PlanLibraryShapes(unittest.TestCase):
    """Structure: every shape hits its declared budget and validates."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.built = {name: builder(EVIDENCE, mart=f"{name}_mart") for name, builder in SHAPES}
        cls.tasks = {
            name: build_task(built, f"proof__{name}") for name, built in cls.built.items()
        }

    def test_registry_is_unique_and_builders_emit_the_registered_identity(self) -> None:
        names = tuple(registration.shape_name for registration in mp.SHAPE_REGISTRY)
        suffixes = tuple(
            registration.adapter_suffix for registration in mp.SHAPE_REGISTRY
        )
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(suffixes), len(set(suffixes)))
        self.assertEqual(mp.registered_shape_builders(), mp.SHAPE_BUILDERS)
        for name, built in self.built.items():
            with self.subTest(shape=name):
                self.assertEqual(name, built.shape.shape_name)

    def test_every_shape_clears_the_column_budget_contract(self) -> None:
        for name, built in self.built.items():
            with self.subTest(shape=name):
                self.assertEqual([], mp.budget_problems(built))
                self.assertGreaterEqual(len(built.columns), mp.MIN_MART_COLUMNS)
                self.assertGreaterEqual(built.computed_count, mp.MIN_MART_COMPUTED)
                self.assertGreaterEqual(
                    built.passthrough_count, mp.MIN_MART_PASSTHROUGH
                )

    def test_every_shape_reaches_the_declared_computed_ratio(self) -> None:
        # The design's per-shape targets, as measured floors.
        floors = {
            "fan_out_rollup": (11, 0.73),
            "status_cohort_union": (8, 0.75),
            "argmax_profile": (7, 0.71),
            "categorical_ladder": (7, 0.57),
            "temporal_grid": (8, 0.75),
            "latest_snapshot": (9, 0.77),
            "measure_state_distribution": (8, 0.75),
            "orphan_coverage": (9, 0.78),
        }
        for name, (min_cols, min_ratio) in floors.items():
            with self.subTest(shape=name):
                built = self.built[name]
                total = len(built.columns)
                self.assertGreaterEqual(total, min_cols)
                self.assertGreaterEqual(built.computed_count / total, min_ratio - 1e-9)

    def test_every_column_is_classified_and_agrees_with_its_ops(self) -> None:
        for name, task in self.tasks.items():
            with self.subTest(shape=name):
                mart = task.marts[0]
                self.assertEqual([], mp.unclassified_columns(mart))
                self.assertEqual([], mp.column_kind_problems(mart))

    def test_every_plan_validates_and_compiles(self) -> None:
        for name, task in self.tasks.items():
            with self.subTest(shape=name):
                self.assertEqual([], mp.validate_plan(task, task.marts[0].plan))
                sql = ref.compile_plan_sql(task, task.marts[0])
                self.assertIn("SELECT", sql)

    def test_aggregate_then_filter_claim_is_structurally_certified(self) -> None:
        plan = self.built["aggregate_then_filter"].plan
        self.assertEqual("aggregate_then_filter", plan.template_id)
        self.assertEqual(
            (SemanticPattern.AGGREGATE_THEN_FILTER,), plan.semantic_patterns
        )
        self.assertEqual(
            (SemanticPattern.AGGREGATE_THEN_FILTER,),
            mp.detect_semantic_patterns(plan),
        )
        self.assertEqual([], mp.semantic_pattern_problems(plan))

    def test_a_semantic_tag_cannot_certify_itself(self) -> None:
        base = self.built["fan_out_rollup"].plan
        dishonest = base.model_copy(
            update={
                "template_id": "dishonest_aggregate_then_filter",
                "semantic_patterns": (SemanticPattern.AGGREGATE_THEN_FILTER,),
            }
        )
        self.assertEqual((), mp.detect_semantic_patterns(dishonest))
        self.assertTrue(
            any(
                "declared but not certified" in problem
                for problem in mp.semantic_pattern_problems(dishonest)
            )
        )

    def test_an_implemented_pattern_cannot_be_hidden_from_coverage(self) -> None:
        plan = self.built["aggregate_then_filter"].plan.model_copy(
            update={"semantic_patterns": ()}
        )
        self.assertEqual(
            (SemanticPattern.AGGREGATE_THEN_FILTER,),
            mp.detect_semantic_patterns(plan),
        )
        self.assertTrue(
            any(
                "missing from semantic_patterns" in problem
                for problem in mp.semantic_pattern_problems(plan)
            )
        )

    def test_an_unimplemented_pattern_declaration_fails_closed(self) -> None:
        plan = self.built["fan_out_rollup"].plan.model_copy(
            update={
                "template_id": "premature_pivot",
                "semantic_patterns": (SemanticPattern.PIVOT,),
            }
        )
        self.assertTrue(
            any(
                "no registered structural certifier" in problem
                for problem in mp.semantic_pattern_problems(plan)
            )
        )

    def test_strict_coverage_accepts_the_executable_pattern_slice(self) -> None:
        policy = SemanticCoveragePolicy(
            known_template_ids=("aggregate_then_filter",),
            known_patterns=(SemanticPattern.AGGREGATE_THEN_FILTER,),
            require_explicit_template_id=True,
            require_explicit_patterns=True,
            require_certified_patterns=True,
            minimum_template_marts=(("aggregate_then_filter", 1),),
            minimum_template_tasks=(("aggregate_then_filter", 1),),
            minimum_pattern_marts=((SemanticPattern.AGGREGATE_THEN_FILTER, 1),),
            minimum_pattern_tasks=((SemanticPattern.AGGREGATE_THEN_FILTER, 1),),
        )
        report = measure_semantic_coverage(
            (("having", self.tasks["aggregate_then_filter"]),),
            policy=policy,
        )
        self.assertEqual((), report.problems)
        self.assertEqual(1, report.templates[0].mart_count)
        self.assertEqual(1, report.templates[0].task_count)
        self.assertEqual(1, report.patterns[0].mart_count)
        self.assertEqual(1, report.patterns[0].task_count)

    def test_populations_are_coherent(self) -> None:
        for name, task in self.tasks.items():
            with self.subTest(shape=name):
                self.assertEqual([], pops.validate_population_coverage(task))

    def test_the_library_emits_operator_families_build_star_never_did(self) -> None:
        """The library exercises every semantic family absent from the old star."""
        kinds: set[MartOpKind] = set()
        for built in self.built.values():
            kinds |= {op.kind for op in built.plan.ops}
        for required in (
            MartOpKind.FILTERED_AGGREGATE,
            MartOpKind.EXTREMA,
            MartOpKind.WINDOW,
            MartOpKind.CONDITIONAL,
            MartOpKind.RATIO,
            MartOpKind.FILTER,
            MartOpKind.DISTINCT,
            MartOpKind.UNION,
        ):
            self.assertIn(required, kinds)

    def test_column_kinds_cover_the_whole_anchor_taxonomy(self) -> None:
        kinds: set[MartColumnKind] = set()
        for built in self.built.values():
            kinds |= {c.kind for c in built.columns}
        self.assertEqual(set(MartColumnKind), kinds)

    def test_plan_template_signatures_are_all_distinct(self) -> None:
        """Under this signature the current corpus collapses 46 tasks to 2
        templates. Every evidence-backed shape must remain a distinct template."""
        signatures = {
            name: mp.plan_template_signature(built.plan)
            for name, built in self.built.items()
        }
        self.assertEqual(len(set(signatures.values())), len(SHAPES), signatures)

    def test_status_cohorts_are_real_filter_distinct_union_branches(self) -> None:
        plan = self.built["status_cohort_union"].plan
        kinds = [op.kind for op in plan.ops]
        self.assertEqual(3, kinds.count(MartOpKind.FILTER))
        self.assertEqual(3, kinds.count(MartOpKind.DISTINCT))
        self.assertEqual(2, kinds.count(MartOpKind.UNION))
        self.assertNotEqual(
            mp.plan_template_signature(plan),
            mp.plan_template_signature(self.built["argmax_profile"].plan),
        )

    def test_latest_snapshot_orders_by_time_then_a_unique_row_key(self) -> None:
        task = self.tasks["latest_snapshot"]
        mart = task.marts[0]
        extrema = [op for op in mart.plan.ops if op.kind is MartOpKind.EXTREMA]
        self.assertEqual(1, len(extrema))
        order = extrema[0].details["order_by"]
        self.assertIn('"snapshot_at" DESC NULLS LAST', order)
        self.assertIn('"snapshot_row_id" ASC NULLS LAST', order)
        self.assertEqual("snapshot_row_id", extrema[0].details["tie_break"])
        sql = ref.compile_plan_sql(task, mart)
        self.assertIn("ROW_NUMBER() OVER", sql)
        self.assertIn('ORDER BY "snapshot_at" DESC NULLS LAST', sql)

    def test_parent_count_descriptions_state_the_empty_group_default(self) -> None:
        for shape, column in (
            ("argmax_profile", "child_count"),
            ("latest_snapshot", "event_count"),
            ("categorical_ladder", "link_count"),
        ):
            with self.subTest(shape=shape, column=column):
                description = next(
                    item.description
                    for item in self.built[shape].columns
                    if item.name == column
                )
                self.assertIn("0 when there are none", description)

    def test_latest_snapshot_accepts_nullable_time_without_status_payload(self) -> None:
        evidence = dataclasses.replace(
            EVIDENCE,
            bridge_status="",
            bridge_status_pass=(),
            bridge_status_fail=(),
            bridge_timestamp_nullable=True,
        )
        built = mp.latest_snapshot(evidence, mart="statusless_snapshot")
        self.assertEqual(8, len(built.columns))
        self.assertNotIn("latest_status", {column.name for column in built.columns})
        self.assertEqual(
            ("inner_join", "no_null_default", "wrong_denominator", "wrong_window"),
            built.shape.attack_claims,
        )
        task = build_task(built, "proof__statusless_snapshot")
        mart = task.marts[0]
        self.assertEqual([], mp.validate_plan(task, mart.plan))
        sql = ref.compile_plan_sql(task, mart)
        self.assertNotIn("snapshot_status", sql)
        self.assertIn(
            'ORDER BY "snapshot_at" DESC NULLS LAST, '
            '"snapshot_row_id" ASC NULLS LAST',
            sql,
        )

    def test_latest_snapshot_contract_tracks_measured_nullability(self) -> None:
        statusless = {
            "bridge_status": "",
            "bridge_status_pass": (),
            "bridge_status_fail": (),
        }
        strict = mp.latest_snapshot(
            dataclasses.replace(
                EVIDENCE,
                **statusless,
                bridge_timestamp_nullable=False,
                bridge_amount_nullable=False,
                bridge_label_nullable=False,
            ),
            mart="strict_snapshot",
        )
        strict_contract = strict.plan.model_dump_json() + " ".join(
            column.description for column in strict.columns
        )
        self.assertNotIn("ordering measure has no value", strict_contract)
        self.assertNotIn("winning value is missing", strict_contract)
        self.assertNotIn("none of those rows carries", strict_contract)
        self.assertIn("required on every real input row", strict_contract)

        nullable = mp.latest_snapshot(
            dataclasses.replace(
                EVIDENCE,
                **statusless,
                bridge_timestamp_nullable=True,
                bridge_amount_nullable=True,
                bridge_label_nullable=True,
            ),
            mart="nullable_snapshot",
        )
        nullable_contract = nullable.plan.model_dump_json() + " ".join(
            column.description for column in nullable.columns
        )
        self.assertIn("ordering measure has no value", nullable_contract)
        self.assertIn("winning value is missing", nullable_contract)
        self.assertIn("none of those rows carries", nullable_contract)

        # Contract specialization must not remove the defensive SQL or the
        # childless-parent discriminator for the required default mutation.
        task = build_task(strict, "proof__strict_snapshot_contract")
        mart = task.marts[0]
        sql = ref.compile_plan_sql(task, mart)
        self.assertIn('"snapshot_at" DESC NULLS LAST', sql)
        self.assertIn("COALESCE", sql)
        nullable_same_sql = mp.latest_snapshot(
            dataclasses.replace(
                EVIDENCE,
                **statusless,
                bridge_timestamp_nullable=True,
                bridge_amount_nullable=True,
                bridge_label_nullable=True,
            ),
            mart=strict.plan.mart,
        )
        nullable_task = build_task(
            nullable_same_sql,
            "proof__nullable_snapshot_sql",
        )
        self.assertEqual(
            sql,
            ref.compile_plan_sql(nullable_task, nullable_task.marts[0]),
        )
        mutant = attacks._apply_kind(AttackKind.NO_NULL_DEFAULT, sql, mart)
        self.assertIsNotNone(mutant)
        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
        con = duckdb.connect(":memory:")
        try:
            _load(con, task, rows)
            self.assertNotEqual(_fetch(con, sql), _fetch(con, mutant))
        finally:
            con.close()

    def test_measure_distribution_has_disjoint_distinct_union_branches(self) -> None:
        plan = self.built["measure_state_distribution"].plan
        kinds = [op.kind for op in plan.ops]
        self.assertEqual(2, kinds.count(MartOpKind.FILTER))
        self.assertEqual(2, kinds.count(MartOpKind.DISTINCT))
        self.assertEqual(2, kinds.count(MartOpKind.RATIO))
        self.assertEqual(1, kinds.count(MartOpKind.UNION))
        signature = mp.plan_template_signature(plan)
        self.assertNotEqual(
            signature,
            mp.plan_template_signature(self.built["argmax_profile"].plan),
        )
        self.assertNotEqual(
            signature,
            mp.plan_template_signature(self.built["status_cohort_union"].plan),
        )

    def test_keyless_latest_snapshot_deduplicates_before_ranking(self) -> None:
        evidence = dataclasses.replace(EVIDENCE, bridge_needs_dedupe=True)
        built = mp.latest_snapshot(evidence, mart="deduped_snapshot")
        self.assertIn(MartOpKind.DEDUPE, {op.kind for op in built.plan.ops})
        self.assertIn("no_dedup", built.shape.attack_claims)
        dedupe = next(op for op in built.plan.ops if op.kind is MartOpKind.DEDUPE)
        self.assertEqual((), dedupe.columns, "empty means byte-for-byte SELECT DISTINCT *")

    def test_a_shape_refuses_evidence_it_does_not_have(self) -> None:
        """No declared domain, no ladder: an invented domain has no
        out-of-domain witness, so its ELSE is unfalsifiable."""
        blind = mp.ChainEvidence(
            parent="accounts", parent_key="account_id", parent_attr="account_name",
            bridge="subscriptions", bridge_key="subscription_id",
            bridge_parent_fk="account_id",
        )
        with self.assertRaises(ValueError):
            mp.categorical_ladder(blind, mart="nope")

    def test_a_shape_cannot_declare_a_witness_it_cannot_build(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            mp.build_rollup(
                mart="bad",
                shape_name="bad",
                parent="accounts",
                keys=(
                    mp.KeyColumn(
                        column="k", type=ColumnType.BIGINT, description="k",
                        source="account_id",
                    ),
                ),
                measures=(mp.Measure(column="n", expr="COUNT(*)", description="n"),),
                witnesses=(mp.WITNESS_TIE,),
            )
        self.assertIn("unconstructible", str(ctx.exception))

    def test_select_shapes_emits_a_multi_mart_task_at_the_anchor_median(self) -> None:
        """layered_dag: two marts is how a task of 9-11-column marts reaches the
        anchor's median of 14 target / 10 computed columns per TASK."""
        built = mp.select_shapes(EVIDENCE, mart_prefix="acct", budget=2)
        self.assertEqual(2, len(built))
        total = sum(len(b.columns) for b in built)
        computed = sum(b.computed_count for b in built)
        self.assertGreaterEqual(total, 14)
        self.assertGreaterEqual(computed, 10)

    def test_measure_distribution_excludes_present_rows_from_absent_state(self) -> None:
        """The public contract must rule out an entity-level 'absent' reading."""
        built = self.built["measure_state_distribution"]
        absent_filter = next(
            op
            for op in built.plan.ops
            if op.kind is MartOpKind.FILTER and "absent measure-state" in op.description
        )
        state_column = next(c for c in built.columns if c.name == "measure_state")
        for text in (absent_filter.description, state_column.description):
            self.assertIn("belongs only to the present state", text)
            self.assertIn("never to", text)

    def test_multi_mart_childless_conditions_are_scoped_and_execute(self) -> None:
        """Both marts own a public row-B claim and both INNER mutants lose."""

        selected = (
            self.built["fan_out_rollup"],
            self.built["argmax_profile"],
        )
        marts = tuple(
            MartSpec(
                name=built.plan.mart,
                description=f"{built.shape.shape_name} mart.",
                grain=built.shape.shape_name + " grain",
                key_columns=built.shape.key_columns,
                columns=built.columns,
                plan=built.plan,
            )
            for built in selected
        )
        populations, cases = pops.derive_populations_and_attacks(
            task_id="proof__two_scoped_marts",
            tables=TABLES,
            relationships=RELATIONSHIPS,
            shapes=tuple(built.shape for built in selected),
            scale_hint=SCALE,
            backends=2,
        )
        task = TaskIR(
            task_id="proof__two_scoped_marts",
            family_id="proof__two_scoped_marts",
            cluster_id="proof__two_scoped_marts",
            origin=Origin.SYNTHETIC,
            license="CC0-1.0",
            tables=TABLES,
            relationships=RELATIONSHIPS,
            backends=BACKENDS,
            marts=marts,
            populations=populations,
            attack_cases=cases,
        )
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        row_b = pops._WITNESS_PROSE[mp.WITNESS_CHILDLESS]
        row_b_conditions = [
            condition
            for condition in counterfactual.conditions
            if condition.endswith(row_b)
        ]
        self.assertEqual(2, len(row_b_conditions))
        self.assertEqual(
            {built.plan.mart for built in selected},
            {
                condition.split("mart=", 1)[1].split(";", 1)[0]
                for condition in row_b_conditions
            },
        )

        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
        con = duckdb.connect(":memory:")
        try:
            _load(con, task, rows)
            for mart in marts:
                sql = ref.compile_plan_sql(task, mart)
                mutant = attacks._apply_kind(AttackKind.INNER_JOIN, sql, mart)
                self.assertIsNotNone(mutant)
                with self.subTest(mart=mart.name):
                    self.assertNotEqual(_fetch(con, sql), _fetch(con, mutant))
        finally:
            con.close()

    def test_counterfactual_tie_contract_uses_exact_text_order(self) -> None:
        counterfactual = self.tasks["argmax_profile"].population(
            PopulationName.COUNTERFACTUAL
        )
        contract = " ".join(counterfactual.conditions)
        self.assertNotIn("alphabetically", contract.lower())
        self.assertIn(mp.TEXT_ORDER_PROSE, contract)


class PlanLibraryDiscrimination(unittest.TestCase):
    """Execution: every claimed attack must actually lose reward."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.measured: dict[str, dict] = {}
        for name, builder in SHAPES:
            built = builder(EVIDENCE, mart=f"{name}_mart")
            task = build_task(built, f"proof__{name}")
            mart = task.marts[0]
            sql = ref.compile_plan_sql(task, mart)
            cols = tuple(c.name for c in mart.columns)
            per_pop: dict[PopulationName, dict[str, float]] = {}
            for pop in PopulationName:
                rows = generate_rows(task, pop)
                con = duckdb.connect(":memory:")
                try:
                    _load(con, task, rows)
                    gold_rows = _fetch(con, sql)
                    gold_csv = upstream_eval.rows_to_canonical_csv(gold_rows, cols)
                    scores: dict[str, float] = {
                        "__reference__": 1.0
                        if upstream_eval.compare_mart(gold_csv, gold_rows, mart)
                        else 0.0
                    }
                    for claim in built.shape.attack_claims:
                        kind, variant = attacks.split_kind_directive(claim)
                        mutant = attacks._apply_kind(kind, sql, mart, variant)
                        if mutant is None:
                            scores[claim] = -1.0  # not applicable
                            continue
                        try:
                            actual = _fetch(con, mutant)
                        except duckdb.Error:
                            actual = []
                        scores[claim] = (
                            1.0
                            if upstream_eval.compare_mart(gold_csv, actual, mart)
                            else 0.0
                        )
                    per_pop[pop] = scores
                finally:
                    con.close()
            cls.measured[name] = {
                "built": built,
                "task": task,
                "rewards": per_pop,
            }

    def test_the_reference_earns_full_reward_on_every_population(self) -> None:
        for name, m in self.measured.items():
            for pop, scores in m["rewards"].items():
                with self.subTest(shape=name, population=pop.value):
                    self.assertEqual(1.0, scores["__reference__"])

    def test_every_claimed_attack_is_killed_by_execution(self) -> None:
        """THE BAR. Not 'the surface exists' — the mutant must MEASURABLY lose
        reward on the population whose rows were constructed for it. This is the
        assertion that would have caught fivetran's 9 tasks with 0.00
        transform-discriminating attacks at generation time."""
        for name, m in self.measured.items():
            counterfactual = m["rewards"][PopulationName.COUNTERFACTUAL]
            for claim in m["built"].shape.attack_claims:
                with self.subTest(shape=name, attack=claim):
                    self.assertEqual(
                        0.0,
                        counterfactual[claim],
                        f"{name}: claimed attack {claim!r} scores "
                        f"{counterfactual[claim]} on the counterfactual — the "
                        "declaration is a lie the witnesses cannot support",
                    )

    def test_wrong_aggregate_stage_executes_and_leaks_only_the_below_group(self) -> None:
        measured = self.measured["aggregate_then_filter"]
        built = measured["built"]
        task = measured["task"]
        mart = task.marts[0]
        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)

        below_position = built.shape.witnesses.index(mp.WITNESS_BELOW_THRESHOLD)
        boundary_position = built.shape.witnesses.index(mp.WITNESS_ON_THRESHOLD)
        below_key = rows[EVIDENCE.parent][below_position][EVIDENCE.parent_key]
        boundary_key = rows[EVIDENCE.parent][boundary_position][EVIDENCE.parent_key]
        link_counts = {
            parent_key: sum(
                row[EVIDENCE.bridge_parent_fk] == parent_key
                for row in rows[EVIDENCE.bridge]
            )
            for parent_key in (below_key, boundary_key)
        }
        self.assertEqual(1, link_counts[below_key])
        self.assertEqual(2, link_counts[boundary_key])

        sql = ref.compile_plan_sql(task, mart)
        mutant = attacks._apply_kind(
            AttackKind.WRONG_AGG_STAGE,
            sql,
            mart,
            "filter_before_aggregate",
        )
        self.assertIsNotNone(mutant)
        con = duckdb.connect(":memory:")
        try:
            _load(con, task, rows)
            gold = _fetch(con, sql)
            actual = _fetch(con, mutant)
        finally:
            con.close()

        gold_keys = {row["parent_key"] for row in gold}
        mutant_keys = {row["parent_key"] for row in actual}
        self.assertNotIn(below_key, gold_keys)
        self.assertIn(below_key, mutant_keys)
        self.assertIn(boundary_key, gold_keys)
        self.assertIn(boundary_key, mutant_keys)

    def test_statusless_nullable_snapshot_keeps_all_attack_kills(self) -> None:
        evidence = dataclasses.replace(
            EVIDENCE,
            bridge_status="",
            bridge_status_pass=(),
            bridge_status_fail=(),
            bridge_timestamp_nullable=True,
        )
        built = mp.latest_snapshot(evidence, mart="statusless_snapshot")
        task = build_task(built, "proof__statusless_snapshot_attacks")
        mart = task.marts[0]
        sql = ref.compile_plan_sql(task, mart)
        columns = tuple(column.name for column in mart.columns)
        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
        con = duckdb.connect(":memory:")
        try:
            _load(con, task, rows)
            gold_rows = _fetch(con, sql)
            gold_csv = upstream_eval.rows_to_canonical_csv(gold_rows, columns)
            for claim in built.shape.attack_claims:
                with self.subTest(attack=claim):
                    kind, variant = attacks.split_kind_directive(claim)
                    mutant = attacks._apply_kind(kind, sql, mart, variant)
                    self.assertIsNotNone(mutant, f"attack {claim!r} is inapplicable")
                    try:
                        actual = _fetch(con, mutant)
                    except duckdb.Error:
                        actual = []
                    self.assertFalse(
                        upstream_eval.compare_mart(gold_csv, actual, mart),
                        f"attack {claim!r} survived the statusless snapshot",
                    )
        finally:
            con.close()

    def test_every_shape_declares_at_least_four_transform_attacks(self) -> None:
        """Measured per-pool baseline: 0.00 (fivetran) / 0.38 (dlt) / 1.67
        (synsql) / 2.00 (schemapile) / 2.45 (wikidbs) discriminating attacks per
        task. The target is >= 4.0 in every pool."""
        for name, m in self.measured.items():
            with self.subTest(shape=name):
                self.assertGreaterEqual(len(m["built"].shape.attack_claims), 4)

    def test_the_declared_attack_cases_are_exactly_the_claims(self) -> None:
        """Every TRANSFORM case comes from the star's own claims — no more, no
        less.

        Two families are excluded because they are not statements about the
        star at all: the plan-level degenerates (statements about the SOLVER),
        and the load-side cases, which `verification/attacks.py` applies to the
        RENDERED ARTIFACTS and which are derived from the backend layout. A
        star shape has nothing to say about whether a table's CSV header can be
        ingested as a row, so requiring it to claim one would be requiring a
        claim nobody can make.
        """
        plan_level = {"hardcoded_primary_outputs", "keys_only", "no_op", "skip_extraction"}
        for name, m in self.measured.items():
            with self.subTest(shape=name):
                declared = {
                    c.name
                    for c in m["task"].attack_cases
                    if c.name not in plan_level
                    and not c.mutation.startswith(attacks.LOAD_DIRECTIVE_PREFIX)
                }
                claimed = {c.replace("@", "__") for c in m["built"].shape.attack_claims}
                self.assertEqual(claimed, declared)

    def test_witness_rows_are_deterministic(self) -> None:
        for name, builder in SHAPES:
            with self.subTest(shape=name):
                built = builder(EVIDENCE, mart=f"{name}_mart")
                first = pops.counterfactual_literal_rows(
                    TABLES, RELATIONSHIPS, built.shape
                )
                second = pops.counterfactual_literal_rows(
                    TABLES, RELATIONSHIPS, built.shape
                )
                self.assertEqual(first, second)

    def test_the_fan_out_witness_makes_count_and_distinct_count_disagree(self) -> None:
        """Row D is the whole reason the second hop exists: without it COUNT and
        COUNT(DISTINCT) agree and the no_dedup surface is decorative."""
        m = self.measured["fan_out_rollup"]
        task = m["task"]
        sql = ref.compile_plan_sql(task, task.marts[0])
        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
        con = duckdb.connect(":memory:")
        try:
            _load(con, task, rows)
            gold = _fetch(con, sql)
        finally:
            con.close()
        diverging = [
            r for r in gold if r["link_count"] != r["distinct_child_count"]
        ]
        self.assertTrue(
            diverging,
            "no counterfactual row separates COUNT from COUNT(DISTINCT)",
        )


class LegacyStarIsUnchanged(unittest.TestCase):
    """The plan library is ADDITIVE: build_star's output must not move, or
    every already-generated task's content hash moves with it."""

    def test_build_star_still_emits_the_legacy_shape(self) -> None:
        built = mp.build_star(
            mart="legacy",
            parent="accounts",
            parent_keys=("account_id",),
            key_columns=("account_key",),
            joins=(
                mp.StarJoin(
                    table="subscriptions",
                    on_pairs=(("account_key", "account_id"),),
                    carry=(("subscription_id", "sub_id"),),
                    rel_columns=("account_id",),
                ),
            ),
            measures=(mp.Measure(column="sub_count", expr="COUNT(sub_id)"),),
        )
        self.assertEqual(
            [
                MartOpKind.SOURCE,
                MartOpKind.SOURCE,
                MartOpKind.DERIVE,
                MartOpKind.JOIN,
                MartOpKind.AGGREGATE,
                MartOpKind.DERIVE,
                MartOpKind.TIE_BREAK,
            ],
            [op.kind for op in built.plan.ops],
        )
        self.assertEqual((), built.shape.witnesses)
        self.assertEqual((), built.columns)

    def test_the_legacy_counterfactual_still_builds_three_parent_rows(self) -> None:
        built = mp.build_star(
            mart="legacy",
            parent="accounts",
            parent_keys=("account_id",),
            key_columns=("account_key",),
            joins=(
                mp.StarJoin(
                    table="subscriptions",
                    on_pairs=(("account_key", "account_id"),),
                    carry=(("subscription_id", "sub_id"),),
                    rel_columns=("account_id",),
                ),
            ),
            measures=(mp.Measure(column="sub_count", expr="COUNT(sub_id)"),),
        )
        rows = pops.counterfactual_literal_rows(TABLES, RELATIONSHIPS, built.shape)
        self.assertEqual(3, len(rows["accounts"]))


class LiteralRowsRespectDeclaredTypes(unittest.TestCase):
    """No constructed counterfactual value may violate its column's type.

    dbt_apple_search_ads: `search_term_report`'s grain LEADS with a date, so
    the star's (fact_link_columns, parent_keys) zip paired that date key with
    INTEGER `campaign_id` and copied '2029-06-23' into it. The value then
    spread through type-clean integer links into `campaign_history.id`, and
    the failure only surfaced at reference-run as "Could not convert string
    '2029-06-23' to INT32" — several stages and one repair round later.
    """

    def _table(self):
        return TableSpec(
            name="t",
            columns=(
                ColumnSpec(name="id", type=ColumnType.INTEGER, description="pk"),
                ColumnSpec(name="when", type=ColumnType.DATE, description="day"),
            ),
            primary_key=("id",),
        )

    def test_a_date_in_an_integer_column_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            pops.assert_literal_rows_typed(
                (self._table(),), {"t": ({"id": "2029-06-23", "when": "2029-06-23"},)}
            )
        self.assertIn("t[0].id", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    def test_a_number_in_a_date_column_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            pops.assert_literal_rows_typed(
                (self._table(),), {"t": ({"id": 1, "when": 2029},)}
            )

    def test_well_typed_rows_pass_and_NULL_is_allowed(self) -> None:
        pops.assert_literal_rows_typed(
            (self._table(),),
            {"t": ({"id": 1, "when": "2024-01-01"}, {"id": 2, "when": None})},
        )


class NullableAggregateClassification(unittest.TestCase):
    """Which measures may a description promise an EMPTY value for?

    Only those that can be NULL for a NON-EMPTY group. The same reddit_ads
    dual build caught this in BOTH directions, one round apart:
      * unguarded `SUM(total_items)` IS NULL when every input is NULL — gold
        empty, and a builder that coalesced lost primary + resampled;
      * a CASE's non-NULL ELSE arm does not prove its nullable THEN arm. If
        every row qualifies and every selected value is NULL, SUM is NULL.
        Only syntax proving every result branch non-NULL may suppress the
        conservative nullable contract.
    """

    def _can_be_null(self, expr: str, **kw) -> bool:
        return mp._aggregate_can_be_null(mp.Measure(column="m", expr=expr, **kw))

    def test_unguarded_sum_can_be_null(self) -> None:
        self.assertTrue(self._can_be_null("SUM(total_items)"))
        self.assertTrue(self._can_be_null("MAX(closed_at)"))

    def test_else_literal_does_not_mask_a_nullable_then_branch(self) -> None:
        expression = (
            "SUM(CASE WHEN event_name = 'custom' THEN total_items ELSE 0 END)"
        )
        self.assertTrue(self._can_be_null(expression))
        self.assertTrue(self._can_be_null(expression, input_nullable=True))
        # Even a real-row non-null declaration cannot prove what a LEFT-join
        # null extension plus an arbitrary predicate will feed the CASE.
        self.assertTrue(self._can_be_null(expression, input_nullable=False))

    def test_every_non_null_case_result_is_proven_non_null(self) -> None:
        self.assertFalse(
            self._can_be_null("SUM(CASE WHEN p THEN 1 ELSE 0 END)")
        )
        self.assertFalse(
            self._can_be_null(
                "SUM(CASE WHEN p THEN COALESCE(x, 0) ELSE 0.0 END)"
            )
        )

    def test_all_qualifying_all_null_case_is_null_in_duckdb(self) -> None:
        con = duckdb.connect(":memory:")
        self.addCleanup(con.close)
        expression = "SUM(CASE WHEN qualifies THEN nullable_x ELSE 0 END)"
        all_qualifying = con.execute(
            "SELECT " + expression + " FROM (VALUES "
            "(TRUE, CAST(NULL AS INTEGER)), "
            "(TRUE, CAST(NULL AS INTEGER))) AS rows(qualifies, nullable_x)"
        ).fetchone()[0]
        with_non_qualifying = con.execute(
            "SELECT " + expression + " FROM (VALUES "
            "(TRUE, CAST(NULL AS INTEGER)), "
            "(FALSE, CAST(NULL AS INTEGER))) AS rows(qualifies, nullable_x)"
        ).fetchone()[0]
        self.assertIsNone(all_qualifying)
        self.assertEqual(with_non_qualifying, 0)
        self.assertTrue(self._can_be_null(expression))

    def test_else_null_is_still_nullable(self) -> None:
        """`ELSE NULL` guards nothing — an all-NULL group still sums to NULL."""
        self.assertTrue(
            self._can_be_null("SUM(CASE WHEN p THEN x ELSE NULL END)")
        )

    def test_count_coalesce_and_declared_default_are_never_null(self) -> None:
        self.assertFalse(self._can_be_null("COUNT(*)"))
        self.assertFalse(self._can_be_null("COUNT(DISTINCT order_id)"))
        self.assertFalse(self._can_be_null("COALESCE(SUM(amount), 0)"))
        self.assertFalse(self._can_be_null("SUM(amount)", null_default="0"))

    def test_inner_coalesce_and_count_prefix_do_not_overclaim_a_proof(self) -> None:
        self.assertTrue(self._can_be_null("SUM(x + COALESCE(y, 0))"))
        self.assertTrue(self._can_be_null("COUNT(*) + MAX(x)"))
        self.assertTrue(self._can_be_null("SUM(1 % 0)"))
        self.assertTrue(self._can_be_null("SUM(TRY_CAST('bad' AS INTEGER))"))
        self.assertTrue(self._can_be_null("SUM(x)", null_default="NULL"))
        self.assertTrue(self._can_be_null("not valid sql ("))


class CounterfactualTimestampsSpanCalendarDays(unittest.TestCase):
    """A constructed counterfactual row's TIMESTAMP must advance the calendar
    DAY, not the second.

    `_mint` spaced timestamps by seconds while its own DATE branch (and the
    witness coercer `_coerce`) spaced by days, so every constructed row landed
    on 2024-01-01. A mart grained on the DATE PART of a timestamp — dlt's
    `<event>_activity`, one row per calendar day of the incremental cursor —
    therefore collapsed to ONE gold row on the counterfactual. Measured on
    pipedrive: `deals_activity` had 1 gold row, a constant submission scored
    1/3, and degenerate-zero + info-content rejected the task.
    """

    def _ts_column(self) -> ColumnSpec:
        return ColumnSpec(
            name="update_time",
            type=ColumnType.TIMESTAMP,
            description="incremental cursor",
        )

    def test_successive_indices_land_on_distinct_days(self) -> None:
        col = self._ts_column()
        days = {pops._mint(col, "deals", i)[:10] for i in range(9)}
        self.assertEqual(9, len(days), f"timestamps collapsed onto {days}")

    def test_timestamps_stay_unique_and_monotonic(self) -> None:
        col = self._ts_column()
        minted = [pops._mint(col, "deals", i) for i in range(12)]
        self.assertEqual(len(minted), len(set(minted)))
        self.assertEqual(minted, sorted(minted))

    def test_date_and_timestamp_branches_agree_on_the_day(self) -> None:
        """The two temporal branches must not disagree about what day it is —
        that divergence is what hid the bug."""
        ts = self._ts_column()
        dt = ColumnSpec(
            name="won_date", type=ColumnType.DATE, description="close date"
        )
        for i in (0, 1, 5):
            self.assertEqual(
                pops._mint(dt, "deals", i), pops._mint(ts, "deals", i)[:10]
            )



class RecoveredRelationshipDoesNotCollapseWitnesses(unittest.TestCase):
    """B3.2 (blocker1 plan). A RECOVERED relationship (§C1) whose child column
    is the witness bridge FK used to slip past the pair-based skip set, and
    `_close_foreign_keys` repointed every witness bridge row at the one minted
    dim row — measured: distinct anchor keys 5 -> 1, every witness collapsed
    into a single group. The skip now also refuses any relationship writing
    into a column the witness construction controls (resolved bridge FK,
    fan-out child key, grain columns).
    """

    def test_witness_bridge_keys_survive_a_recovered_relationship(self) -> None:
        from elt_taskgen.generation.populations import witness_literal_rows
        from elt_taskgen.models import ColumnSpec, ColumnType, Relationship, TableSpec

        for name, builder in SHAPES:
            with self.subTest(shape=name):
                built = builder(EVIDENCE, mart=f"{name}_mart")
                shape = built.shape
                _at, _ak, bridge_t, bridge_fk = shape.witness_anchor()
                dim = TableSpec(
                    name="recovered_dim", description="Recovered lookup.",
                    columns=(ColumnSpec(name="dim_id", type=ColumnType.TEXT,
                                        description="id", nullable=False),),
                    primary_key=("dim_id",))
                recovered = Relationship(
                    child_table=bridge_t, child_columns=(bridge_fk,),
                    parent_table="recovered_dim", parent_columns=("dim_id",),
                    required=True)
                base = witness_literal_rows(TABLES, RELATIONSHIPS, shape)
                rec = witness_literal_rows(
                    TABLES + (dim,), RELATIONSHIPS + (recovered,), shape)
                anchor_table, anchor_key, _bridge_table, _bridge_column = (
                    shape.witness_anchor()
                )
                anchor_values = {
                    row.get(anchor_key) for row in base.get(anchor_table, ())
                }
        # A required edge suppresses the optional orphan on the same child
        # column; the A-L witness rows remain the regression target.
                base_bridge_keys = [
                    row.get(bridge_fk)
                    for row in base.get(bridge_t, ())
                    if row.get(bridge_fk) is None
                    or row.get(bridge_fk) in anchor_values
                ]
                recovered_bridge_keys = [
                    row.get(bridge_fk)
                    for row in rec.get(bridge_t, ())
                    if row.get(bridge_fk) is None
                    or row.get(bridge_fk) in anchor_values
                ]
                self.assertEqual(
                    base_bridge_keys,
                    recovered_bridge_keys,
                    f"{name}: the recovered relationship repointed witness rows",
                )

if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Fix-round additions: role distinctness, defaulted windows, prose arms,
# nullable-grain declines, certified kinds, narrowed trial protocol
# ---------------------------------------------------------------------------

import dataclasses  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from elt_taskgen.reference import gold as gold_mod  # noqa: E402
from elt_taskgen.reference import runner as runner_mod  # noqa: E402


def _nullable(tables: tuple, table: str, *columns: str) -> tuple:
    """Copy of `tables` with the named columns of `table` declared nullable."""
    out = []
    for t in tables:
        if t.name != table:
            out.append(t)
            continue
        out.append(
            t.model_copy(
                update={
                    "columns": tuple(
                        c.model_copy(update={"nullable": True}) if c.name in columns else c
                        for c in t.columns
                    )
                }
            )
        )
    return tuple(out)


#: NOT NULL owner link + required relationship: the fixture temporal_grid may
#: legitimately grain on (the shared TABLES declare account_id nullable and the
#: link optional — exactly what the shape now declines).
STRICT_TABLES = tuple(
    t.model_copy(
        update={
            "columns": tuple(
                c.model_copy(update={"nullable": False}) if c.name == "account_id" else c
                for c in t.columns
            )
        }
    )
    if t.name == "subscriptions"
    else t
    for t in TABLES
)
STRICT_RELATIONSHIPS = tuple(
    r.model_copy(update={"required": True}) if r.child_columns == ("account_id",) else r
    for r in RELATIONSHIPS
)


def _strict_task(built: mp.BuiltPlan, task_id: str) -> TaskIR:
    mart = MartSpec(
        name=built.plan.mart,
        description=f"{built.shape.shape_name} mart.",
        grain=built.shape.shape_name + " grain",
        key_columns=built.shape.key_columns,
        columns=built.columns,
        plan=built.plan,
    )
    populations, cases = pops.derive_populations_and_attacks(
        task_id=task_id,
        tables=STRICT_TABLES,
        relationships=STRICT_RELATIONSHIPS,
        shapes=(built.shape,),
        scale_hint=SCALE,
        backends=2,
    )
    return TaskIR(
        task_id=task_id,
        family_id=f"proof__{built.shape.shape_name}",
        cluster_id=task_id,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=STRICT_TABLES,
        relationships=STRICT_RELATIONSHIPS,
        backends=BACKENDS,
        marts=(mart,),
        populations=populations,
        attack_cases=cases,
    )


class RoleDistinctness(unittest.TestCase):
    """N-mart_plan-1: one source column never plays two roles in a shape."""

    def test_shapes_refuse_colliding_roles(self) -> None:
        # bridge_amount == bridge_key: the "measure" would be the row's own id.
        amount_is_key = dataclasses.replace(EVIDENCE, bridge_amount="subscription_id")
        for builder in (mp.argmax_profile, mp.fan_out_rollup):
            with self.subTest(shape=builder.__name__, collision="amount=key"):
                with self.assertRaises(mp.ShapeNotSelectable) as ctx:
                    builder(amount_is_key, mart="x")
                self.assertIn("bridge_key", str(ctx.exception))
                self.assertIn("bridge_amount", str(ctx.exception))
                self.assertIn("subscription_id", str(ctx.exception))
        # parent_attr == parent_key: parent_name would duplicate parent_key.
        attr_is_key = dataclasses.replace(EVIDENCE, parent_attr="account_id")
        for name, builder in SHAPES:
            with self.subTest(shape=name, collision="attr=key"):
                with self.assertRaises(mp.ShapeNotSelectable) as ctx:
                    builder(attr_is_key, mart="x")
                self.assertIn("parent_key", str(ctx.exception))
                self.assertIn("parent_attr", str(ctx.exception))
        # bridge_key == bridge_parent_fk: refused by shapes that need two
        # distinguishable links in one parent. Argmax alone can stay honest by
        # dropping top_row_id and its row-id tie break.
        key_is_fk = dataclasses.replace(EVIDENCE, bridge_key="account_id")
        for name, builder in SHAPES:
            if name == "argmax_profile":
                continue
            with self.subTest(shape=name, collision="key=fk"):
                with self.assertRaises(mp.ShapeNotSelectable):
                    builder(key_is_fk, mart="x")

    def test_measure_distribution_declines_a_shared_or_parent_fk_link_key(self) -> None:
        """Its O witness needs same parent FK and different link-key values."""
        shared_role = dataclasses.replace(EVIDENCE, bridge_key="account_id")
        with self.assertRaises(mp.ShapeNotSelectable) as shared:
            mp.measure_state_distribution(shared_role, mart="distribution_shared")
        self.assertIn("bridge_key", str(shared.exception))
        self.assertIn("bridge_parent_fk", str(shared.exception))

        flagged_parent_fk = dataclasses.replace(
            EVIDENCE, bridge_key_is_parent_fk=True
        )
        with self.assertRaises(mp.ShapeNotSelectable) as flagged:
            mp.measure_state_distribution(flagged_parent_fk, mart="distribution_flagged")
        self.assertIn("row-distinguishing link key", str(flagged.exception))
        self.assertIn("distinct-measure witness", str(flagged.exception))

    def test_argmax_drops_row_id_when_bridge_key_is_the_parent_fk(self) -> None:
        """The dlt__workable case: `_countable_key` fell back to `_jobs_id`,
        and top_row_id equalled parent_key on 266/266 rows while the prose
        said it 'identifies WHICH row won'."""
        key_is_fk = dataclasses.replace(EVIDENCE, bridge_key="account_id")
        built = mp.argmax_profile(key_is_fk, mart="argmax_fk")
        names = [c.name for c in built.columns]
        self.assertNotIn("top_row_id", names)
        self.assertEqual([], mp.budget_problems(built))
        self.assertGreaterEqual(len(names), mp.MIN_MART_COLUMNS)
        extrema = [op for op in built.plan.ops if op.kind is MartOpKind.EXTREMA]
        self.assertEqual(1, len(extrema))
        self.assertNotIn("f_id", extrema[0].details["order_by"])
        for op in built.plan.ops:
            self.assertNotIn("then the smallest", op.description)
        for column in built.columns:
            self.assertNotIn("WHICH row won", column.description)
            self.assertNotIn("then the smallest", column.description)
        # The declared flag is honoured too (adapters state it explicitly).
        flagged = mp.argmax_profile(
            dataclasses.replace(EVIDENCE, bridge_key_is_parent_fk=True), mart="argmax_flag"
        )
        self.assertNotIn("top_row_id", [c.name for c in flagged.columns])
        # And the ordinary evidence still ships the row identifier + clause.
        full = mp.argmax_profile(EVIDENCE, mart="argmax_full")
        self.assertIn("top_row_id", [c.name for c in full.columns])
        self.assertTrue(
            any("WHICH row won" in c.description for c in full.columns)
        )
        # It validates, compiles, and every claimed attack is still killed.
        task = build_task(built, "proof__argmax_fk")
        self.assertEqual([], mp.validate_plan(task, built.plan))
        mart = task.marts[0]
        sql = ref.compile_plan_sql(task, mart)
        cols = tuple(c.name for c in mart.columns)
        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
        con = duckdb.connect(":memory:")
        try:
            _load(con, task, rows)
            gold_rows = _fetch(con, sql)
            gold_csv = upstream_eval.rows_to_canonical_csv(gold_rows, cols)
            self.assertTrue(upstream_eval.compare_mart(gold_csv, gold_rows, mart))
            for claim in built.shape.attack_claims:
                kind, variant = attacks.split_kind_directive(claim)
                mutant = attacks._apply_kind(kind, sql, mart, variant)
                self.assertIsNotNone(mutant, claim)
                try:
                    actual = _fetch(con, mutant)
                except duckdb.Error:
                    actual = []
                with self.subTest(attack=claim):
                    self.assertFalse(upstream_eval.compare_mart(gold_csv, actual, mart))
        finally:
            con.close()

    def test_argmax_drops_row_id_when_countable_key_is_not_unique(self) -> None:
        """A numeric fallback can count rows without identifying one row.

        WikiDBs clinical-trial data exposed this distinction: participantcount
        is non-null and numeric, but repeats within a parent.  Publishing it as
        ``top_row_id`` claimed an identifier the source does not possess.
        """
        nonunique = dataclasses.replace(
            EVIDENCE,
            bridge_key="participantcount",
            bridge_key_is_unique=False,
        )
        built = mp.argmax_profile(nonunique, mart="argmax_nonunique")
        self.assertNotIn("top_row_id", [c.name for c in built.columns])
        extrema = next(
            op for op in built.plan.ops if op.kind is MartOpKind.EXTREMA
        )
        self.assertNotIn("f_id", extrema.details["order_by"])
        for column in built.columns:
            self.assertNotIn("WHICH row won", column.description)

    def test_distinct_source_problems_names_a_duplicated_passthrough(self) -> None:
        keys = (mp.KeyColumn(column="k", type=ColumnType.BIGINT, description="k",
                             source="account_id"),)
        dup = (mp.Passthrough(column="k_again", type=ColumnType.BIGINT,
                              description="k again", source="account_id"),)
        problems = mp.distinct_source_problems(keys=keys, passthrough=dup, hops=())
        self.assertEqual(1, len(problems))
        self.assertIn("account_id", problems[0])
        self.assertIn("k_again", problems[0])
        clean = (mp.Passthrough(column="n", type=ColumnType.TEXT, description="n",
                                source="account_name"),)
        self.assertEqual([], mp.distinct_source_problems(keys=keys, passthrough=clean, hops=()))


class TrialProtocolIsNarrow(unittest.TestCase):
    """G7-B: only ShapeNotSelectable / MartBudgetError are 'not on this
    schema'; any other ValueError is a builder bug and must surface."""

    def test_select_shapes_swallows_only_shape_not_selectable(self) -> None:
        def buggy(evidence, *, mart, notes=""):
            raise ValueError("bug")

        def declines(evidence, *, mart, notes=""):
            raise mp.ShapeNotSelectable("no")

        def over_budget(evidence, *, mart, notes=""):
            raise mp.MartBudgetError("thin")

        original = mp.SHAPE_BUILDERS
        try:
            mp.SHAPE_BUILDERS = (("declines", declines), ("over_budget", over_budget),
                                 ("argmax_profile", mp.argmax_profile))
            built = mp.select_shapes(EVIDENCE, mart_prefix="acct", budget=2)
            self.assertEqual(["acct_argmax_profile"], [b.plan.mart for b in built])
            mp.SHAPE_BUILDERS = (("buggy", buggy),) + original
            with self.assertRaises(ValueError) as ctx:
                mp.select_shapes(EVIDENCE, mart_prefix="acct", budget=2)
            self.assertEqual("bug", str(ctx.exception))
        finally:
            mp.SHAPE_BUILDERS = original

    def test_the_declines_are_value_errors_for_legacy_callers(self) -> None:
        self.assertTrue(issubclass(mp.ShapeNotSelectable, ValueError))
        self.assertTrue(issubclass(mp.MartBudgetError, ValueError))
        blind = mp.ChainEvidence(parent="accounts", parent_key="account_id")
        with self.assertRaises(mp.ShapeNotSelectable):
            mp.fan_out_rollup(blind, mart="nope")


class CertifiedKinds(unittest.TestCase):
    """N-mart_plan-6: a declared kind is checked before it is counted."""

    def test_budget_problems_refuses_an_uncertified_kind(self) -> None:
        built = mp.argmax_profile(EVIDENCE, mart="argmax_kinds")
        self.assertEqual([], mp.budget_problems(built))
        tampered = dataclasses.replace(
            built,
            columns=tuple(
                c.model_copy(update={"kind": MartColumnKind.RANKED})
                if c.name == "parent_name"
                else c
                for c in built.columns
            ),
        )
        problems = mp.budget_problems(tampered)
        self.assertTrue(problems)
        self.assertIn("parent_name", problems[0])
        self.assertIn("declared kind 'ranked'", problems[0])
        undeclared = dataclasses.replace(
            built,
            columns=tuple(
                c.model_copy(update={"kind": None}) if c.name == "top_label" else c
                for c in built.columns
            ),
        )
        problems = mp.budget_problems(undeclared)
        self.assertTrue(any("top_label" in p and "declare no kind" in p for p in problems), problems)
        # Legacy build_star plans carry no column contract and are not judged.
        self.assertEqual([], mp.kind_certification_problems(
            mp.build_star(
                mart="legacy", parent="accounts", parent_keys=("account_id",),
                key_columns=("account_key",),
                measures=(mp.Measure(column="n", expr="COUNT(*)"),),
            )
        ))


class WindowsReadDeclaredDefaults(unittest.TestCase):
    """N-mart_plan-2 Part A: post-aggregate windows read the mart's DEFAULTED
    values, never the raw m_<i> aggregate aliases."""

    def test_post_windows_read_declared_defaults_not_raw_aggregates(self) -> None:
        tables = _nullable(TABLES, "subscriptions", "amount")
        built = mp.temporal_grid(EVIDENCE, mart="temporal_grid_mart")
        ops = built.plan.ops
        window_idx = [i for i, op in enumerate(ops) if op.kind is MartOpKind.WINDOW]
        derive_idx = [
            i for i, op in enumerate(ops)
            if op.kind is MartOpKind.DERIVE and "COALESCE" in op.details.get("select", "")
        ]
        self.assertTrue(window_idx and derive_idx)
        self.assertGreater(window_idx[-1], derive_idx[-1], "WINDOW must follow the named DERIVE")
        window_select = ops[window_idx[-1]].details["select"]
        self.assertIsNone(re.search(r"\bm_\d+\b", window_select), window_select)
        self.assertIn('SUM("period_amount") OVER', window_select)
        self.assertIn('LAG("period_amount", 1, 0) OVER', window_select)

        task = build_task(built, "proof__temporal_defaults", tables=tables)
        sql = ref.compile_plan_sql(task, task.marts[0])
        con = duckdb.connect(":memory:")
        try:
            for table in task.tables:
                ref.create_table(con, table)
            con.execute("INSERT INTO accounts VALUES (1, 'one', 'active'), (2, 'two', 'trial')")
            con.execute(
                "INSERT INTO subscriptions VALUES "
                "(1, 1, NULL, 'paid', 'a', NULL, DATE '2024-01-03'), "  # Jan: all NULL
                "(2, 1, NULL, 'paid', 'b', NULL, DATE '2024-01-09'), "
                "(3, 1, NULL, 'paid', 'c', 5, DATE '2024-02-01'), "     # Feb: 5
                "(4, 2, NULL, 'paid', 'd', 3, DATE '2024-01-15'), "     # Jan: 3
                "(5, 2, NULL, 'paid', 'e', NULL, DATE '2024-02-20'), "  # Feb: all NULL
                "(6, 2, NULL, 'paid', 'f', 4, DATE '2024-03-05')"       # Mar: 4
            )
            rows = {(r["entity_key"], str(r["period_start"])): r for r in _fetch(con, sql)}
        finally:
            con.close()
        jan1 = rows[(1, "2024-01-01")]
        feb1 = rows[(1, "2024-02-01")]
        self.assertEqual(0, jan1["period_amount"])
        self.assertEqual(0, jan1["running_amount"])
        self.assertEqual(0, feb1["prev_period_amount"])
        self.assertEqual("up", feb1["trend"])
        mar2 = rows[(2, "2024-03-01")]
        self.assertEqual(0, mar2["prev_period_amount"])
        self.assertEqual("up", mar2["trend"])
        self.assertEqual(3, mar2["running_amount"] - 4)


class TemporalGridGrain(unittest.TestCase):
    """N-mart_plan-2 Part B: grain keys cannot be NULL by construction."""

    def test_temporal_grid_declines_a_nullable_optional_owner_link(self) -> None:
        with self.assertRaises(mp.ShapeNotSelectable) as ctx:
            mp.temporal_grid(
                dataclasses.replace(EVIDENCE, owner_key_nullable=True), mart="x"
            )
        self.assertIn("NullGrainKeyError", str(ctx.exception))
        with self.assertRaises(mp.ShapeNotSelectable) as ctx:
            mp.temporal_grid(
                dataclasses.replace(EVIDENCE, bridge_timestamp_nullable=True), mart="x"
            )
        self.assertIn("period_start", str(ctx.exception))
        # The other shapes do not grain on the link and are unaffected.
        mp.orphan_coverage(dataclasses.replace(EVIDENCE, owner_key_nullable=True), mart="ok")

    def test_temporal_grid_gold_freezes_on_every_population(self) -> None:
        # NOT NULL fk + required link (STRICT_*): the schema temporal_grid may
        # grain on. `owner_link_optional` is False to match — a required link
        # has no orphan witness to declare.
        strict = dataclasses.replace(EVIDENCE, owner_link_optional=False)
        built = mp.temporal_grid(strict, mart="temporal_grid_mart")
        task = _strict_task(built, "proof__temporal_freeze")
        mart = task.marts[0]
        sql = ref.compile_plan_sql(task, mart)
        results = {}
        for pop in task.populations:
            rows = generate_rows(task, pop.name)
            con = duckdb.connect(":memory:")
            try:
                _load(con, task, rows)
                mart_rows = runner_mod.sort_mart_rows(ref.execute_mart(con, mart, sql), mart)
            finally:
                con.close()
            for row in mart_rows:
                for key in mart.key_columns:
                    self.assertIsNotNone(row[key], (pop.name.value, row))
            results[pop.name] = runner_mod.RunResult(
                population=pop.name,
                stage1_counts={t.name: len(rows.get(t.name) or []) for t in task.tables},
                mart_rows={mart.name: mart_rows},
            )
        with tempfile.TemporaryDirectory() as tmp:
            bundle = gold_mod.freeze_gold(task, results, Path(tmp) / "answer_key")
        self.assertEqual(set(bundle.stage2_csv), {p.name.value for p in task.populations})


class ProseStatesEveryReachableArm(unittest.TestCase):
    """N-mart_plan-3: every default that fires on a NON-EMPTY group is stated."""

    ARM = re.compile(r"none of .*?rows carries|lacks a .* value|itself (is )?missing")

    def test_prose_states_every_default_that_fires_on_a_non_empty_group(self) -> None:
        tables = _nullable(TABLES, "subscriptions", "amount")
        tables = _nullable(tables, "accounts", "account_name")
        judged = {MartColumnKind.AGGREGATED, MartColumnKind.PASSTHROUGH}
        for name, builder in SHAPES:
            built = builder(EVIDENCE, mart=f"{name}_mart")
            task = build_task(built, f"proof__{name}_arms", tables=tables)
            mart = task.marts[0]
            kinds = {c.name: c.kind for c in mart.columns}
            sql = ref.compile_plan_sql(task, mart)
            stripped = attacks._apply_kind(attacks.AttackKind.NO_NULL_DEFAULT, sql, mart, None)
            if stripped is None:
                continue
            con = duckdb.connect(":memory:")
            try:
                for table in task.tables:
                    ref.create_table(con, table)
                # Parent 2 is MATCHED, has ONE child whose amount is NULL, and
                # its own name is NULL; parent 1 is an ordinary control.
                con.execute(
                    "INSERT INTO accounts VALUES (1, 'one', 'active'), (2, NULL, 'trial')"
                )
                con.execute("INSERT INTO plans VALUES (10, 'basic')")
                con.execute(
                    "INSERT INTO subscriptions VALUES "
                    "(1, 1, 10, 'paid', 'a', 7, DATE '2024-01-03'), "
                    "(2, 2, 10, 'paid', 'b', NULL, DATE '2024-02-01')"
                )
                gold_rows = _fetch(con, sql)
                literal_rows = _fetch(con, stripped)
            finally:
                con.close()
            key = tuple(mart.key_columns)
            literal_by_key = {tuple(r[k] for k in key): r for r in literal_rows}
            descriptions = {c.name: c.description for c in mart.columns}
            count_col = next(
                (c for c in ("child_count", "event_count", "link_count") if c in descriptions),
                None,
            )
            for gold in gold_rows:
                literal = literal_by_key[tuple(gold[k] for k in key)]
                if count_col is not None and not gold[count_col]:
                    continue  # the empty arm is stated already
                for column, value in gold.items():
                    if literal[column] == value or kinds[column] not in judged:
                        # Derived/categorical/ranked columns read the DEFAULTED
                        # measures and describe themselves in those terms.
                        continue
                    with self.subTest(shape=name, column=column):
                        self.assertRegex(
                            descriptions[column],
                            self.ARM,
                            f"{name}.{column}: gold {value!r} vs literal "
                            f"{literal[column]!r} on a non-empty group, and the "
                            "description does not state that arm",
                        )

    def test_no_null_capable_measure_describes_an_unreachable_empty_arm(self) -> None:
        for builder in (mp.temporal_grid, mp.orphan_coverage):
            built = builder(EVIDENCE, mart="x")
            for column in built.columns:
                with self.subTest(shape=built.shape.shape_name, column=column.name):
                    self.assertNotIn("when empty", column.description)


class TextTieBreakProse(unittest.TestCase):
    """N-mart_plan-5: the collation is stated, 'alphabetically' is gone."""

    def test_argmax_text_tie_break_prose_states_case_sensitivity(self) -> None:
        built = mp.argmax_profile(EVIDENCE, mart="argmax_case")
        top_label = next(c for c in built.columns if c.name == "top_label")
        for text in (top_label.description,) + tuple(
            op.description for op in built.plan.ops if op.kind is MartOpKind.EXTREMA
        ):
            self.assertIn("case-sensitive", text)
            self.assertIn("uppercase", text)
            self.assertNotIn("alphabetically", text)
        for column in built.columns:
            self.assertNotIn("alphabetically", column.description)
        text_op = mp.extrema_op(
            source="s", name="n", partition_by=("k",), measure="m", tie_break="lbl",
            projections=(("lbl", "top"),), tie_break_is_text=True,
        )
        plain_op = mp.extrema_op(
            source="s", name="n", partition_by=("k",), measure="m", tie_break="rid",
            projections=(("rid", "top"),),
        )
        self.assertIn("case-sensitive", text_op.description)
        self.assertNotIn("case-sensitive", plain_op.description)


class UndefaultedMeasureProse(unittest.TestCase):
    """build_rollup names every emitted aggregate without a proven final
    missing-result replacement and states the rule a solver must not 'fix'.
    Reached mainly by recovered (dbt) rollups
    — none of the five library shapes leaves a measure undefaulted — which is
    why this is pinned here rather than by the shape corpus.
    """

    @staticmethod
    def _agg_description(measure: mp.Measure) -> str:
        built = mp.build_rollup(
            mart="probe_mart",
            shape_name="probe",
            parent="subscriptions",
            keys=(
                mp.KeyColumn(
                    column="account_key", type=ColumnType.BIGINT,
                    description="The owning account.", source="account_id",
                ),
            ),
            parent_carry=(("amount", "amt"),),
            measures=(
                mp.Measure(column="n_rows", expr="COUNT(*)", description="Rows."),
                measure,
            ),
            enforce_budget=False,
        )
        aggs = [
            op for op in built.plan.ops
            if op.kind in (MartOpKind.AGGREGATE, MartOpKind.FILTERED_AGGREGATE)
        ]
        assert len(aggs) == 1, [op.kind for op in built.plan.ops]
        return aggs[0].description

    def test_undefaulted_sum_and_max_name_the_missing_input_rule(self) -> None:
        for expr in ("SUM(amt)", "MAX(amt)", "MIN(amt)"):
            with self.subTest(expr=expr):
                text = self._agg_description(
                    mp.Measure(
                        column="total_amt", expr=expr, type=ColumnType.DECIMAL,
                        description="Total.",
                    )
                )
                self.assertIn(
                    "These aggregate outputs have no declared replacement for "
                    "an empty result: total_amt. Preserve an empty result as "
                    "empty, not 0: a missing input value contributes nothing, "
                    "and an output whose matching input values are all missing "
                    "is empty, whether it reads every matching row or only the "
                    "rows that qualify for its condition.",
                    text,
                )
                # n_rows is a COUNT: never NULL, never listed.
                self.assertNotIn("n_rows, a group", text)

    def test_defaulted_coalesced_and_count_measures_do_not_get_the_rule(self) -> None:
        cases = {
            "defaulted": mp.Measure(
                column="total_amt", expr="SUM(amt)", null_default="0",
                type=ColumnType.DECIMAL, description="Total.",
            ),
            "inner coalesce": mp.Measure(
                column="total_amt", expr="COALESCE(SUM(amt), 0)",
                type=ColumnType.DECIMAL, description="Total.",
            ),
            "count": mp.Measure(
                column="total_amt", expr="COUNT(amt)", description="Counted.",
            ),
        }
        for label, measure in cases.items():
            with self.subTest(case=label):
                self.assertNotIn(
                    "aggregate outputs have no declared replacement",
                    self._agg_description(measure),
                )

    def test_the_rule_is_declarative_and_sayable(self) -> None:
        """The sentence reaches authored prose through prose_fidelity, so it
        must clear the operator lexicon and name only the mart's own column."""
        from elt_taskgen.review import declarative_prose

        text = self._agg_description(
            mp.Measure(
                column="total_amt", expr="SUM(amt)", type=ColumnType.DECIMAL,
                description="Total.",
            )
        )
        identifiers = frozenset(
            {"subscriptions", "amount", "account_id", "probe_mart", "account_key",
             "n_rows", "total_amt"}
        )
        self.assertEqual([], declarative_prose.operator_problems(text, identifiers))
        sentence = text.split("These aggregate outputs", 1)[1]
        self.assertEqual(
            {"total_amt"}, set(re.findall(r"[a-z0-9]+(?:_[a-z0-9]+)+", sentence)),
        )

    def test_join_counterfactual_witness_defaults_to_constructed(self) -> None:
        """`StarShape.join_counterfactual_witness` defaults True (the legacy star
        and every library shape construct the childless-parent witness); an
        adapter whose plans are RECOVERED from vendor SQL opts out explicitly."""
        field = next(
            f for f in dataclasses.fields(mp.StarShape)
            if f.name == "join_counterfactual_witness"
        )
        self.assertIs(field.default, True)
        field = next(
            f for f in dataclasses.fields(mp.StarShape)
            if f.name == "dedupe_counterfactual_witness"
        )
        self.assertIs(field.default, True)

"""Tests for generation/: populations, source_data, mart_plan.

WHY THIS EXISTS
The generation layer decides whether the five populations can actually
separate wrong logic from right logic, and whether every artifact is
byte-deterministic. These tests pin: the five-population plan and its coverage
validator, per-column RNG stream independence (the rejected predecessor's S1
defect), constraint-aware row generation reproducing the demo attack matrix on
REAL DuckDB executions, renderer byte-determinism and pagination boundaries,
and mart-plan validation / attack-surface enumeration.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import duckdb

from elt_taskgen.demo_fixture import (
    COUNTERFACTUAL_EXPECTED_MART,
    COUNTERFACTUAL_LITERAL_ROWS,
    MART_NAME,
    REFERENCE_SQL,
    demo_mart_plan,
    demo_task,
)
from elt_taskgen.generation import mart_plan, populations, source_data
from elt_taskgen.generation.difficulty_profiles import (
    CHALLENGING_DIFFICULTY_PROFILE,
    GenerationDifficultyProfile,
)
from elt_taskgen.reference import solution as ref_solution
from elt_taskgen.models import (
    AttackKind,
    ColumnSpec,
    ColumnType,
    MartOp,
    MartOpKind,
    MartPlan,
    PopulationName,
    Relationship,
    TableSpec,
    derive_seed,
)

P = PopulationName


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_DUCK_TYPES = {
    ColumnType.INTEGER: "INTEGER",
    ColumnType.BIGINT: "BIGINT",
    ColumnType.FLOAT: "DOUBLE",
    ColumnType.DECIMAL: "DOUBLE",
    ColumnType.TEXT: "VARCHAR",
    ColumnType.BOOLEAN: "BOOLEAN",
    ColumnType.DATE: "DATE",
    ColumnType.TIMESTAMP: "TIMESTAMP",
    ColumnType.JSON: "VARCHAR",
}


def _load_duckdb(task, rows):
    con = duckdb.connect(":memory:")
    for tspec in task.tables:
        cols = ", ".join(f'"{c.name}" {_DUCK_TYPES[c.type]}' for c in tspec.columns)
        con.execute(f'CREATE TABLE "{tspec.name}" ({cols})')
        table_rows = rows.get(tspec.name, [])
        if table_rows:
            placeholders = ", ".join("?" for _ in tspec.columns)
            con.executemany(
                f'INSERT INTO "{tspec.name}" VALUES ({placeholders})',
                [[r.get(c.name) for c in tspec.columns] for r in table_rows],
            )
    return con


def _rows_close(a, b):
    """Order-insensitive row-set comparison with relative float tolerance."""
    if len(a) != len(b):
        return False

    def norm(row):
        out = []
        for v in row:
            if isinstance(v, float):
                out.append(round(v, 4))
            else:
                out.append(v)
        return tuple(out)

    return sorted(map(norm, a)) == sorted(map(norm, b))


def _dir_hashes(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _attack_sql(task, name):
    for case in task.attack_cases:
        if case.name == name:
            return case.mutation
    raise KeyError(name)


# ---------------------------------------------------------------------------
# populations.py
# ---------------------------------------------------------------------------

class TestDefaultPopulations(unittest.TestCase):
    def test_all_five_with_derived_seeds(self):
        pops = populations.default_populations("t__x", {"a": 1000, "b": 3000})
        names = [p.name for p in pops]
        self.assertEqual(set(names), set(PopulationName))
        self.assertEqual(len(names), 5)
        for pop in pops:
            self.assertEqual(pop.seed, derive_seed("t__x", pop.name.value))

    def test_primary_resampled_share_scale_and_stress_scales_up(self):
        pops = {p.name: p for p in populations.default_populations("t__x", {"a": 1000})}
        self.assertEqual(pops[P.PRIMARY].scale, {"a": 1000})
        self.assertEqual(pops[P.PRIMARY].scale, pops[P.RESAMPLED].scale)
        self.assertEqual(pops[P.STRESS].scale, {"a": 10000})
        self.assertLessEqual(pops[P.DEVELOPMENT].scale["a"], 8)
        self.assertGreaterEqual(pops[P.DEVELOPMENT].scale["a"], 2)

    def test_default_counterfactual_placeholder_is_rejected_by_coverage(self):
        task = demo_task()
        pops = populations.default_populations(
            task.task_id, {"customers": 1000, "orders": 3000, "order_items": 9000}
        )
        candidate = task.model_copy(update={"populations": pops})
        problems = populations.validate_population_coverage(candidate)
        self.assertTrue(
            any("counterfactual" in p for p in problems),
            f"placeholder counterfactual must be flagged, got: {problems}",
        )


class TestDifficultyProfiles(unittest.TestCase):
    def test_challenging_scale_is_deterministic_and_idempotent(self):
        original = demo_task()
        scaled = populations.apply_data_scale_profile(
            original, CHALLENGING_DIFFICULTY_PROFILE
        )
        again = populations.apply_data_scale_profile(
            scaled, CHALLENGING_DIFFICULTY_PROFILE
        )

        primary = scaled.population(P.PRIMARY)
        self.assertGreaterEqual(
            sum(primary.scale.values()),
            CHALLENGING_DIFFICULTY_PROFILE.synthetic_primary_row_floor,
        )
        self.assertLessEqual(
            sum(primary.scale.values()),
            CHALLENGING_DIFFICULTY_PROFILE.max_primary_rows_per_task,
        )
        self.assertLessEqual(
            max(primary.scale.values()),
            CHALLENGING_DIFFICULTY_PROFILE.max_primary_rows_per_table,
        )
        self.assertEqual(primary.scale, scaled.population(P.RESAMPLED).scale)
        self.assertEqual(
            scaled.population(P.DEVELOPMENT), original.population(P.DEVELOPMENT)
        )
        self.assertEqual(
            scaled.population(P.COUNTERFACTUAL), original.population(P.COUNTERFACTUAL)
        )
        self.assertEqual(
            scaled.population(P.STRESS).scale, original.population(P.STRESS).scale
        )
        self.assertEqual(scaled, again)
        self.assertEqual(populations.validate_population_coverage(scaled), [])

    def test_literal_primary_is_not_synthetically_inflated(self):
        original = demo_task()
        literal = original.population(P.COUNTERFACTUAL).literal_rows
        changed = []
        for population in original.populations:
            if population.name is P.PRIMARY:
                population = population.model_copy(
                    update={"scale": {}, "literal_rows": literal}
                )
            changed.append(population)
        real_task = original.model_copy(update={"populations": tuple(changed)})
        self.assertEqual(
            populations.apply_data_scale_profile(
                real_task, CHALLENGING_DIFFICULTY_PROFILE
            ),
            real_task,
        )

    def test_scale_envelope_fails_closed(self):
        impossible = GenerationDifficultyProfile(
            name="impossible",
            synthetic_primary_row_floor=1_000,
            max_scale_multiplier=2,
            max_primary_rows_per_table=100,
            max_primary_rows_per_task=1_000,
        )
        with self.assertRaisesRegex(ValueError, "safety cap"):
            impossible.scale_hint({"a": 10, "b": 10})

    def test_pool_specific_mart_parameters(self):
        self.assertEqual(
            CHALLENGING_DIFFICULTY_PROFILE.mart_parameters(
                pool="synsql", default_budget=2
            ),
            (4, True, 2),
        )
        self.assertEqual(
            CHALLENGING_DIFFICULTY_PROFILE.mart_parameters(
                pool="wikidbs", default_budget=2
            ),
            (2, True, 2),
        )
        self.assertEqual(
            CHALLENGING_DIFFICULTY_PROFILE.mart_parameters(
                pool="dlt", default_budget=2
            ),
            (2, True, 1),
        )
        self.assertEqual(
            CHALLENGING_DIFFICULTY_PROFILE.mart_parameters(
                pool="schemapile",
                default_budget=3,
                default_spread_grains=True,
                default_max_per_shape=3,
            ),
            (5, True, 2),
        )


class TestPopulationCoverage(unittest.TestCase):
    def test_demo_task_is_clean(self):
        self.assertEqual(populations.validate_population_coverage(demo_task()), [])

    def test_missing_population_flagged(self):
        task = demo_task()
        four = tuple(p for p in task.populations if p.name is not P.STRESS)
        problems = populations.validate_population_coverage(
            task.model_copy(update={"populations": four})
        )
        self.assertIn("missing population: stress", problems)

    def test_scale_mismatch_flagged(self):
        task = demo_task()
        pops = []
        for pop in task.populations:
            if pop.name is P.RESAMPLED:
                pop = pop.model_copy(update={"scale": {"customers": 5}})
            pops.append(pop)
        problems = populations.validate_population_coverage(
            task.model_copy(update={"populations": tuple(pops)})
        )
        self.assertTrue(any("share the same scale" in p for p in problems))

    def test_counterfactual_without_literals_needs_targeting_conditions(self):
        task = demo_task()
        pops = []
        for pop in task.populations:
            if pop.name is P.COUNTERFACTUAL:
                pop = pop.model_copy(
                    update={"literal_rows": {}, "conditions": ("some vague prose",),
                            "scale": {"customers": 3}}
                )
            pops.append(pop)
        problems = populations.validate_population_coverage(
            task.model_copy(update={"populations": tuple(pops)})
        )
        for kind in ("inner_join", "constants", "no_dedup", "no_null_default"):
            self.assertTrue(
                any(kind in p for p in problems),
                f"required kind {kind} not demanded; problems: {problems}",
            )

    def test_literal_rows_enum_violation_flagged(self):
        task = demo_task()
        bad_literals = dict(COUNTERFACTUAL_LITERAL_ROWS)
        bad_literals["orders"] = (
            {"order_id": 1, "customer_id": 10, "status": "shipped"},
        )
        pops = []
        for pop in task.populations:
            if pop.name is P.COUNTERFACTUAL:
                pop = pop.model_copy(update={"literal_rows": bad_literals})
            pops.append(pop)
        problems = populations.validate_population_coverage(
            task.model_copy(update={"populations": tuple(pops)})
        )
        self.assertTrue(any("outside enum domain" in p for p in problems))

    def test_literal_rows_broken_required_fk_flagged(self):
        task = demo_task()
        bad_literals = dict(COUNTERFACTUAL_LITERAL_ROWS)
        bad_literals["order_items"] = (
            {"order_id": 9999, "quantity": 1, "unit_price": 1.0},
        )
        pops = []
        for pop in task.populations:
            if pop.name is P.COUNTERFACTUAL:
                pop = pop.model_copy(update={"literal_rows": bad_literals})
            pops.append(pop)
        problems = populations.validate_population_coverage(
            task.model_copy(update={"populations": tuple(pops)})
        )
        self.assertTrue(any("references missing orders key" in p for p in problems))

    def test_required_relationship_cycle_is_rejected_before_generation(self):
        task = demo_task()
        relationships = (
            Relationship(
                child_table="customers",
                child_columns=("customer_id",),
                parent_table="orders",
                parent_columns=("order_id",),
                required=True,
            ),
            Relationship(
                child_table="orders",
                child_columns=("order_id",),
                parent_table="order_items",
                parent_columns=("order_id",),
                required=True,
            ),
            Relationship(
                child_table="order_items",
                child_columns=("order_id",),
                parent_table="orders",
                parent_columns=("order_id",),
                required=True,
            ),
        )
        cyclic = task.model_copy(update={"relationships": relationships})

        with self.assertRaisesRegex(ValueError, "empty parent pool"):
            source_data.generate_rows(cyclic, P.DEVELOPMENT)
        problems = populations.validate_population_coverage(cyclic)
        self.assertTrue(
            any(
                "required relationship cycle" in problem
                and "empty parent pool" in problem
                for problem in problems
            ),
            problems,
        )

    def test_optional_relationship_cycle_remains_generatable(self):
        task = demo_task()
        customers = task.table("customers")
        customers = customers.model_copy(
            update={
                "columns": customers.columns
                + (
                    ColumnSpec(
                        name="item_id", type=ColumnType.INTEGER, nullable=True
                    ),
                )
            }
        )
        optional_cycle = task.model_copy(
            update={
                "tables": tuple(
                    customers if table.name == "customers" else table
                    for table in task.tables
                ),
                "relationships": task.relationships
                + (
                    Relationship(
                        child_table="customers",
                        child_columns=("item_id",),
                        parent_table="order_items",
                        parent_columns=("order_id",),
                        required=False,
                    ),
                ),
            }
        )

        self.assertEqual(
            populations.validate_population_coverage(optional_cycle), []
        )
        source_data.generate_rows(optional_cycle, P.DEVELOPMENT)

    def test_fully_literal_required_cycle_remains_generatable(self):
        task = demo_task()
        literal_rows = dict(COUNTERFACTUAL_LITERAL_ROWS)
        literal_rows["customers"] = tuple(
            row
            for row in literal_rows["customers"]
            if row["customer_id"] in (11, 12)
        )
        relationships = (
            task.relationships[0].model_copy(update={"required": True}),
            task.relationships[1],
            Relationship(
                child_table="customers",
                child_columns=("customer_id",),
                parent_table="orders",
                parent_columns=("customer_id",),
                required=True,
            ),
        )
        all_literal = task.model_copy(
            update={
                "relationships": relationships,
                "populations": tuple(
                    population.model_copy(
                        update={"scale": {}, "literal_rows": literal_rows}
                    )
                    for population in task.populations
                ),
            }
        )

        self.assertEqual(populations.validate_population_coverage(all_literal), [])
        for population in P:
            source_data.generate_rows(all_literal, population)


# ---------------------------------------------------------------------------
# source_data.py — RNG streams
# ---------------------------------------------------------------------------

class TestColumnRng(unittest.TestCase):
    def test_same_identity_same_stream(self):
        a = source_data.column_rng("t__x", P.PRIMARY, "orders", "order_id")
        b = source_data.column_rng("t__x", P.PRIMARY, "orders", "order_id")
        self.assertEqual([a.random() for _ in range(32)], [b.random() for _ in range(32)])

    def test_streams_are_disjoint_across_columns_tables_populations(self):
        identities = [
            ("t__x", P.PRIMARY, "orders", "a"),
            ("t__x", P.PRIMARY, "orders", "b"),
            ("t__x", P.PRIMARY, "items", "a"),
            ("t__x", P.RESAMPLED, "orders", "a"),
            ("t__y", P.PRIMARY, "orders", "a"),
        ]
        sequences = [
            tuple(source_data.column_rng(*ident).randint(0, 10**9) for _ in range(32))
            for ident in identities
        ]
        self.assertEqual(len(set(sequences)), len(sequences),
                         "two integer column streams shared a sequence")

    def test_inserting_a_column_does_not_shift_other_streams(self):
        """Regression against the rejected predecessor's shared-sequence defect."""
        task = demo_task()
        base = source_data.generate_rows(task, P.PRIMARY)

        tables = []
        for tspec in task.tables:
            if tspec.name == "order_items":
                tspec = tspec.model_copy(
                    update={
                        "columns": tspec.columns
                        + (ColumnSpec(name="note", type=ColumnType.TEXT,
                                      description="extra column"),)
                    }
                )
            tables.append(tspec)
        widened = task.model_copy(update={"tables": tuple(tables)})
        wide = source_data.generate_rows(widened, P.PRIMARY)

        self.assertEqual(
            [r["quantity"] for r in base["order_items"]],
            [r["quantity"] for r in wide["order_items"]],
        )
        self.assertEqual(
            [r["unit_price"] for r in base["order_items"]],
            [r["unit_price"] for r in wide["order_items"]],
        )
        self.assertEqual(base["orders"], wide["orders"])


# ---------------------------------------------------------------------------
# source_data.py — realized row counts (the declared scale is not the answer)
# ---------------------------------------------------------------------------

class TestRealizedRowCount(unittest.TestCase):
    """The scale_hint == realized-count identity is BROKEN, and stays broken.

    WHY THIS EXISTS
    The extract-load reward is `upstream_eval.compare_stage1`: strict binary
    over a vector of per-table row counts and nothing else. While the generator
    realized `PopulationSpec.scale` exactly, that vector was a handful of round
    numbers a solver could read off the task documentation and submit without
    opening an artifact — measured by the `fabricate_scale_hint` probe in
    verification/gates.py, which scored 1.0. These tests pin the properties
    that keep it at 0.0 — divergent, banded, non-round, reproducible across
    processes — without breaking the two invariants the divergence sits on top
    of: the memorization pair's shared count vector, and referential integrity.
    """

    DECLARED = (50, 51, 60, 97, 200, 1000, 3000, 9000, 20000, 60000, 701352)

    def test_declared_is_never_the_realized_count(self):
        for declared in self.DECLARED:
            for table in ("customers", "orders", "order_items", "zzz"):
                got = source_data.realized_row_count("t__x", table, declared)
                self.assertNotEqual(got, declared, f"{table}@{declared}")

    def test_divergence_stays_inside_the_declared_band(self):
        lo = source_data.REALIZED_DIVERGENCE_MIN_PCT
        hi = source_data.REALIZED_DIVERGENCE_MAX_PCT
        for declared in self.DECLARED:
            for table in ("customers", "orders", "order_items", "zzz"):
                got = source_data.realized_row_count("t__x", table, declared)
                delta = abs(got - declared)
                # At least MIN% off, so the declared number is never the
                # answer; at most MAX% off (the round-number nudge spends the
                # one row of headroom the draw reserved), so prose that says
                # "approximately <declared>" stays TRUE.
                self.assertGreaterEqual(
                    delta * 100, lo * declared, f"{table}@{declared}"
                )
                self.assertLessEqual(delta * 100, hi * declared, f"{table}@{declared}")

    def test_realized_counts_are_not_round(self):
        for declared in self.DECLARED:
            for table in ("customers", "orders", "order_items", "zzz"):
                got = source_data.realized_row_count("t__x", table, declared)
                self.assertNotEqual(got % 10, 0, f"{table}@{declared} is round")

    def test_small_scales_are_realized_exactly(self):
        # A few percent of a handful of rows is a rounding accident, not a
        # divergence — and development (the only population this reaches on
        # the demo task) is solver-VISIBLE and ungraded anyway.
        for declared in (0, 1, 2, 4, 8, 49):
            self.assertEqual(
                source_data.realized_row_count("t__x", "orders", declared), declared
            )

    def test_collapsed_band_fails_closed(self):
        # Unreachable with the shipped constants (MIN_SCALE=50 always leaves
        # room), so it is pinned by forcing the collapse: the alternative to
        # raising is quietly realizing the declared count, i.e. handing the
        # extract-load answer back to whoever reads the documentation.
        with unittest.mock.patch.object(
            source_data, "REALIZED_DIVERGENCE_MIN_SCALE", 10
        ):
            with self.assertRaises(ValueError) as ctx:
                source_data.realized_row_count("t__x", "orders", 12)
        self.assertIn("divergence band", str(ctx.exception))

    def test_deterministic_across_processes(self):
        # sha256 via derive_seed, never a runtime Random() and never the clock:
        # a SEPARATE interpreter (fresh PYTHONHASHSEED) must agree exactly.
        script = (
            "from elt_taskgen.generation.source_data import realized_row_count as r;"
            "print([r('t__x', t, n) for t in ('a','b') "
            "for n in (50, 1000, 3000, 60000)])"
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": "random"},
        ).stdout.strip()
        here = [
            source_data.realized_row_count("t__x", t, n)
            for t in ("a", "b")
            for n in (50, 1000, 3000, 60000)
        ]
        self.assertEqual(out, str(here))

    def test_keyed_on_what_the_memorization_pair_shares(self):
        """NOT on the population name — primary and resampled must still agree.

        `validate_population_coverage` forces primary.scale == resampled.scale
        and `gates._gate_el_data_sensitivity` FAILS a task whose two frozen
        count vectors differ under an equal declared scale. So the divergence
        is derived from (task_id, table, declared count), which the pair shares
        — and populations with a DIFFERENT declared scale still move.
        """
        task = demo_task()
        primary = source_data.generate_rows(task, P.PRIMARY)
        resampled = source_data.generate_rows(task, P.RESAMPLED)
        stress = source_data.generate_rows(task, P.STRESS)
        def counts(rows):
            return {t: len(r) for t, r in rows.items()}

        self.assertEqual(counts(primary), counts(resampled))
        self.assertNotEqual(counts(primary), counts(stress))

    def test_submitting_the_declared_scale_misses_every_table(self):
        task = demo_task()
        for pop in (P.PRIMARY, P.RESAMPLED, P.STRESS):
            declared = task.population(pop).scale
            realized = {
                t: len(r) for t, r in source_data.generate_rows(task, pop).items()
            }
            for table, want in declared.items():
                self.assertNotEqual(
                    realized[table],
                    want,
                    f"{pop.value}/{table}: the declared scale IS the answer",
                )


# ---------------------------------------------------------------------------
# source_data.py — row generation
# ---------------------------------------------------------------------------

class TestGenerateRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = demo_task()
        cls.rows = {
            pop: source_data.generate_rows(cls.task, pop) for pop in PopulationName
        }

    def _status_by_customer(self, pop):
        by_customer = {}
        for order in self.rows[pop]["orders"]:
            cid = order["customer_id"]
            if cid is not None:
                by_customer.setdefault(cid, set()).add(order["status"])
        return by_customer

    def test_counterfactual_literal_rows_verbatim(self):
        rows = self.rows[P.COUNTERFACTUAL]
        for table, literal in COUNTERFACTUAL_LITERAL_ROWS.items():
            self.assertEqual(rows[table], [dict(r) for r in literal])

    def test_development_full_coverage(self):
        rows = self.rows[P.DEVELOPMENT]
        self.assertEqual(len(rows["customers"]), 2)
        self.assertEqual(len(rows["orders"]), 4)
        self.assertEqual(len(rows["order_items"]), 8)
        # no NULL FKs
        self.assertTrue(all(o["customer_id"] is not None for o in rows["orders"]))
        # every customer has BOTH statuses (INNER JOIN indistinguishable)
        by_customer = self._status_by_customer(P.DEVELOPMENT)
        for c in rows["customers"]:
            self.assertEqual(by_customer.get(c["customer_id"]), {"cancelled", "completed"})
        # every order has at least one item (no NULL totals anywhere)
        order_ids = {o["order_id"] for o in rows["orders"]}
        item_order_ids = {i["order_id"] for i in rows["order_items"]}
        self.assertEqual(order_ids, order_ids & item_order_ids)
        # no duplicate rows
        as_tuples = [tuple(sorted(o.items())) for o in rows["orders"]]
        self.assertEqual(len(as_tuples), len(set(as_tuples)))

    def test_primary_conditions(self):
        rows = self.rows[P.PRIMARY]
        # The declared scale is the INTENDED size; the realized counts are a
        # few percent off it BY DESIGN (see source_data.realized_row_count —
        # an exactly-realized scale made the extract-load answer a vector of
        # round numbers readable off the documentation). The invariant that
        # matters here is the band, not the number.
        declared = self.task.population(P.PRIMARY).scale
        for table, want in declared.items():
            got = len(rows[table])
            self.assertNotEqual(got, want, f"{table}: realized == declared scale")
            self.assertLessEqual(
                abs(got - want) * 100,
                source_data.REALIZED_DIVERGENCE_MAX_PCT * want,
                f"{table}: realized {got} left the divergence band around {want}",
            )
        customer_ids = {c["customer_id"] for c in rows["customers"]}
        by_customer = self._status_by_customer(P.PRIMARY)
        # some customers have no orders at all
        childless = customer_ids - set(by_customer)
        self.assertGreater(len(childless), 0)
        # some customers have orders but never a completed one
        only_uncompleted = [
            cid for cid, statuses in by_customer.items() if "completed" not in statuses
        ]
        self.assertGreater(len(only_uncompleted), 0)
        # cancelled orders present; enum domain respected
        statuses = {o["status"] for o in rows["orders"]}
        self.assertEqual(statuses, {"cancelled", "completed"})
        # some orders have NULL customer_id; non-NULL FKs are valid
        cids = [o["customer_id"] for o in rows["orders"]]
        self.assertIn(None, cids)
        self.assertTrue(set(c for c in cids if c is not None) <= customer_ids)
        # required link: every item references a real order
        order_ids = {o["order_id"] for o in rows["orders"]}
        self.assertTrue(all(i["order_id"] in order_ids for i in rows["order_items"]))
        # business-key uniqueness: order_id unique among distinct rows
        self.assertEqual(len(order_ids), len(rows["orders"]))

    def test_resampled_same_scale_new_ids_new_data(self):
        primary = self.rows[P.PRIMARY]
        resampled = self.rows[P.RESAMPLED]
        self.assertEqual(len(primary["customers"]), len(resampled["customers"]))
        p_ids = {c["customer_id"] for c in primary["customers"]}
        r_ids = {c["customer_id"] for c in resampled["customers"]}
        self.assertEqual(p_ids & r_ids, set(), "resampled ids must not overlap primary")
        self.assertNotEqual(primary["order_items"], resampled["order_items"])

    def test_stress_skew_duplicates_coverage(self):
        rows = self.rows[P.STRESS]
        # exact-duplicate order header rows exist
        as_tuples = [tuple(sorted(o.items())) for o in rows["orders"]]
        self.assertGreater(len(as_tuples) - len(set(as_tuples)), 0)
        # skew: one customer holds a large share of all orders
        counts = {}
        for o in rows["orders"]:
            if o["customer_id"] is not None:
                counts[o["customer_id"]] = counts.get(o["customer_id"], 0) + 1
        whale_share = max(counts.values()) / len(rows["orders"])
        self.assertGreater(whale_share, 0.2)
        # every customer still has a completed order (INNER JOIN indistinguishable)
        by_customer = self._status_by_customer(P.STRESS)
        for c in rows["customers"]:
            self.assertIn("completed", by_customer.get(c["customer_id"], set()))
        # every order has at least one item
        order_ids = {o["order_id"] for o in rows["orders"]}
        item_order_ids = {i["order_id"] for i in rows["order_items"]}
        self.assertEqual(order_ids, order_ids & item_order_ids)
        # plan-vs-sentence truth: the demo's stress prose says the duplicate
        # order header rows are deduplicated, and the plan really carries a
        # DEDUPE op over orders (populations.stress_duplicate_conditions
        # generates this agreement for adapter-built tasks).
        stress = self.task.population(P.STRESS)
        self.assertTrue(any("must dedupe" in c and "order header" in c for c in stress.conditions))
        deduped = {
            t for op in self.task.marts[0].plan.ops if op.kind is MartOpKind.DEDUPE for t in op.tables
        }
        self.assertIn("orders", deduped)
        self.assertNotIn("order_items", deduped)
        self.assertTrue(any("line items are not deduplicated" in c for c in stress.conditions))

    def test_coverage_pairs_are_parent_first(self):
        """Development at orders=2 hands EACH customer exactly one order (the
        old parent-major schedule gave customer 0 both statuses and customer
        1 nothing); at orders=4 every (customer, status) pair occurs."""
        def dev_rows(orders: int):
            specs = tuple(
                p.model_copy(update={"scale": {"customers": 2, "orders": orders, "order_items": 8}})
                if p.name is P.DEVELOPMENT else p
                for p in self.task.populations
            )
            return source_data.generate_rows(
                self.task.model_copy(update={"populations": specs}), P.DEVELOPMENT
            )

        two = dev_rows(2)
        per_customer = {}
        for o in two["orders"]:
            per_customer.setdefault(o["customer_id"], []).append(o["status"])
        self.assertEqual({len(v) for v in per_customer.values()}, {1})
        self.assertEqual(set(per_customer), {c["customer_id"] for c in two["customers"]})
        four = dev_rows(4)
        pairs = {(o["customer_id"], o["status"]) for o in four["orders"]}
        self.assertEqual(len(pairs), 4)

    def test_referential_integrity_survives_the_count_divergence(self):
        """Jittering parents and children independently creates no dangling key.

        Foreign keys are never resolved against a COUNT: `_generate_table`
        draws each key from the pool of rows that were ACTUALLY generated for
        the parent table, so a different realized count changes how many keys
        are drawn, never whether they exist. Asserted on EVERY population,
        because the divergence moves parents and children by different amounts
        and in different directions.
        """
        for pop in P:
            rows = self.rows[pop]
            customer_ids = {c["customer_id"] for c in rows["customers"]}
            order_ids = {o["order_id"] for o in rows["orders"]}
            dangling_orders = [
                o
                for o in rows["orders"]
                if o["customer_id"] is not None
                and o["customer_id"] not in customer_ids
            ]
            dangling_items = [
                i for i in rows["order_items"] if i["order_id"] not in order_ids
            ]
            self.assertEqual(dangling_orders, [], f"{pop.value}: orders.customer_id")
            self.assertEqual(dangling_items, [], f"{pop.value}: order_items.order_id")

    def test_generation_is_deterministic(self):
        again = source_data.generate_rows(self.task, P.PRIMARY)
        self.assertEqual(self.rows[P.PRIMARY], again)

    def test_required_link_with_empty_parent_pool_fails_closed(self):
        task = self.task
        pops = []
        for pop in task.populations:
            if pop.name is P.DEVELOPMENT:
                pop = pop.model_copy(
                    update={"scale": {"customers": 2, "orders": 0, "order_items": 5}}
                )
            pops.append(pop)
        broken = task.model_copy(update={"populations": tuple(pops)})
        with self.assertRaises(ValueError):
            source_data.generate_rows(broken, P.DEVELOPMENT)

    def test_undeclared_population_fails_closed(self):
        task = self.task.model_copy(
            update={"populations": tuple(p for p in self.task.populations
                                         if p.name is not P.STRESS)}
        )
        with self.assertRaises(KeyError):
            source_data.generate_rows(task, P.STRESS)


# ---------------------------------------------------------------------------
# populations.py — duplicate-witness byte-identity (the 042316 audit fix)
# ---------------------------------------------------------------------------

class TestDuplicateWitnessIntegrity(unittest.TestCase):
    """Row C's promise is BYTE-IDENTICAL duplicates, key columns included.

    The audit found a task (042316) whose 'duplicate' fact rows differed in
    `key` (900 vs None): foreign-key closure's optional-link NULL landed on
    the LAST fact row, which was one of the duplicate twins, so DISTINCT and
    no-dedupe agreed and the witness was inert. These tests pin the fix (the
    NULL moves to a non-witness row; the pair survives byte-identical) and
    the fail-closed contract (a diverged or collapsed pair RAISES a typed
    InertDuplicateWitnessError instead of freezing an inert construction).
    """

    #: The 042316 shape: parent star with a dedupe fact that ALSO carries an
    #: optional nullable FK (`key`) to a second dimension — the link whose
    #: closure used to null one duplicate twin.
    TABLES = (
        TableSpec(
            name="p",
            columns=(
                ColumnSpec(name="p_id", type=ColumnType.BIGINT),
                ColumnSpec(name="p_name", type=ColumnType.TEXT),
            ),
            primary_key=("p_id",),
        ),
        TableSpec(
            name="d",
            columns=(
                ColumnSpec(name="d_id", type=ColumnType.BIGINT),
                ColumnSpec(name="d_name", type=ColumnType.TEXT),
            ),
            primary_key=("d_id",),
        ),
        TableSpec(
            name="f",  # no primary key: exactly when fact_dedupe is declared
            columns=(
                ColumnSpec(name="f_val", type=ColumnType.INTEGER),
                ColumnSpec(name="p_id", type=ColumnType.BIGINT),
                ColumnSpec(name="key", type=ColumnType.BIGINT, nullable=True),
            ),
        ),
    )
    RELATIONSHIPS = (
        Relationship(child_table="f", child_columns=("p_id",), parent_table="p",
                     parent_columns=("p_id",), required=False),
        Relationship(child_table="f", child_columns=("key",), parent_table="d",
                     parent_columns=("d_id",), required=False),
    )
    SHAPE = mart_plan.StarShape(
        mart="m", parent="p", parent_keys=("p_id",), key_columns=("p_id",),
        fact="f", fact_link_columns=("p_id",), fact_dedupe=True, has_join=True,
    )

    def test_duplicate_pair_is_byte_identical_including_key_columns(self):
        rows = populations.counterfactual_literal_rows(
            self.TABLES, self.RELATIONSHIPS, self.SHAPE
        )
        f_rows = rows["f"]
        # Policy v6 adds one optional-owner orphan after the two matched row-A
        # facts and the duplicate row-C pair.
        self.assertEqual(len(f_rows), 5)
        dup_a, dup_b = f_rows[2], f_rows[3]
        self.assertEqual(dup_a, dup_b)  # byte-identical, `key` INCLUDED
        self.assertIsNotNone(dup_a["key"])
        # Row C still fans out under the CHILDLESS-row-B construction: both
        # twins point at parent row 3, and row B (parent row 2) has no facts.
        parent_keys = [r["p_id"] for r in rows["p"]]
        self.assertEqual(dup_a["p_id"], parent_keys[2])
        self.assertNotIn(parent_keys[1], [r["p_id"] for r in f_rows])

    def test_optional_link_null_moved_to_a_non_witness_row(self):
        """The optional-link NULL condition is still literally present — on a
        matched (row A) fact row, never on a duplicate twin."""
        rows = populations.counterfactual_literal_rows(
            self.TABLES, self.RELATIONSHIPS, self.SHAPE
        )
        f_rows = rows["f"]
        self.assertTrue(any(r["key"] is None for r in f_rows[:2]))
        self.assertTrue(all(r["key"] is not None for r in f_rows[2:]))

    def test_construction_is_deterministic(self):
        first = populations.counterfactual_literal_rows(
            self.TABLES, self.RELATIONSHIPS, self.SHAPE
        )
        second = populations.counterfactual_literal_rows(
            self.TABLES, self.RELATIONSHIPS, self.SHAPE
        )
        self.assertEqual(first, second)

    def test_key_differing_duplicate_pair_raises_inert(self):
        """Item 2 proof-by-construction: the 042316 pair (key 900 vs None)
        RAISES as inert instead of shipping."""
        twin_a = {"f_val": 902, "p_id": 902, "key": 900}
        twin_b = {"f_val": 902, "p_id": 902, "key": None}
        with self.assertRaises(populations.InertDuplicateWitnessError) as ctx:
            populations._verify_duplicate_witness(
                {"f": [twin_a, twin_b]}, "f", [twin_a, twin_b]
            )
        message = str(ctx.exception)
        self.assertIn("not byte-identical", message)
        self.assertIn("key", message)

    def test_collapsed_duplicate_pair_raises_inert(self):
        """A twin dropped by a post-construction pass (PK enforcement) is the
        same inertness and raises the same typed error."""
        twin = {"f_val": 902, "p_id": 902, "key": 900}
        with self.assertRaises(populations.InertDuplicateWitnessError) as ctx:
            populations._verify_duplicate_witness(
                {"f": [twin]}, "f", [twin, dict(twin)]
            )
        self.assertIn("lost", str(ctx.exception))


# ---------------------------------------------------------------------------
# source_data.py — artifacts + renderers
# ---------------------------------------------------------------------------

class TestArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = demo_task()
        cls.rows = source_data.generate_rows(cls.task, P.DEVELOPMENT)

    def test_write_rows_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            h1 = source_data.write_rows(self.rows, Path(tmp) / "a")
            h2 = source_data.write_rows(self.rows, Path(tmp) / "b")
        self.assertEqual(h1, h2)
        self.assertEqual(set(h1), {"customers", "orders", "order_items"})

    def test_render_population_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_a = Path(tmp) / "a"
            out_b = Path(tmp) / "b"
            paths = source_data.render_population(self.task, P.DEVELOPMENT, self.rows, out_a)
            source_data.render_population(self.task, P.DEVELOPMENT, self.rows, out_b)
            self.assertEqual(_dir_hashes(out_a), _dir_hashes(out_b))
            # backend routing per BackendAssignment
            self.assertEqual(paths["customers"].suffix, ".sql")
            self.assertIn("postgres", str(paths["customers"]))
            self.assertEqual(paths["orders"].suffix, ".jsonl")
            self.assertIn("mongodb", str(paths["orders"]))
            self.assertEqual(paths["order_items"].suffix, ".csv")
            self.assertIn("files", str(paths["order_items"]))

    def test_postgres_sql_round_trips_through_duckdb(self):
        customers = self.task.table("customers")
        rows = self.rows["customers"] + [
            {"customer_id": 999, "customer_name": "O'Brien; DROP"}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = source_data.render_postgres(customers, rows, Path(tmp))
            sql = path.read_text(encoding="utf-8")
            con = duckdb.connect(":memory:")
            con.execute(sql)
            count = con.execute('SELECT COUNT(*) FROM "customers"').fetchone()[0]
            name = con.execute(
                'SELECT customer_name FROM "customers" WHERE customer_id = 999'
            ).fetchone()[0]
        self.assertEqual(count, len(rows))
        self.assertEqual(name, "O'Brien; DROP")

    def test_rest_pagination_boundary_exact(self):
        orders = self.task.table("orders")
        rows_200 = [
            {"order_id": i, "customer_id": 1, "status": "completed"} for i in range(200)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tdir = source_data.render_rest(orders, rows_200, Path(tmp) / "exact",
                                           page_size=100)
            pages = sorted(p.name for p in tdir.glob("page_*.json"))
            self.assertEqual(pages, ["page_0001.json", "page_0002.json"])

            tdir = source_data.render_rest(orders, rows_200 + rows_200[:1],
                                           Path(tmp) / "plus1", page_size=100)
            pages = sorted(p.name for p in tdir.glob("page_*.json"))
            self.assertEqual(len(pages), 3)

            tdir = source_data.render_rest(orders, [], Path(tmp) / "empty",
                                           page_size=100)
            self.assertEqual(list(tdir.glob("page_*.json")), [])
            self.assertTrue((tdir / "index.json").exists())

    def test_files_csv_shape(self):
        items = self.task.table("order_items")
        rows = [{"order_id": 1, "quantity": None, "unit_price": 2.5}]
        with tempfile.TemporaryDirectory() as tmp:
            path = source_data.render_files(items, rows, Path(tmp))
            text = path.read_text(encoding="utf-8")
        self.assertEqual(text, "order_id,quantity,unit_price\n1,,2.5\n")

    def test_s3_and_mongodb_jsonl(self):
        orders = self.task.table("orders")
        rows = self.rows["orders"]
        with tempfile.TemporaryDirectory() as tmp:
            s3dir = source_data.render_s3(orders, rows, Path(tmp) / "s3")
            parts = sorted(s3dir.glob("part-*.jsonl"))
            self.assertEqual(len(parts), 1)
            lines = parts[0].read_text().splitlines()
            self.assertEqual(len(lines), len(rows))
            mpath = source_data.render_mongodb(orders, rows, Path(tmp) / "mongo")
            self.assertEqual(len(mpath.read_text().splitlines()), len(rows))


# ---------------------------------------------------------------------------
# semantic cross-check: the generated populations must reproduce the demo
# attack matrix when executed for real
# ---------------------------------------------------------------------------

class TestGeneratedDataSeparatesAttacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = demo_task()
        cls.rows = {
            pop: source_data.generate_rows(cls.task, pop) for pop in PopulationName
        }

    def _run(self, pop, sql):
        con = _load_duckdb(self.task, self.rows[pop])
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_counterfactual_reference_matches_spec_expectation(self):
        got = self._run(P.COUNTERFACTUAL, REFERENCE_SQL)
        expected = [
            (r["customer_id"], r["completed_order_count"], r["total_spend"])
            for r in COUNTERFACTUAL_EXPECTED_MART
        ]
        self.assertTrue(_rows_close(got, expected), f"got {got}")

    def test_sql_attack_matrix_reproduced_on_generated_data(self):
        reference = {pop: self._run(pop, REFERENCE_SQL) for pop in PopulationName}
        for case in self.task.attack_cases:
            if case.mutation.startswith("directive:"):
                continue  # compiled by verification/attacks.py, not SQL
            for pop, should_pass in case.expected_pass.items():
                mutant = self._run(pop, case.mutation)
                same = _rows_close(reference[pop], mutant)
                self.assertEqual(
                    same,
                    should_pass,
                    f"attack {case.name} on {pop.value}: expected "
                    f"{'full reward' if should_pass else 'reward loss'}, "
                    f"but mutant output {'matched' if same else 'diverged'}",
                )

    def test_count_without_distinct_returns_three_for_c11(self):
        rows = self._run(P.COUNTERFACTUAL, _attack_sql(self.task, "count_without_distinct"))
        by_id = {r[0]: r for r in rows}
        self.assertEqual(by_id[11][1], 3)


# ---------------------------------------------------------------------------
# mart_plan.py
# ---------------------------------------------------------------------------

class TestMartPlan(unittest.TestCase):
    def setUp(self):
        self.task = demo_task()
        self.plan = demo_mart_plan()

    def test_demo_plan_is_task_coherent_but_not_compiler_grade(self):
        """ONE definition of a valid plan, and the demo does not meet it.

        The demo fixture's plan is PROSE: its aggregate op carries
        ``{"function", "expression"}`` instead of ``group_by`` + measures, its
        joins carry no select list and its derive names no table. It is
        task-COHERENT — every table is declared, every column resolves, every
        mart column is produced, every join is FK-backed — which is why it
        validated clean for six rounds while `compile_plan_sql` refused it. The
        demo ships a hand-written REFERENCE_SQL, so the pipeline never compiles
        the plan and nothing noticed.

        `validate_plan` now consumes the compiler's own dry run, so the two
        readers report the SAME acceptance set. This test pins that agreement
        rather than the old (false) claim that the demo plan is compiler-grade.
        Making it compiler-grade is a fixture change that MOVES the pinned demo
        content hash (tests/test_models_round3.py, tests/test_models_pools.py)
        and is deliberately not done here. The full biconditional over every
        plan in the repo lives in tests/test_plan_contract.py.
        """
        problems = mart_plan.validate_plan(self.task, self.plan)
        self.assertNotEqual(problems, [])
        # The disagreement is structural-only: not one task-coherence problem.
        for marker in ("unknown tables", "not backed by", "not produced by any op"):
            self.assertFalse([p for p in problems if marker in p], problems)
        with self.assertRaises(ref_solution.PlanCompilationError):
            ref_solution.compile_plan_sql(self.task, self.task.marts[0])

    def test_unknown_mart_flagged(self):
        plan = self.plan.model_copy(update={"mart": "nope"})
        problems = mart_plan.validate_plan(self.task, plan)
        self.assertEqual(len(problems), 1)
        self.assertIn("not a mart", problems[0])

    def test_unknown_table_and_column_flagged(self):
        bad_ops = self.plan.ops + (
            MartOp(kind=MartOpKind.FILTER, description="bogus",
                   tables=("no_such_table",), columns=("ghost_col",)),
        )
        problems = mart_plan.validate_plan(
            self.task, self.plan.model_copy(update={"ops": bad_ops})
        )
        self.assertTrue(any("unknown tables" in p for p in problems))

    def test_join_without_relationship_flagged(self):
        plan = MartPlan(
            mart=MART_NAME,
            ops=(
                MartOp(
                    kind=MartOpKind.JOIN,
                    description="join customers to items directly",
                    tables=("customers", "order_items"),
                    columns=("customer_id",),
                    join_type=self.plan.ops[3].join_type,
                ),
                MartOp(
                    kind=MartOpKind.AGGREGATE,
                    description="produce mart columns",
                    tables=("customers",),
                    columns=("customer_id", "completed_order_count", "total_spend"),
                ),
            ),
        )
        problems = mart_plan.validate_plan(self.task, plan)
        self.assertTrue(any("not backed by a declared relationship" in p for p in problems))

    def test_unproduced_mart_column_flagged(self):
        ops = tuple(
            op.model_copy(
                update={"columns": tuple(c for c in op.columns if c != "total_spend")}
            )
            for op in self.plan.ops
        )
        problems = mart_plan.validate_plan(
            self.task, self.plan.model_copy(update={"ops": ops})
        )
        self.assertTrue(any("not produced by any op" in p and "total_spend" in p
                            for p in problems))

    def test_plan_summary_is_deterministic_prose(self):
        text = mart_plan.plan_summary(self.plan)
        self.assertEqual(text, mart_plan.plan_summary(self.plan))
        self.assertIn(MART_NAME, text)
        for idx in range(1, len(self.plan.ops) + 1):
            self.assertIn(f"{idx}. [", text)
        self.assertIn("join type: left", text)
        self.assertIn("Notes:", text)

    def test_attack_surface_demo(self):
        surface = mart_plan.attack_surface(self.plan)
        self.assertEqual(surface[AttackKind.INNER_JOIN], [3, 4])
        self.assertEqual(surface[AttackKind.DROPPED_FILTER], [0])
        self.assertEqual(surface[AttackKind.NO_NULL_DEFAULT], [6])
        self.assertEqual(surface[AttackKind.NO_DEDUP], [1, 5])
        self.assertEqual(surface[AttackKind.WRONG_GRAIN], [2, 5])
        self.assertNotIn(AttackKind.WRONG_WINDOW, surface)
        self.assertNotIn(AttackKind.WRONG_DENOMINATOR, surface)
        every = list(range(len(self.plan.ops)))
        for kind in (AttackKind.CONSTANTS, AttackKind.KEYS_ONLY,
                     AttackKind.NO_OP, AttackKind.SKIP_EXTRACTION):
            self.assertEqual(surface[kind], every)


class GuardLiteralsReachMaterializedRows(unittest.TestCase):
    """END-TO-END PRODUCIBILITY over the shipped drives, not declarations.

    `populations._dead_predicate_problems` is a declaration-level floor: a
    guard literal must sit in some enum domain or literal row. Between that
    check and the bytes a solver sees stand the FK closers (which repair child
    keys inside the constructors) and materialization. This asserts the whole
    chain for every task under runs/ that actually has materialized population
    artifacts: every guard literal is PRESENT in the rows of every graded
    population. Intake-only draft workspaces are intentionally out of scope;
    their absence of rows is checked by lifecycle gates when generation is
    requested, not by this materialized-data invariant.

    Measured at introduction: 72/72 literals present in all five
    populations of the dbt drive; the other four pools ship no guarded
    measures.
    """

    def test_every_guard_literal_is_present_in_every_population(self) -> None:
        import json as _json

        from elt_taskgen.generation.populations import (
            AGGREGATE_FAMILY_KINDS,
            guard_literals,
        )
        from elt_taskgen.models import task_from_json

        repo = Path(__file__).resolve().parents[1]
        irs = sorted(repo.glob("runs/*/tasks/*/task_ir.json"))
        if not irs:
            self.skipTest("no built drives under runs/")
        checked = 0
        for ir in irs:
            task = task_from_json(ir.read_text(encoding="utf-8"))
            lits = {
                lit
                for mart in task.marts
                for op in mart.plan.ops
                if op.kind in AGGREGATE_FAMILY_KINDS
                for _alias, _col, lit in guard_literals(op)
            }
            if not lits:
                continue
            pops = sorted((ir.parent / "populations").glob("*/rows"))
            if not pops:
                continue
            for rows_dir in pops:
                vals: set[str] = set()
                for f in rows_dir.glob("*.jsonl"):
                    for line in f.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            vals.update(
                                v
                                for v in _json.loads(line).values()
                                if isinstance(v, str)
                            )
                missing = lits - vals
                with self.subTest(task=task.task_id, population=rows_dir.parent.name):
                    self.assertFalse(
                        missing,
                        f"guard literals {sorted(missing)} appear in NO materialized "
                        f"row of {rows_dir.parent.name} — the guarded measures are "
                        "constants there",
                    )
                checked += 1
        if checked == 0:
            self.skipTest("no drive on disk carries a guarded measure")


if __name__ == "__main__":
    unittest.main()

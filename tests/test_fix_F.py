"""Group F fixes — generation/source_data.py + generation/populations.py.

Findings covered here (see the fix brief): G2 Part B (relationship parent
columns minted as identity + GENERATION_POLICY_VERSION), G3 (FK-only identity
uniqueness + capacity), G5 (stress duplicate sentence generated from plan
facts), G6 (FILES render refuses '' text), G8-A (one dangling-lever reader),
I2 (bare NUMERIC), E2 (materialize_population / population_drift),
N-populations_battery-1 (union merge + CounterfactualMergeError), -2 (legacy
inner_join / no_null_default claims only the counterfactual), -3 (identity
links mint parents instead of collapsing the anchor), -4 (column-specific
witness closure skip), -5 (parent-first coverage schedule + coverage budget),
-6 (per-population policy conditions), V3-(3) (header_as_row required only
where a text-only CSV exists).
"""

from __future__ import annotations

import re
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

import duckdb

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartOpKind,
    PopulationName,
    PopulationSpec,
    Relationship,
    TableSpec,
    TaskIR,
    derive_seed,
)

P = PopulationName


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _add_table(
    task: TaskIR,
    table: TableSpec,
    rels: tuple[Relationship, ...],
    scale: dict[str, int],
    backend: Backend = Backend.MONGODB,
) -> TaskIR:
    """`task` plus one table + relationships, scaled per population name."""
    specs = []
    for spec in task.populations:
        if spec.name.value in scale:
            specs.append(
                spec.model_copy(update={"scale": {**spec.scale, table.name: scale[spec.name.value]}})
            )
        elif spec.name is P.COUNTERFACTUAL:
            specs.append(spec)  # literal rows only; the new table generates nothing
        else:
            specs.append(spec)
    return task.model_copy(
        update={
            "tables": task.tables + (table,),
            "relationships": task.relationships + rels,
            "backends": task.backends + (BackendAssignment(table=table.name, backend=backend),),
            "populations": tuple(specs),
        }
    )


def _pk_tuples(rows, key):
    return [tuple(r.get(c) for c in key) for r in rows]


# ---------------------------------------------------------------------------
# G2 Part B — relationship parent columns are minted identities
# ---------------------------------------------------------------------------

class RelationshipParentIdentity(unittest.TestCase):
    """A declared link never fans out on the parent side."""

    def _task(self) -> TaskIR:
        atoms = TableSpec(
            name="atoms",
            columns=(
                ColumnSpec(name="uuid", type=ColumnType.BIGINT),          # non-PK parent col
                ColumnSpec(name="region_id", type=ColumnType.INTEGER, nullable=True),
                ColumnSpec(name="kind", type=ColumnType.TEXT, enum_values=("a", "b")),
                ColumnSpec(name="flag", type=ColumnType.BOOLEAN),
                ColumnSpec(name="height", type=ColumnType.INTEGER),
            ),
            primary_key=(),
        )
        vals = TableSpec(
            name="valuations",
            columns=(
                ColumnSpec(name="atom", type=ColumnType.BIGINT),
                ColumnSpec(name="region", type=ColumnType.INTEGER, nullable=True),
                ColumnSpec(name="kind_ref", type=ColumnType.TEXT, enum_values=("a", "b")),
                ColumnSpec(name="flag_ref", type=ColumnType.BOOLEAN),
                ColumnSpec(name="v", type=ColumnType.INTEGER),
            ),
        )
        rels = (
            Relationship(child_table="valuations", child_columns=("atom",),
                         parent_table="atoms", parent_columns=("uuid",), required=True),
            Relationship(child_table="valuations", child_columns=("region",),
                         parent_table="atoms", parent_columns=("region_id",), required=False),
            Relationship(child_table="valuations", child_columns=("kind_ref",),
                         parent_table="atoms", parent_columns=("kind",), required=False),
            Relationship(child_table="valuations", child_columns=("flag_ref",),
                         parent_table="atoms", parent_columns=("flag",), required=False),
        )
        base = demo_task()
        pops_ = tuple(
            spec.model_copy(
                update={
                    "scale": {"atoms": n, "valuations": n} if spec.name is not P.COUNTERFACTUAL else {},
                    "literal_rows": {},
                    "conditions": ("x",),
                }
            )
            for spec, n in zip(base.populations, (2, 60, 60, 0, 600), strict=True)
        )
        return base.model_copy(
            update={
                "tables": (atoms, vals),
                "relationships": rels,
                "backends": (
                    BackendAssignment(table="atoms", backend=Backend.MONGODB),
                    BackendAssignment(table="valuations", backend=Backend.MONGODB),
                ),
                "populations": pops_,
                "attack_cases": (),
            }
        )

    def test_identity_columns_include_non_enum_parent_columns(self):
        task = self._task()
        atoms = task.table("atoms")
        ident = source_data.identity_columns(task, atoms, ())
        self.assertEqual(ident, frozenset({"uuid", "region_id"}))
        # PK ∪ BK ∪ P − FK
        self.assertEqual(
            source_data.identity_columns(task, task.table("valuations"), ("atom", "region")),
            frozenset(),
        )

    def test_relationship_parent_columns_are_unique_in_every_population(self):
        task = self._task()
        for pop in (P.DEVELOPMENT, P.PRIMARY, P.RESAMPLED, P.STRESS):
            rows = source_data.generate_rows(task, pop)["atoms"]
            self.assertTrue(rows, pop.value)
            # atoms has no PK, so STRESS injects verbatim duplicate rows: the
            # identity claim is over DISTINCT rows (the sanctioned repetition).
            distinct = [dict(d) for d in {tuple(sorted(r.items())) for r in rows}]
            for col in ("uuid", "region_id"):
                values = [r[col] for r in distinct]
                self.assertNotIn(None, values, f"{pop.value}: {col} minted, never NULL")
                self.assertEqual(len(set(values)), len(distinct), f"{pop.value}: {col} unique")
            if pop is P.STRESS:
                # not the {1,2,3} palette any more
                self.assertFalse({r["uuid"] for r in rows} <= {1, 2, 3})
            # enum-valued parent column keeps its domain (not minted); BOOLEAN not minted
            self.assertTrue({r["kind"] for r in rows} <= {"a", "b"})
            self.assertTrue(all(isinstance(r["flag"], bool) for r in rows))
            # a non-key measure column still draws from _synth_value
            if pop is P.STRESS:
                self.assertTrue({r["height"] for r in rows} <= {1, 2, 3})

    def test_relationship_parent_problems_are_advisory(self):
        task = self._task()
        notes = pops.relationship_parent_problems(task)
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("kind" in n and "enum" in n for n in notes))
        self.assertTrue(any("flag" in n and "boolean" in n for n in notes))
        # advisory: not a coverage failure
        self.assertFalse([p for p in pops.validate_population_coverage(task) if "fans out" in p])

    def test_demo_parent_identity_set_remains_stable_under_policy_three(self):
        """Adversarial values change bytes, never which columns are identities."""
        task = demo_task()
        for tspec in task.tables:
            fk = {c for r in task.relationships if r.child_table == tspec.name for c in r.child_columns}
            self.assertEqual(
                source_data.identity_columns(task, tspec, fk),
                (set(tspec.primary_key) | set(tspec.business_key)) - fk,
            )


# ---------------------------------------------------------------------------
# G3 — FK-only identities are unique; capacity fails closed
# ---------------------------------------------------------------------------

def _bridge_task(*, business_key: bool = False, scale: dict[str, int] | None = None) -> TaskIR:
    """demo_task() + order_customer_link keyed (order_id, customer_id) of two FKs."""
    key = ("order_id", "customer_id")
    link = TableSpec(
        name="order_customer_link",
        columns=(
            ColumnSpec(name="order_id", type=ColumnType.INTEGER),
            ColumnSpec(name="customer_id", type=ColumnType.INTEGER),
            ColumnSpec(name="note", type=ColumnType.TEXT, nullable=True),
        ),
        primary_key=() if business_key else key,
        business_key=key if business_key else (),
    )
    rels = (
        Relationship(child_table="order_customer_link", child_columns=("order_id",),
                     parent_table="orders", parent_columns=("order_id",), required=True),
        Relationship(child_table="order_customer_link", child_columns=("customer_id",),
                     parent_table="customers", parent_columns=("customer_id",), required=True),
    )
    task = demo_task()
    scale = scale or {
        p.name.value: p.scale["orders"] for p in task.populations if p.scale
    }
    return _add_table(task, link, rels, scale)


class ForeignKeyIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = demo_task()
        cls.demo_rows = {pop: source_data.generate_rows(cls.demo, pop) for pop in P}

    def test_bridge_pk_of_fk_columns_unique_all_populations(self):
        task = _bridge_task()
        for pop in P:
            rows = source_data.generate_rows(task, pop)
            link = rows["order_customer_link"]
            if pop is P.COUNTERFACTUAL:
                self.assertEqual(link, [])
                continue
            keys = _pk_tuples(link, ("order_id", "customer_id"))
            self.assertEqual(len(keys), len(set(keys)), f"{pop.value}: duplicate PK tuples")
            orders = {o["order_id"] for o in rows["orders"]}
            customers = {c["customer_id"] for c in rows["customers"]}
            self.assertTrue(all(k[0] in orders and k[1] in customers for k in keys), pop.value)
            # the pre-existing tables are BYTE-IDENTICAL to the plain demo
            for name in ("customers", "orders", "order_items"):
                self.assertEqual(rows[name], self.demo_rows[pop][name], f"{pop.value}/{name}")

    def test_business_key_of_fk_columns_unique(self):
        task = _bridge_task(business_key=True)
        for pop in (P.DEVELOPMENT, P.PRIMARY, P.RESAMPLED, P.STRESS):
            link = source_data.generate_rows(task, pop)["order_customer_link"]
            distinct = {tuple(sorted(r.items())) for r in link}
            keys = [(r["order_id"], r["customer_id"]) for r in (dict(d) for d in distinct)]
            self.assertEqual(len(keys), len(set(keys)), pop.value)

    def _one_to_one(self, dev: int, primary: int, stress: int) -> TaskIR:
        profile = TableSpec(
            name="customer_profile",
            columns=(
                ColumnSpec(name="customer_id", type=ColumnType.INTEGER),
                ColumnSpec(name="bio", type=ColumnType.TEXT),
            ),
            primary_key=("customer_id",),
        )
        rels = (
            Relationship(child_table="customer_profile", child_columns=("customer_id",),
                         parent_table="customers", parent_columns=("customer_id",), required=True),
        )
        task = demo_task()
        # customers: 2 / 1000 / 1000 / 200 (dev, primary, resampled, stress)
        return _add_table(
            task, profile, rels,
            {"development": dev, "primary": primary, "resampled": primary, "stress": stress},
        )

    def test_single_fk_pk_child_unique_and_capacity_fail_closed(self):
        ok = self._one_to_one(2, 850, 170)   # customers 1000 -> realized ~930+, stress 200 -> ~186+
        self.assertEqual(
            [p for p in pops.validate_population_coverage(ok) if "foreign keys" in p], []
        )
        for pop in (P.DEVELOPMENT, P.PRIMARY, P.STRESS):
            rows = source_data.generate_rows(ok, pop)
            ids = [r["customer_id"] for r in rows["customer_profile"]]
            self.assertEqual(len(ids), len(set(ids)), pop.value)
            self.assertTrue(set(ids) <= {c["customer_id"] for c in rows["customers"]})
        too_big = self._one_to_one(2, 1000, 240)  # primary 1000 vs 1000 customers (realized 930-1070)
        problems = pops.validate_population_coverage(too_big)
        self.assertTrue(
            any("composed entirely of foreign keys" in p and "customer_profile" in p for p in problems),
            problems,
        )
        with self.assertRaises(source_data.FkIdentityCapacityError):
            source_data.generate_rows(too_big, P.STRESS)

    def test_generate_rows_asserts_declared_pk_unique(self):
        task = demo_task()
        real = source_data._generate_table

        def broken(task_, pop, policy, tspec, n, generated):
            rows = real(task_, pop, policy, tspec, n, generated)
            if tspec.name == "customers" and len(rows) > 1:
                rows[1]["customer_id"] = rows[0]["customer_id"]
            return rows

        with unittest.mock.patch.object(source_data, "_generate_table", broken):
            with self.assertRaises(source_data.SourceKeyUniquenessError) as ctx:
                source_data.generate_rows(task, P.DEVELOPMENT)
        self.assertIn("customers", str(ctx.exception))
        # a repeated BUSINESS key among distinct rows is refused too; verbatim
        # duplicates (stress injection) are the sanctioned repetition
        orders = task.table("orders")
        rows = {"orders": [{"order_id": 1, "customer_id": 1, "status": "completed"},
                           {"order_id": 1, "customer_id": 1, "status": "completed"}]}
        source_data._assert_declared_keys_unique(task.model_copy(update={"tables": (orders,)}), rows)
        rows["orders"][1]["status"] = "cancelled"
        with self.assertRaises(source_data.SourceKeyUniquenessError):
            source_data._assert_declared_keys_unique(task.model_copy(update={"tables": (orders,)}), rows)

    def test_schema_scale_hint_caps_fk_identity_tables(self):
        dim = TableSpec(name="d", columns=(ColumnSpec(name="d_id", type=ColumnType.INTEGER),), primary_key=("d_id",))
        dim2 = TableSpec(name="e", columns=(ColumnSpec(name="e_id", type=ColumnType.INTEGER),), primary_key=("e_id",))
        bridge = TableSpec(
            name="de",
            columns=(ColumnSpec(name="d_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="e_id", type=ColumnType.INTEGER)),
            primary_key=("d_id", "e_id"),
        )
        child = TableSpec(
            name="d_ext",
            columns=(ColumnSpec(name="d_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="x", type=ColumnType.TEXT)),
            primary_key=("d_id",),
        )
        rels = (
            Relationship(child_table="de", child_columns=("d_id",), parent_table="d", parent_columns=("d_id",)),
            Relationship(child_table="de", child_columns=("e_id",), parent_table="e", parent_columns=("e_id",)),
            Relationship(child_table="d_ext", child_columns=("d_id",), parent_table="d", parent_columns=("d_id",)),
        )
        hint = pops.schema_scale_hint((dim, dim2, bridge, child), rels)
        self.assertEqual(hint["d"], pops.DIMENSION_ROWS)
        self.assertEqual(hint["de"], pops.FACT_TABLE_ROWS)          # 60*60 capacity: not capped
        self.assertEqual(hint["d_ext"], int(0.85 * pops.DIMENSION_ROWS))  # 51
        self.assertGreaterEqual(hint["d_ext"], source_data.REALIZED_DIVERGENCE_MIN_SCALE)


# ---------------------------------------------------------------------------
# G5 — stress duplicate sentence is generated from plan facts
# ---------------------------------------------------------------------------

def _captains(*, landings_pk: bool = False) -> tuple[TableSpec, ...]:
    return (
        TableSpec(
            name="captains",
            columns=(ColumnSpec(name="captain_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="captain_name", type=ColumnType.TEXT)),
            primary_key=("captain_id",),
        ),
        TableSpec(
            name="landings",
            columns=(ColumnSpec(name="landing_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="captain_id", type=ColumnType.INTEGER, nullable=True),
                     ColumnSpec(name="tonnes", type=ColumnType.DECIMAL)),
            primary_key=("landing_id",) if landings_pk else (),
        ),
    )


_CAPTAIN_RELS = (
    Relationship(child_table="landings", child_columns=("captain_id",),
                 parent_table="captains", parent_columns=("captain_id",), required=False),
)


def _star(*, dedupe: bool) -> mp.BuiltPlan:
    return mp.build_star(
        mart="captain_summary",
        parent="captains",
        parent_keys=("captain_id",),
        key_columns=("captain_id",),
        joins=(
            mp.StarJoin(
                table="landings",
                on_pairs=(("captain_id", "captain_id"),),
                carry=(("captain_id", "landings__captain_id"), ("tonnes", "landings__tonnes")),
                rel_columns=("captain_id",),
            ),
        ),
        measures=(
            mp.Measure(column="landings_count", expr='COUNT("landings__captain_id")'),
            mp.Measure(column="total_tonnes", expr='SUM("landings__tonnes")', null_default="0"),
        ),
        dedupe=("landings", ("landing_id", "captain_id", "tonnes")) if dedupe else (),
    )


class StressDuplicateSentence(unittest.TestCase):
    def test_stress_duplicate_conditions_pure_function(self):
        keyed = _captains(landings_pk=True)
        (only,) = pops.stress_duplicate_conditions(keyed, (), ("captains", "landings"))
        self.assertIn("no exact-duplicate rows are injected", only)

        lines = pops.stress_duplicate_conditions(_captains(), ("landings",), ("captains", "landings"))
        self.assertIn("landings", lines[0])
        self.assertTrue(any("counts ONCE" in ln and "landings" in ln for ln in lines))
        self.assertFalse(any("not deduplicat" in ln for ln in lines))

        lines = pops.stress_duplicate_conditions(_captains(), (), ("captains", "landings"))
        self.assertTrue(any("not deduplicat" in ln.lower() or "No mart deduplicates" in ln for ln in lines))
        self.assertTrue(any("every physical copy" in ln for ln in lines))
        for ln in lines:
            self.assertNotIn("must dedupe", ln)

    def _stress_conditions(self, built: mp.BuiltPlan) -> tuple[str, ...]:
        populations, _cases = pops.derive_populations_and_attacks(
            task_id="t__x", tables=_captains(), relationships=_CAPTAIN_RELS,
            shapes=(built.shape,), scale_hint={"captains": 60, "landings": 240},
        )
        return next(p for p in populations if p.name is P.STRESS).conditions

    def test_stress_condition_agrees_with_plan_dedupe_ops(self):
        for dedupe in (True, False):
            built = _star(dedupe=dedupe)
            deduped_ops = {t for op in built.plan.ops if op.kind is MartOpKind.DEDUPE for t in op.tables}
            conditions = self._stress_conditions(built)
            text = " ".join(conditions)
            self.assertNotIn("must dedupe", text)
            self.assertIn("landings", text)
            once = [ln for ln in conditions if "counts ONCE" in ln]
            raw = [ln for ln in conditions if "No mart deduplicates" in ln]
            if dedupe:
                self.assertEqual(deduped_ops, {"landings"})
                self.assertTrue(once and "landings" in once[0])
                self.assertFalse(raw)
            else:
                self.assertEqual(deduped_ops, set())
                self.assertFalse(once)
                self.assertTrue(raw and "landings" in raw[0])
            self.assertNotIn(pops._STRESS_DUPLICATE_NEUTRAL, conditions)

    def test_default_populations_sentence_is_neutral(self):
        stress = next(p for p in pops.default_populations("t__x", {"a": 60}) if p.name is P.STRESS)
        self.assertIn(pops._STRESS_DUPLICATE_NEUTRAL, stress.conditions)
        self.assertNotIn("must dedupe", " ".join(stress.conditions))


# ---------------------------------------------------------------------------
# G6 — FILES render refuses the empty string
# ---------------------------------------------------------------------------

class FilesRenderEmptyString(unittest.TestCase):
    def test_files_render_refuses_empty_string_text(self):
        table = TableSpec(
            name="t",
            columns=(ColumnSpec(name="id", type=ColumnType.INTEGER),
                     ColumnSpec(name="name", type=ColumnType.TEXT, nullable=True)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = source_data.render_files(table, [{"id": 1, "name": None}], Path(tmp))
            self.assertEqual(path.read_text(encoding="utf-8"), "id,name\n1,\n")
            with self.assertRaises(ValueError) as ctx:
                source_data.render_files(table, [{"id": 2, "name": ""}], Path(tmp))
        self.assertIn("t.name", str(ctx.exception))
        self.assertIn("empty string", str(ctx.exception))


# ---------------------------------------------------------------------------
# G8-A — one dangling-lever reader
# ---------------------------------------------------------------------------

class DanglingLever(unittest.TestCase):
    def test_declares_dangling_is_a_word_and_ignores_negation(self):
        yes = (
            "Some optional foreign keys are dangling by design.",
            "DANGLING LINK: x.y references p.q optionally — 3 of 10 values have no matching p row.",
            "3 further declared link(s) also dangle in the same way.",
            "dangling keys present",
            "OPTIONAL-LINK WITNESS: c.parent_id has no matching p.id row.",
        )
        no = (
            "No dangling foreign keys anywhere.",
            "Every optional link resolves; the data is free of dangling references.",
            "Not a single dangling key.",
            "No OPTIONAL-LINK WITNESS is present.",
            "The dangler column is a person's title.",   # not the word
            "Data is undangling.",                          # not a whole word
        )
        for c in yes:
            self.assertTrue(source_data.declares_dangling((c,)), c)
        for c in no:
            self.assertFalse(source_data.declares_dangling((c,)), c)
        self.assertFalse(source_data.declares_dangling(()))

    def test_dangling_lever_drives_generation(self):
        """A negated sentence generates ZERO out-of-pool keys on an optional
        NON-nullable link; the affirmative one generates some."""
        task = demo_task()
        # make orders.customer_id NOT NULL so the optional link can only dangle
        tables = []
        for t in task.tables:
            if t.name == "orders":
                cols = tuple(
                    c.model_copy(update={"nullable": False}) if c.name == "customer_id" else c
                    for c in t.columns
                )
                t = t.model_copy(update={"columns": cols})
            tables.append(t)
        task = task.model_copy(update={"tables": tuple(tables)})

        def out_of_pool(conditions):
            specs = tuple(
                p.model_copy(update={"conditions": conditions}) if p.name is P.PRIMARY else p
                for p in task.populations
            )
            rows = source_data.generate_rows(task.model_copy(update={"populations": specs}), P.PRIMARY)
            ids = {c["customer_id"] for c in rows["customers"]}
            return sum(1 for o in rows["orders"] if o["customer_id"] not in ids)

        self.assertEqual(out_of_pool(("No dangling foreign keys anywhere.",)), 0)
        self.assertGreater(out_of_pool(("Some optional links are dangling by design.",)), 0)


# ---------------------------------------------------------------------------
# I2 — DECIMAL renders as bare NUMERIC (no scale the gold loader lacks)
# ---------------------------------------------------------------------------

class PostgresDecimal(unittest.TestCase):
    def test_postgres_types_impose_no_scale_the_gold_loader_lacks(self):
        from elt_taskgen.reference import solution

        scaled = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")
        for ctype in ColumnType:
            self.assertEqual(
                bool(scaled.search(source_data._POSTGRES_TYPES[ctype])),
                bool(scaled.search(solution._DUCKDB_TYPES[ctype])),
                ctype,
            )
        self.assertEqual(source_data._POSTGRES_TYPES[ColumnType.DECIMAL], "NUMERIC")

    def test_rendered_decimal_keeps_its_digits_when_the_ddl_is_honoured(self):
        table = TableSpec(
            name="m",
            columns=(ColumnSpec(name="id", type=ColumnType.INTEGER),
                     ColumnSpec(name="amount", type=ColumnType.DECIMAL)),
            primary_key=("id",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            sql = source_data.render_postgres(
                table, [{"id": 1, "amount": 42.3744438889}], Path(tmp)
            ).read_text(encoding="utf-8")
        self.assertIn('"amount" NUMERIC', sql)
        self.assertNotIn("NUMERIC(", sql)
        con = duckdb.connect(":memory:")
        con.execute(sql)   # DuckDB reads bare NUMERIC as DECIMAL(18,3)
        value = con.execute('SELECT amount FROM "m"').fetchone()[0]
        self.assertAlmostEqual(float(value), 42.3744438889, delta=1e-3)


# ---------------------------------------------------------------------------
# E2 — materialize_population + population_drift
# ---------------------------------------------------------------------------

class Materialization(unittest.TestCase):
    def test_materialize_then_drift_is_pinned_and_names_every_divergence(self):
        task = demo_task()
        with tempfile.TemporaryDirectory() as tmp:
            pop_dir = Path(tmp) / "development"
            hashes = source_data.materialize_population(task, P.DEVELOPMENT, pop_dir)
            self.assertEqual(set(hashes), {"customers", "orders", "order_items"})
            self.assertTrue((pop_dir / "rows" / "orders.jsonl").is_file())
            self.assertTrue((pop_dir / "rendered" / "files" / "order_items.csv").is_file())
            self.assertEqual(source_data.population_drift(task, P.DEVELOPMENT, pop_dir), ())
            # tree_digest is deterministic and rooted
            again = source_data.tree_digest(pop_dir)
            self.assertIn("rows/orders.jsonl", again)
            self.assertEqual(again, source_data.tree_digest(pop_dir))
            # hand-edit a rows file, add a stale rendered file, delete a page
            (pop_dir / "rows" / "orders.jsonl").write_text("{}\n", encoding="utf-8")
            (pop_dir / "rendered" / "mongodb" / "stale.jsonl").write_text("x", encoding="utf-8")
            (pop_dir / "rendered" / "files" / "order_items.csv").unlink()
            drift = source_data.population_drift(task, P.DEVELOPMENT, pop_dir)
        self.assertIn("rows/orders.jsonl: differs", drift)
        self.assertIn("rendered/mongodb/stale.jsonl: not derived from the IR", drift)
        self.assertIn("rendered/files/order_items.csv: missing on disk", drift)
        self.assertEqual(len(drift), 3)

    def test_drift_against_a_missing_dir_lists_everything_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            drift = source_data.population_drift(demo_task(), P.DEVELOPMENT, Path(tmp) / "nope")
        self.assertTrue(drift)
        self.assertTrue(all(d.endswith("missing on disk") for d in drift))


# ---------------------------------------------------------------------------
# N-populations_battery-5 / G8-D — parent-first schedule + coverage budget
# ---------------------------------------------------------------------------

def _enum_chain(domain: tuple[str, ...] = ("a", "b", "c", "d", "e")) -> tuple[tuple[TableSpec, ...], tuple[Relationship, ...]]:
    parents = TableSpec(
        name="parents",
        columns=(ColumnSpec(name="p_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="p_name", type=ColumnType.TEXT)),
        primary_key=("p_id",),
    )
    facts = TableSpec(
        name="facts",
        columns=(ColumnSpec(name="f_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="p_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="kind", type=ColumnType.TEXT, enum_values=domain),
                 ColumnSpec(name="amount", type=ColumnType.INTEGER)),
        primary_key=("f_id",),
    )
    grand = TableSpec(
        name="grand",
        columns=(ColumnSpec(name="g_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="f_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="v", type=ColumnType.INTEGER)),
        primary_key=("g_id",),
    )
    rels = (
        Relationship(child_table="facts", child_columns=("p_id",), parent_table="parents", parent_columns=("p_id",)),
        Relationship(child_table="grand", child_columns=("f_id",), parent_table="facts", parent_columns=("f_id",)),
    )
    return (parents, facts, grand), rels


def _task_from(tables, rels, populations, task_id="t__cov") -> TaskIR:
    base = demo_task()
    return base.model_copy(
        update={
            "task_id": task_id,
            "tables": tables,
            "relationships": rels,
            "backends": tuple(BackendAssignment(table=t.name, backend=Backend.MONGODB) for t in tables),
            "populations": populations,
            "attack_cases": (),
        }
    )


class CoverageSchedule(unittest.TestCase):
    def test_coverage_schedule_is_parent_first(self):
        tables, rels = _enum_chain()
        specs = tuple(
            PopulationSpec(name=name, seed=derive_seed("t__cov", name.value),
                           scale={"parents": 2, "facts": 2, "grand": 2}, conditions=("x",))
            for name in P
        )
        task = _task_from(tables, rels, specs)
        rows = source_data.generate_rows(task, P.DEVELOPMENT)
        by_parent = {}
        for f in rows["facts"]:
            by_parent.setdefault(f["p_id"], []).append(f["kind"])
        self.assertEqual(len(by_parent), 2, "every parent has a child at 2 rows over 2 parents")
        self.assertEqual(max(len(v) for v in by_parent.values()), 1, "no parent has 2 before all have 1")
        self.assertEqual(len({f["kind"] for f in rows["facts"]}), 2, "two enum values across parents")
        # with the full budget every (parent, value) pair occurs
        full = _task_from(
            tables, rels,
            tuple(s.model_copy(update={"scale": {"parents": 2, "facts": 10, "grand": 10}}) for s in specs),
        )
        rows = source_data.generate_rows(full, P.DEVELOPMENT)
        pairs = {(f["p_id"], f["kind"]) for f in rows["facts"]}
        self.assertEqual(len(pairs), 10)

    def test_coverage_budget_raises_child_scales_and_cascades(self):
        tables, rels = _enum_chain(("a", "b", "c", "d"))
        hint = {"parents": 60, "facts": 240, "grand": 240}
        specs = {s.name: s for s in pops.default_populations("t__cov", hint, tables=tables, relationships=rels)}
        dev, stress = specs[P.DEVELOPMENT], specs[P.STRESS]
        rp = source_data.realized_row_count
        self.assertGreaterEqual(rp("t__cov", "facts", dev.scale["facts"]), rp("t__cov", "parents", dev.scale["parents"]) * 4)
        self.assertGreaterEqual(rp("t__cov", "grand", dev.scale["grand"]), rp("t__cov", "facts", dev.scale["facts"]))
        self.assertGreaterEqual(
            rp("t__cov", "facts", stress.scale["facts"]),
            pops.STRESS_WHALE_HEADROOM * rp("t__cov", "parents", stress.scale["parents"]) * 4,
        )
        self.assertGreaterEqual(
            rp("t__cov", "grand", stress.scale["grand"]),
            pops.STRESS_WHALE_HEADROOM * rp("t__cov", "facts", stress.scale["facts"]),
        )
        self.assertLessEqual(stress.scale["grand"], pops.STRESS_CAP)
        # primary/resampled untouched; no shortfall notes when the budget fits
        self.assertEqual(specs[P.PRIMARY].scale, dict(sorted(hint.items())))
        self.assertFalse([c for c in dev.conditions + stress.conditions if "SHORTFALL" in c])
        # legacy call (no tables) is the plain derivation
        plain = {s.name: s for s in pops.default_populations("t__cov", hint)}
        self.assertEqual(plain[P.DEVELOPMENT].scale, {"facts": 2, "grand": 2, "parents": 2})
        # scales_by_population reports the same numbers
        self.assertEqual(
            pops.scales_by_population("t__cov", hint, tables=tables, relationships=rels)[P.STRESS],
            stress.scale,
        )

    def test_generated_dev_and_stress_honor_conditions(self):
        tables, rels = _enum_chain(("a", "b", "c", "d"))
        hint = {"parents": 60, "facts": 240, "grand": 240}
        specs = pops.default_populations("t__cov", hint, tables=tables, relationships=rels)
        task = _task_from(tables, rels, specs)
        for pop in (P.DEVELOPMENT, P.STRESS):
            rows = source_data.generate_rows(task, pop)
            parents = {p["p_id"] for p in rows["parents"]}
            kids: dict = {}
            for f in rows["facts"]:
                kids.setdefault(f["p_id"], []).append(f["kind"])
            self.assertEqual(parents - set(kids), set(), f"{pop.value}: childless parents")
            self.assertTrue(all(set(v) == {"a", "b", "c", "d"} for v in kids.values()), pop.value)
            facts = {f["f_id"] for f in rows["facts"]}
            grand_parents = {g["f_id"] for g in rows["grand"]}
            self.assertEqual(facts - grand_parents, set(), f"{pop.value}: childless facts")
            if pop is P.STRESS:
                share = max(len(v) for v in kids.values()) / len(rows["facts"])
                self.assertGreater(share, 0.2, "whale parent")

    def test_budget_degrades_and_records_when_the_cap_binds(self):
        big = tuple(f"v{i}" for i in range(80))   # 80-value domain: 60 parents x 80 = 4800 > DEV cap
        tables, rels = _enum_chain(big)
        hint = {"parents": 60, "facts": 240, "grand": 240}
        specs = {s.name: s for s in pops.default_populations("t__cov", hint, tables=tables, relationships=rels)}
        dev = specs[P.DEVELOPMENT]
        self.assertLessEqual(dev.scale["facts"], pops.DEV_COVERAGE_CAP)
        notes = [c for c in dev.conditions if "COVERAGE SHORTFALL" in c]
        self.assertTrue(notes, dev.conditions)
        self.assertIn("facts -> parents", notes[0])
        # degraded, not broken: every parent still gets a child (parents-first)
        task = _task_from(tables, rels, tuple(specs.values()))
        rows = source_data.generate_rows(task, P.DEVELOPMENT)
        parents = {p["p_id"] for p in rows["parents"]}
        self.assertEqual(parents - {f["p_id"] for f in rows["facts"]}, set())
        # a 1:1 child cannot cover its parent past capacity: noted, not raised
        one = TableSpec(
            name="ext",
            columns=(ColumnSpec(name="p_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="x", type=ColumnType.TEXT)),
            primary_key=("p_id",),
        )
        rels2 = (Relationship(child_table="ext", child_columns=("p_id",), parent_table="parents", parent_columns=("p_id",)),)
        specs2 = {
            s.name: s
            for s in pops.default_populations(
                "t__cov", {"parents": 60, "ext": 51}, tables=(tables[0], one), relationships=rels2
            )
        }
        self.assertTrue(any("keyed by its foreign key" in c for c in specs2[P.STRESS].conditions))
        self.assertEqual(specs2[P.STRESS].scale["ext"], 510)


def _bar_bridge(kinds: tuple[str, ...] = ("draft", "bottle", "can")):
    """schemapile-style bridge sells(bar_id, beer_id) keyed by TWO foreign keys."""
    bars = TableSpec(name="bars", columns=(ColumnSpec(name="bar_id", type=ColumnType.INTEGER),
                                          ColumnSpec(name="bar_name", type=ColumnType.TEXT)),
                     primary_key=("bar_id",))
    beers = TableSpec(name="beers", columns=(ColumnSpec(name="beer_id", type=ColumnType.INTEGER),
                                            ColumnSpec(name="beer_name", type=ColumnType.TEXT)),
                      primary_key=("beer_id",))
    sells = TableSpec(
        name="sells",
        columns=(ColumnSpec(name="bar_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="beer_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="price", type=ColumnType.DECIMAL),
                 ColumnSpec(name="kind", type=ColumnType.TEXT, enum_values=kinds)),
        primary_key=("bar_id", "beer_id"),
    )
    rels = (
        Relationship(child_table="sells", child_columns=("bar_id",), parent_table="bars",
                     parent_columns=("bar_id",), required=True),
        Relationship(child_table="sells", child_columns=("beer_id",), parent_table="beers",
                     parent_columns=("beer_id",), required=True),
    )
    return (bars, beers, sells), rels


class BridgeCoverageBudget(unittest.TestCase):
    """A key spanning SEVERAL links has capacity = the product of its parent
    pools — the budget applies below that bound instead of declaring a
    shortfall the data does not have; a key made SOLELY of the first link's
    columns (1:1 child) is still noted and left alone."""

    def test_bridge_budget_uses_the_key_capacity_product(self):
        tables, rels = _bar_bridge()
        hint = pops.schema_scale_hint(tables, rels)
        specs = {s.name: s for s in pops.default_populations("t__bar", hint, tables=tables, relationships=rels)}
        stress = specs[P.STRESS]
        # capacity 600 x 600 >> budget: NO "keyed by its foreign key(s)" note,
        # and the whale tier fits (>= 2 x pool x |domain|)
        self.assertFalse([c for c in stress.conditions if "keyed by its foreign key" in c], stress.conditions)
        self.assertFalse([c for c in stress.conditions if "SHORTFALL" in c], stress.conditions)
        rp = source_data.realized_row_count
        pool = rp("t__bar", "bars", stress.scale["bars"])
        self.assertGreaterEqual(rp("t__bar", "sells", stress.scale["sells"]), pops.STRESS_WHALE_HEADROOM * pool * 3)
        # development: 2 bars x 2 beers -> key capacity 3 (0.85 x 4) binds
        # BEFORE the (parent x enum) cross product of 6 — the note names the
        # KEY capacity, not the row cap, and every bar still gets a row
        dev = specs[P.DEVELOPMENT]
        notes = [c for c in dev.conditions if "SHORTFALL" in c]
        self.assertEqual(len(notes), 1, dev.conditions)
        self.assertIn("key capacity", notes[0])
        self.assertIn("every bars row still has a sells row", notes[0])
        self.assertNotIn("keyed by its foreign key(s), so", notes[0])
        # the generated data honours what the prose says
        task = _task_from(tables, rels, tuple(specs.values()), task_id="t__bar")
        self.assertEqual(
            [p for p in pops.validate_population_coverage(task) if "foreign keys" in p or "coverage" in p], []
        )
        for pop in (P.DEVELOPMENT, P.STRESS):
            rows = source_data.generate_rows(task, pop)
            keys = _pk_tuples(rows["sells"], ("bar_id", "beer_id"))
            self.assertEqual(len(keys), len(set(keys)), pop.value)
            bars = {b["bar_id"] for b in rows["bars"]}
            self.assertEqual(bars - {k[0] for k in keys}, set(), f"{pop.value}: childless bars")
            if pop is P.STRESS:
                pairs = {(r["bar_id"], r["kind"]) for r in rows["sells"]}
                self.assertEqual(len(pairs), len(bars) * 3, "every (bar, kind) pair occurs on stress")

    def test_whale_over_the_bridge_capacity_moves_off_the_whale(self):
        """Stress routes at least half the remainder to its hottest bar, but a
        bar can hold at most |beers| distinct bridge rows: the FK-only redraw
        falls back to the FIRST link for the overflow instead of raising, so
        the hot share is capped at |other pool| / n and coverage survives."""
        tables, rels = _bar_bridge()
        hint = pops.schema_scale_hint(tables, rels)
        specs = tuple(pops.default_populations("t__bar", hint, tables=tables, relationships=rels))
        task = _task_from(tables, rels, specs, task_id="t__bar")
        stress = {s.name: s for s in specs}[P.STRESS]
        rows = source_data.generate_rows(task, P.STRESS)  # would raise without the fallback
        n = len(rows["sells"])
        beers = len(rows["beers"])
        pool = len(rows["bars"])
        # the whale would need more rows than there are beers
        self.assertGreater(0.5 * (n - pool * 3), beers, "fixture: the whale overflows the other pool")
        per_bar: dict = {}
        for r in rows["sells"]:
            per_bar[r["bar_id"]] = per_bar.get(r["bar_id"], 0) + 1
        self.assertLessEqual(max(per_bar.values()), beers)
        keys = _pk_tuples(rows["sells"], ("bar_id", "beer_id"))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(per_bar), pool, "no bar lost its coverage row to the redraw")
        self.assertEqual(stress.scale["sells"], task.population(P.STRESS).scale["sells"])

    def test_self_referencing_first_link_is_not_budgeted(self):
        spaces = TableSpec(
            name="spaces",
            columns=(ColumnSpec(name="space", type=ColumnType.INTEGER),
                     ColumnSpec(name="parent", type=ColumnType.INTEGER, nullable=True),
                     ColumnSpec(name="label", type=ColumnType.TEXT)),
            primary_key=("space",),
        )
        rels = (
            Relationship(child_table="spaces", child_columns=("parent",), parent_table="spaces",
                         parent_columns=("space",), required=False),
        )
        specs = {s.name: s for s in pops.default_populations("t__self", {"spaces": 60}, tables=(spaces,), relationships=rels)}
        self.assertEqual(specs[P.STRESS].scale, {"spaces": 600})       # plain x10, not doubled
        self.assertEqual(specs[P.DEVELOPMENT].scale, {"spaces": 2})
        self.assertFalse([c for c in specs[P.STRESS].conditions if "SHORTFALL" in c])


class LiteralBusinessKey(unittest.TestCase):
    def test_literal_business_key_repeat_among_distinct_rows_is_refused_at_validation(self):
        task = demo_task()  # orders: business_key (customer_id, status)? use its own declared BK
        table = TableSpec(
            name="slots",
            columns=(ColumnSpec(name="slot_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="code", type=ColumnType.TEXT),
                     ColumnSpec(name="note", type=ColumnType.TEXT, nullable=True)),
            business_key=("code",),
        )
        base = _add_table(task, table, (), {})
        cf = base.population(P.COUNTERFACTUAL)

        def with_rows(rows):
            spec = cf.model_copy(update={"literal_rows": {**cf.literal_rows, "slots": rows}})
            return base.model_copy(
                update={"populations": tuple(spec if p.name is P.COUNTERFACTUAL else p for p in base.populations)}
            )

        distinct = with_rows(({"slot_id": 1, "code": "x", "note": "a"}, {"slot_id": 2, "code": "x", "note": "b"}))
        problems = [p for p in pops.validate_population_coverage(distinct) if "business key" in p]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("slots literal row 1", problems[0])
        self.assertIn("('code',)", problems[0])
        # a byte-identical twin (the duplicate witness) is the sanctioned repetition
        twin = with_rows(({"slot_id": 1, "code": "x", "note": "a"}, {"slot_id": 1, "code": "x", "note": "a"}))
        self.assertEqual([p for p in pops.validate_population_coverage(twin) if "business key" in p], [])
        # NULL components are never a repeat
        nulls = with_rows(({"slot_id": 1, "code": None, "note": "a"}, {"slot_id": 2, "code": None, "note": "b"}))
        self.assertEqual([p for p in pops.validate_population_coverage(nulls) if "business key" in p], [])


# ---------------------------------------------------------------------------
# N-populations_battery-6 — per-population policy conditions
# ---------------------------------------------------------------------------

class PerPopulationPolicy(unittest.TestCase):
    def test_per_population_policy_conditions_attach_only_where_keyed(self):
        populations, _ = pops.derive_populations_and_attacks(
            task_id="t__x", tables=_captains(), relationships=_CAPTAIN_RELS,
            shapes=(_star(dedupe=False).shape,), scale_hint={"captains": 60, "landings": 240},
            policy_conditions=("G",),
            policy_conditions_by_population={P.STRESS: ("S",)},
        )
        for spec in populations:
            self.assertEqual(spec.conditions[0], "G", spec.name)
            if spec.name is P.STRESS:
                self.assertEqual(spec.conditions[1], "S")
            else:
                self.assertNotIn("S", spec.conditions)


# ---------------------------------------------------------------------------
# N-populations_battery-2 — legacy claims only the counterfactual
# ---------------------------------------------------------------------------

class LegacyClaimsPath(unittest.TestCase):
    def test_legacy_inner_join_and_no_null_default_claim_only_the_counterfactual(self):
        cases = {c.name: c for c in pops.derive_attack_cases((_star(dedupe=False).shape,))}
        self.assertEqual(cases["inner_join"].expected_pass, {P.COUNTERFACTUAL: False})
        self.assertEqual(cases["no_null_default"].expected_pass, {P.COUNTERFACTUAL: False})
        provided = {
            c.name: c for c in pops.derive_attack_cases((_star(dedupe=False).shape,), policy=pops.POLICY_PROVIDED_ROWS)
        }
        self.assertEqual(provided["inner_join"].expected_pass, {P.COUNTERFACTUAL: False})


class LegacyWitnessConditions(unittest.TestCase):
    """Legacy star evidence remains visible beside catalogue-backed shapes."""

    @staticmethod
    def _counterfactual(*shapes: mp.StarShape) -> PopulationSpec:
        populations, _attacks = pops.derive_populations_and_attacks(
            task_id="t__legacy_witness_conditions",
            tables=_captains(),
            relationships=_CAPTAIN_RELS,
            shapes=tuple(shapes),
            scale_hint={"captains": 60, "landings": 240},
        )
        return next(
            spec for spec in populations if spec.name is P.COUNTERFACTUAL
        )

    @staticmethod
    def _modern_shape() -> mp.StarShape:
        return replace(
            _star(dedupe=False).shape,
            mart="captain_top",
            witnesses=(mp.WITNESS_CHILDLESS,),
        )

    def test_mixed_modern_and_legacy_shapes_each_publish_scoped_row_b(self):
        modern = self._modern_shape()
        legacy = _star(dedupe=False).shape
        counterfactual = self._counterfactual(modern, legacy)
        row_b = pops._WITNESS_PROSE[mp.WITNESS_CHILDLESS]
        legacy_row_b = pops._LEGACY_CHILDLESS_PROSE

        self.assertEqual(
            [
                "WITNESS SCOPE [mart=captain_top; shape=star; "
                "anchor=captains; bridge=landings; child=(none)]: " + row_b,
                "WITNESS SCOPE [mart=captain_summary; shape=star; "
                "anchor=captains; bridge=landings; child=(none)]: "
                + legacy_row_b,
            ],
            [
                condition
                for condition in counterfactual.conditions
                if condition.endswith((row_b, legacy_row_b))
            ],
        )
        from elt_taskgen.review import repair_proposer

        task = TaskIR.model_construct(
            task_id="t__legacy_witness_conditions",
            populations=(counterfactual,),
        )
        self.assertEqual(
            repair_proposer._declared_witness_scopes(task),
            (
                ((mp.WITNESS_CHILDLESS,), "captains", "landings", ""),
                ((mp.WITNESS_CHILDLESS,), "captains", "landings", ""),
            ),
        )

    def test_legacy_join_witness_opt_out_suppresses_its_row_b(self):
        opted_out = replace(
            _star(dedupe=False).shape,
            mart="captain_summary_opted_out",
            join_counterfactual_witness=False,
        )
        counterfactual = self._counterfactual(opted_out)
        row_b = pops._WITNESS_PROSE[mp.WITNESS_CHILDLESS]

        self.assertFalse(
            any(condition.endswith(row_b) for condition in counterfactual.conditions)
        )
        self.assertFalse(
            any("NO child rows" in condition for condition in counterfactual.conditions),
            "the old unscoped fallback must not bypass the explicit opt-out",
        )
        self.assertTrue(
            any(
                "mart=captain_summary_opted_out;" in condition
                for condition in counterfactual.conditions
            )
        )

    def test_legacy_dedupe_keeps_its_scoped_row_c_in_a_mixed_task(self):
        modern = self._modern_shape()
        legacy = _star(dedupe=True).shape
        counterfactual = self._counterfactual(modern, legacy)
        row_c = pops._WITNESS_PROSE[mp.WITNESS_DUPLICATE]

        self.assertIn(
            "WITNESS SCOPE [mart=captain_summary; shape=star; "
            "anchor=captains; bridge=landings; child=(none)]: " + row_c,
            counterfactual.conditions,
        )
        landings = counterfactual.literal_rows["landings"]
        self.assertTrue(
            any(
                left == right
                for index, left in enumerate(landings)
                for right in landings[index + 1 :]
            ),
            "the scoped row-C claim must retain a byte-identical literal pair",
        )


# ---------------------------------------------------------------------------
# N-populations_battery-1 / -3 / -4 — counterfactual construction closure
# ---------------------------------------------------------------------------

def _soil() -> tuple[tuple[TableSpec, ...], tuple[Relationship, ...]]:
    profiles = TableSpec(
        name="soil_profiles",
        columns=(ColumnSpec(name="profile_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="profile_name", type=ColumnType.TEXT)),
        primary_key=("profile_id",),
    )
    horizons = TableSpec(
        name="horizons",
        columns=(ColumnSpec(name="horizon_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="profile_id", type=ColumnType.INTEGER, nullable=True),
                 ColumnSpec(name="depth_start", type=ColumnType.INTEGER),
                 ColumnSpec(name="horizon_name", type=ColumnType.TEXT)),
        primary_key=("horizon_id",),
    )
    components = TableSpec(
        name="components",
        columns=(ColumnSpec(name="component_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="horizon_id", type=ColumnType.INTEGER),
                 ColumnSpec(name="component_percentage", type=ColumnType.INTEGER),
                 ColumnSpec(name="component_name", type=ColumnType.TEXT)),
        primary_key=("component_id",),
    )
    rels = (
        Relationship(child_table="horizons", child_columns=("profile_id",),
                     parent_table="soil_profiles", parent_columns=("profile_id",), required=False),
        Relationship(child_table="components", child_columns=("horizon_id",),
                     parent_table="horizons", parent_columns=("horizon_id",), required=True),
    )
    return (profiles, horizons, components), rels


def _shape(mart, parent, key, fact, fk, link_key, measure, label, witnesses=None) -> mp.StarShape:
    return mp.StarShape(
        mart=mart, parent=parent, parent_keys=(key,), key_columns=("parent_key",),
        fact=fact, fact_link_columns=(fk,), has_join=True,
        witnesses=witnesses or (mp.WITNESS_CONTROL, mp.WITNESS_CHILDLESS, mp.WITNESS_TIE),
        roles=mp.FactRoles(link_key=link_key, measure=measure, label=label),
    )


def _resolves(rows, rels) -> list[str]:
    broken = []
    for rel in rels:
        parents = {tuple(r.get(c) for c in rel.parent_columns) for r in rows.get(rel.parent_table, ())}
        for r in rows.get(rel.child_table, ()):
            key = tuple(r.get(c) for c in rel.child_columns)
            if any(v is None for v in key):
                continue
            if key not in parents:
                broken.append(f"{rel.child_table}.{rel.child_columns} = {key} -> {rel.parent_table}")
    return broken


_OPTIONAL_OWNER_BREAK = (
    "horizons.('profile_id',) = (1300,) -> soil_profiles"
)


class MultiShapeCounterfactual(unittest.TestCase):
    def setUp(self):
        self.tables, self.rels = _soil()
        self.s0 = _shape("m0", "soil_profiles", "profile_id", "horizons", "profile_id",
                         "horizon_id", "depth_start", "horizon_name")
        self.s1 = _shape("m1", "horizons", "horizon_id", "components", "horizon_id",
                         "component_id", "component_percentage", "component_name")

    def test_multi_shape_chain_shifted_preserves_only_the_optional_owner_orphan(self):
        rows = pops.counterfactual_rows_for_shapes(self.tables, self.rels, (self.s0, self.s1))
        self.assertEqual(_resolves(rows, self.rels), [_OPTIONAL_OWNER_BREAK])
        self.assertEqual(
            _resolves(rows, tuple(rel for rel in self.rels if rel.required)),
            [],
        )
        owner = pops._shape_owner_relationship(self.s0, self.rels)
        self.assertIsNotNone(owner)
        optional_conditions = pops._optional_dangling_conditions(
            self.rels, rows, owner_relationships=(owner,)
        )
        task = _task_from(
            self.tables, self.rels,
            tuple(
                PopulationSpec(name=n, seed=derive_seed("t__cov", n.value),
                               scale={} if n is P.COUNTERFACTUAL else {t.name: 4 for t in self.tables},
                               conditions=(
                                   optional_conditions
                                   if n is P.COUNTERFACTUAL
                                   else ("x",)
                               ),
                               literal_rows=rows if n is P.COUNTERFACTUAL else {})
                for n in P
            ),
        )
        self.assertEqual(pops.validate_population_coverage(task), [])
        # The frozen literal retains exactly the declared optional orphan; all
        # required links remain closed after source-data materialization.
        gen = source_data.generate_rows(task, P.COUNTERFACTUAL)
        self.assertEqual(_resolves(gen, self.rels), [_OPTIONAL_OWNER_BREAK])

    def test_each_mart_publicly_owns_its_childless_witness(self):
        """Repeated row letters remain explicit on every mart/anchor edge."""

        populations, _attacks = pops.derive_populations_and_attacks(
            task_id="t__scoped_multi_shape",
            tables=self.tables,
            relationships=self.rels,
            shapes=(self.s0, self.s1),
            scale_hint={table.name: 12 for table in self.tables},
        )
        counterfactual = next(spec for spec in populations if spec.name is P.COUNTERFACTUAL)
        row_b = pops._WITNESS_PROSE[mp.WITNESS_CHILDLESS]
        expected = [
            "WITNESS SCOPE [mart=m0; shape=star; anchor=soil_profiles; "
            "bridge=horizons; child=(none)]: " + row_b,
            "WITNESS SCOPE [mart=m1; shape=star; anchor=horizons; "
            "bridge=components; child=(none)]: " + row_b,
        ]
        actual = [
            condition
            for condition in counterfactual.conditions
            if condition.endswith(row_b)
        ]
        self.assertEqual(expected, actual)
        self.assertEqual(2, len(actual), "global prose dedupe must not erase mart m1")

        from elt_taskgen.review import repair_proposer

        task = TaskIR.model_construct(
            task_id="t__scoped_multi_shape",
            tables=self.tables,
            relationships=self.rels,
            populations=populations,
        )
        self.assertEqual(
            (
                (
                    (mp.WITNESS_CONTROL, mp.WITNESS_CHILDLESS, mp.WITNESS_TIE),
                    "soil_profiles",
                    "horizons",
                    "",
                ),
                (
                    (mp.WITNESS_CONTROL, mp.WITNESS_CHILDLESS, mp.WITNESS_TIE),
                    "horizons",
                    "components",
                    "",
                ),
            ),
            repair_proposer._declared_witness_scopes(task),
        )
        self.assertEqual((), repair_proposer.witness_problem_codes(task))

    def test_multi_shape_shared_bridge_required_links_close(self):
        p1 = TableSpec(name="p1", columns=(ColumnSpec(name="p1_id", type=ColumnType.INTEGER),
                                           ColumnSpec(name="n1", type=ColumnType.TEXT)), primary_key=("p1_id",))
        p2 = TableSpec(name="p2", columns=(ColumnSpec(name="p2_id", type=ColumnType.INTEGER),
                                           ColumnSpec(name="n2", type=ColumnType.TEXT)), primary_key=("p2_id",))
        b = TableSpec(name="b", columns=(ColumnSpec(name="b_id", type=ColumnType.INTEGER),
                                         ColumnSpec(name="p1_id", type=ColumnType.INTEGER),
                                         ColumnSpec(name="p2_id", type=ColumnType.INTEGER),
                                         ColumnSpec(name="amt", type=ColumnType.INTEGER),
                                         ColumnSpec(name="lbl", type=ColumnType.TEXT)), primary_key=("b_id",))
        rels = (
            Relationship(child_table="b", child_columns=("p1_id",), parent_table="p1", parent_columns=("p1_id",)),
            Relationship(child_table="b", child_columns=("p2_id",), parent_table="p2", parent_columns=("p2_id",)),
        )
        s1 = _shape("m1", "p1", "p1_id", "b", "p1_id", "b_id", "amt", "lbl")
        s2 = _shape("m2", "p2", "p2_id", "b", "p2_id", "b_id", "amt", "lbl")
        rows = pops.counterfactual_rows_for_shapes((p1, p2, b), rels, (s1, s2))
        self.assertEqual(_resolves(rows, rels), [])
        fake = type("T", (), {"table": lambda self, n: {t.name: t for t in (p1, p2, b)}[n], "relationships": rels})()
        spec = PopulationSpec(name=P.COUNTERFACTUAL, seed=1, literal_rows=rows)
        self.assertEqual(pops._literal_row_problems(fake, spec), [])

    def test_multi_shape_witness_rows_are_byte_identical_to_standalone(self):
        shapes = (self.s0, self.s1)
        merged = pops.counterfactual_rows_for_shapes(self.tables, self.rels, shapes)
        for k, shape in enumerate(shapes):
            with pops._shape_mint_offset(k * pops._MINT_OFFSET_STRIDE):
                standalone = pops.counterfactual_literal_rows(self.tables, self.rels, shape)
            for table, rows in standalone.items():
                for row in rows:
                    self.assertIn(dict(row), [dict(r) for r in merged[table]], f"{table}: {row}")
        # duplicate twins survive: a dedupe shape over a PK-less bridge
        tables = tuple(
            t.model_copy(update={"primary_key": ()}) if t.name == "horizons" else t for t in self.tables
        )
        dup = mp.StarShape(
            mart="m0", parent="soil_profiles", parent_keys=("profile_id",), key_columns=("parent_key",),
            fact="horizons", fact_link_columns=("profile_id",), fact_dedupe=True, has_join=True,
            witnesses=(mp.WITNESS_CONTROL, mp.WITNESS_DUPLICATE),
            roles=mp.FactRoles(link_key="horizon_id", measure="depth_start", label="horizon_name"),
        )
        merged = pops.counterfactual_rows_for_shapes(tables, self.rels, (dup, self.s1))
        as_rows = [tuple(sorted(r.items())) for r in merged["horizons"]]
        self.assertGreater(len(as_rows) - len(set(as_rows)), 0, "twins survive the union")

    def test_multi_shape_pk_collision_drops_filler_not_witness(self):
        # a filler table with an enum PK of length 2: two namespaces collide
        tag = TableSpec(name="tag", columns=(ColumnSpec(name="t", type=ColumnType.TEXT, enum_values=("x", "y")),),
                        primary_key=("t",))
        tables = self.tables + (tag,)
        merged = pops.counterfactual_rows_for_shapes(tables, self.rels, (self.s0, self.s1))
        self.assertEqual(len(merged["tag"]), 1)  # the two fillers collide -> one survives
        for k, shape in enumerate((self.s0, self.s1)):
            with pops._shape_mint_offset(k * pops._MINT_OFFSET_STRIDE):
                standalone = pops.counterfactual_literal_rows(tables, self.rels, shape)
            for table in ("soil_profiles", "horizons", "components"):
                if table in pops._shape_core_tables(shape):
                    for row in standalone[table]:
                        self.assertIn(dict(row), [dict(r) for r in merged[table]])

    def test_multi_shape_merge_invariant_raises_on_new_break(self):
        """Every standalone build stays closed; the MERGE loses build 1's
        soil_profiles filler (the row build 1's horizons point at) -> the
        merged output has a break no standalone build had -> refused."""
        real = pops._enforce_primary_keys

        def lossy(by_name, rows):
            real(by_name, rows)
            keys = {r["profile_id"] for r in rows.get("soil_profiles", ())}
            if {900, 1400} <= keys:   # only the MERGED table holds both namespaces
                rows["soil_profiles"] = [r for r in rows["soil_profiles"] if r["profile_id"] < 1400]

        with unittest.mock.patch.object(pops, "_enforce_primary_keys", lossy):
            with self.assertRaises(pops.CounterfactualMergeError) as ctx:
                pops.counterfactual_rows_for_shapes(self.tables, self.rels, (self.s0, self.s1))
        self.assertIn("horizons", str(ctx.exception))

    def test_referential_integrity_gate_passes_on_multi_shape_counterfactual(self):
        from elt_taskgen.verification import gates

        gate = getattr(gates, "_gate_referential_integrity", None)
        if gate is None:
            self.skipTest("gates._gate_referential_integrity not available")
        generated, _attacks = pops.derive_populations_and_attacks(
            task_id="t__cov",
            tables=self.tables,
            relationships=self.rels,
            shapes=(self.s0, self.s1),
            scale_hint={table.name: 4 for table in self.tables},
        )
        task = _task_from(self.tables, self.rels, generated)
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            tdir = ws / "tasks" / task.task_id
            for pop in P:
                source_data.write_rows(source_data.generate_rows(task, pop), tdir / "populations" / pop.value / "rows")
            result = gate(task, ws)
        self.assertTrue(result.passed, result.details)


class IdentityLinkClosure(unittest.TestCase):
    """N-populations_battery-3: an anchor whose PK is an FK keeps its witnesses."""

    def _tables(self):
        (profiles, horizons, components), rels = _soil()
        base = TableSpec(
            name="profile_base",
            columns=(ColumnSpec(name="profile_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="note", type=ColumnType.TEXT, nullable=True)),
            primary_key=("profile_id",),
        )
        rels = rels + (
            Relationship(child_table="soil_profiles", child_columns=("profile_id",),
                         parent_table="profile_base", parent_columns=("profile_id",), required=True),
        )
        return (profiles, horizons, components, base), rels

    def test_anchor_keyed_by_its_fk_keeps_every_witness(self):
        tables, rels = self._tables()
        shape = _shape("m0", "soil_profiles", "profile_id", "horizons", "profile_id",
                       "horizon_id", "depth_start", "horizon_name")
        rows = pops.witness_literal_rows(tables, rels, shape)
        self.assertEqual([r["profile_id"] for r in rows["soil_profiles"]], [900, 901, 902])
        self.assertEqual(_resolves(rows, rels), [_OPTIONAL_OWNER_BREAK])
        self.assertEqual(
            _resolves(rows, tuple(rel for rel in rels if rel.required)), []
        )
        self.assertEqual({r["profile_id"] for r in rows["profile_base"]}, {900, 901, 902})
        # legacy path too
        legacy = mp.StarShape(mart="m", parent="soil_profiles", parent_keys=("profile_id",),
                              key_columns=("parent_key",), fact="horizons",
                              fact_link_columns=("profile_id",), has_join=True)
        rows = pops.counterfactual_literal_rows(tables, rels, legacy)
        self.assertEqual([r["profile_id"] for r in rows["soil_profiles"]], [900, 901, 902])
        self.assertEqual(_resolves(rows, rels), [_OPTIONAL_OWNER_BREAK])
        self.assertEqual(
            _resolves(rows, tuple(rel for rel in rels if rel.required)), []
        )

    def test_bridge_keyed_by_two_fks_keeps_every_witness_row(self):
        (profiles, horizons, components), rels = _soil()
        values = TableSpec(name="vals", columns=(ColumnSpec(name="v_id", type=ColumnType.INTEGER),
                                                 ColumnSpec(name="v", type=ColumnType.TEXT)), primary_key=("v_id",))
        bridge = TableSpec(
            name="horizons",
            columns=(ColumnSpec(name="profile_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="v_id", type=ColumnType.INTEGER),
                     ColumnSpec(name="depth_start", type=ColumnType.INTEGER),
                     ColumnSpec(name="horizon_name", type=ColumnType.TEXT)),
            primary_key=("profile_id", "v_id"),
        )
        rels = (
            Relationship(child_table="horizons", child_columns=("profile_id",),
                         parent_table="soil_profiles", parent_columns=("profile_id",)),
            Relationship(child_table="horizons", child_columns=("v_id",),
                         parent_table="vals", parent_columns=("v_id",)),
        )
        shape = _shape("m0", "soil_profiles", "profile_id", "horizons", "profile_id",
                       "v_id", "depth_start", "horizon_name")
        rows = pops.witness_literal_rows((profiles, bridge, values), rels, shape)
        # control (2 rows) + tie (2 rows) all survive with distinct (profile_id, v_id)
        self.assertEqual(len(rows["horizons"]), 4)
        keys = _pk_tuples(rows["horizons"], ("profile_id", "v_id"))
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(_resolves(rows, rels), [])


class SecondForeignKeyIntoAnchor(unittest.TestCase):
    """N-populations_battery-4: only the witness COLUMN is left alone."""

    def test_second_fk_into_the_anchor_is_closed(self):
        (profiles, horizons, components), rels = _soil()
        horizons = horizons.model_copy(
            update={"columns": horizons.columns + (ColumnSpec(name="secondary_profile_id", type=ColumnType.INTEGER),)}
        )
        for required in (False, True):
            rels2 = rels + (
                Relationship(child_table="horizons", child_columns=("secondary_profile_id",),
                             parent_table="soil_profiles", parent_columns=("profile_id",), required=required),
            )
            shape = _shape("m0", "soil_profiles", "profile_id", "horizons", "profile_id",
                           "horizon_id", "depth_start", "horizon_name")
            rows = pops.witness_literal_rows((profiles, horizons, components), rels2, shape)
            self.assertEqual(
                _resolves(rows, rels2),
                [_OPTIONAL_OWNER_BREAK],
                f"required={required}",
            )
            self.assertEqual(
                _resolves(rows, tuple(rel for rel in rels2 if rel.required)),
                [],
                f"required={required}",
            )
            # the witness link itself is untouched: row B (anchor 901) is childless
            self.assertNotIn(901, [r["profile_id"] for r in rows["horizons"]])
            self.assertEqual(
                [r["profile_id"] for r in rows["horizons"]],
                [900, 900, 902, 902, 1300],
            )


if __name__ == "__main__":
    unittest.main()

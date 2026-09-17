"""Metrology fixture families (review/metrology_fixtures/).

WHY THIS EXISTS
Phase 4 diversifies the council metrology pool from ONE demo-derived family
to at least three (roadmap §7, Table 8 "Pool"; metrology redesign §9). These
tests prove that

  * the package exposes exactly the API `review/metrology.py` codes against
    (`FAMILIES`, `FixtureFamily`, `InjectorAnchor`, one builder per specimen
    kind, a canary GUID per family), and that the demo family wraps the
    existing pool byte for byte;
  * every new family is a REAL frozen star schema: it passes the same
    structural gates the demo fixture passes (IR validity, population
    coverage, structural completeness, the leak scan, row generation, the
    reference SQL reproducing the counterfactual, the SQL attack matrix on
    generated data) and its declared conditions hold on the generated rows;
  * every family's pool is well formed (holdout, two scoring axes, de-leaked
    omissions, on-screen contradictions, distinct surface variants) and the
    two roadmap tests — every specimen leak-free with a distinct target view,
    and a diligent reader detecting every variant of every specimen with zero
    false alarms — hold on EVERY family;
  * families are frozen: one content digest per family is pinned.

The per-family oracles below are harness instruments defined inside the tests
only (the demo family is scored by `tests.test_council_efficacy.OracleProvider`,
imported on purpose so the demo reads exactly as it always has).
"""

from __future__ import annotations

import json
import unittest
from collections import Counter, defaultdict

import duckdb

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations, source_data
from elt_taskgen.models import (
    ColumnType,
    CouncilRole,
    PopulationName,
    TaskIR,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.review import council as council_mod
from elt_taskgen.review import metrology as metrology_mod
from elt_taskgen.review.council import CRITIC_ROLES, leak_findings, render_view
from elt_taskgen.review.metrology_fixtures import (
    ANCHOR_KINDS,
    FAMILIES,
    FAMILY_NAMES,
    FAMILY_SEPARATOR,
    KIND_ROLES,
    SPECIMEN_KINDS,
    FixtureFamily,
    InjectorAnchor,
    build_prose,
    family_of,
    resolve_anchor,
    specimen_name,
)
from elt_taskgen.review.metrology_fixtures import clinic_visits, stock_ledger
from elt_taskgen.verification.structural_completeness import check_structural_completeness

# PRIVATE test instruments of the metrology suite, imported on purpose: the
# demo family must be read by the very oracle that admits the demo pool.
from tests.test_council_efficacy import OracleProvider, _finding

P = PopulationName
NEW_FAMILY_MODULES = {"clinic_visits": clinic_visits, "stock_ledger": stock_ledger}

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


def _load_duckdb(task: TaskIR, rows):
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


def _norm_row(row):
    out = []
    for v in row:
        if isinstance(v, float):
            out.append(round(v, 4))
        elif hasattr(v, "isoformat"):
            out.append(v.isoformat())
        else:
            out.append(v)
    return tuple(out)


def _rows_close(a, b):
    """Order-insensitive row-set comparison with relative float tolerance."""
    if len(a) != len(b):
        return False
    return sorted(map(_norm_row, a)) == sorted(map(_norm_row, b))


def _run_sql(task: TaskIR, rows, sql: str):
    con = _load_duckdb(task, rows)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _new_families() -> list[tuple[str, FixtureFamily]]:
    return [(name, FAMILIES[name]) for name in FAMILY_NAMES if name != "demo"]


def _specimens_by_kind(family: FixtureFamily, kind: str):
    return {s.name: s for s in family.specimen_builders[kind]()}


def _conditions_of(task: TaskIR) -> str:
    return render_view(CouncilRole.POPULATION_ADVERSARY, task).split(
        "POPULATION CONDITIONS:", 1
    )[1].lower()


def _entry(specimen) -> dict:
    """This module's OWN digest entry (independent of `metrology._pool_entry`)."""
    return {
        "name": specimen.name,
        "kind": specimen.kind,
        "canary_kind": specimen.canary_kind,
        "target_role": specimen.target_role.value if specimen.target_role else None,
        "detection_terms": list(specimen.detection_terms),
        "anchor_terms": list(specimen.anchor_terms),
        "task": specimen.task.content_hash(),
        "prose_sha256": sha256_hex(specimen.task.solver_prompt),
        "variants": [
            {"task": v.content_hash(), "prose_sha256": sha256_hex(v.solver_prompt)}
            for v in specimen.variants
        ],
    }


def family_digest(family: FixtureFamily) -> str:
    """The content digest a family is FROZEN by: its clean task, its canary,
    every anchor and the exact task+prose of every surface variant of every
    specimen. Any edit to a family module moves it."""
    doc = {
        "name": family.name,
        "task": family.task().content_hash(),
        "canary": family.canary_guid,
        "anchors": {
            role: [[a.role, a.kind, a.target, a.note] for a in anchors]
            for role, anchors in sorted(family.anchors.items())
        },
        "specimens": [_entry(s) for s in family.specimens()],
    }
    return sha256_hex(canonical_json(doc))


# ---------------------------------------------------------------------------
# Per-family diligent readers (harness instruments; tests only)
# ---------------------------------------------------------------------------


def _absent(phrase: str):
    return lambda low, conditions: phrase not in low


def _present(phrase: str):
    return lambda low, conditions: phrase in low


def _conditions_absent(*phrases: str):
    return lambda low, conditions: not any(p in conditions for p in phrases)


def _conditions_present(phrase: str):
    return lambda low, conditions: phrase in conditions


class FamilyOracleProvider:
    """A diligent reader for ONE family: emits a targeted finding exactly when
    a planted defect is visible in the role's view (naming the element it
    objects to, as two-axis scoring demands) and raises no DEFECT CLAIM on
    clean specimens. The shortcut attacker files its diligence probe report
    when it sees no exploit, exactly as `OracleProvider` does."""

    def __init__(self, triggers, probe):
        self._triggers = triggers
        self._probe = probe

    def complete(self, role: CouncilRole, prompt: str) -> str:
        low = " ".join(prompt.lower().split())
        conditions = low.split("population conditions:", 1)[-1]
        findings: list[dict] = []
        for fires, finding in self._triggers.get(role, ()):
            result = fires(low, conditions)
            if not result:
                continue
            findings.append(finding(low) if callable(finding) else finding)
        if role is CouncilRole.SHORTCUT_ATTACKER and not findings:
            findings.append(self._probe)
        return json.dumps({"findings": findings})


def _tiny_population_findings(entity: str, dev_count: str, cf_count: str, total: str):
    """The shortcut seat's graded-surface enumeration findings."""

    def build(low: str):
        dev = "population development" in low
        cf = "population counterfactual" in low
        if dev and cf:
            return _finding(
                f"Only development and counterfactual are graded: {dev_count} "
                f"plus {cf_count}, {total} in all.",
                "The whole graded surface is trivially enumerable and can be "
                "memorized and hard-coded.",
                severity="major",
                suggested_attack="constants",
            )
        if dev:
            return _finding(
                f"Only the development population is graded: {dev_count}, "
                "trivially enumerable.",
                "The whole graded surface can be memorized and hard-coded; it is "
                "a single population.",
                severity="major",
                suggested_attack="constants",
            )
        return _finding(
            f"Only the counterfactual population is graded: {cf_count}, "
            "trivially enumerable.",
            "The whole graded surface can be memorized and hard-coded; it is a "
            "single population.",
            severity="major",
            suggested_attack="constants",
        )

    def fires(low: str, conditions: str) -> bool:
        return "population primary" not in low and (
            "population development" in low or "population counterfactual" in low
        )

    return fires, build


def _clinic_oracle() -> FamilyOracleProvider:
    amb = CouncilRole.AMBIGUITY_CRITIC
    pop = CouncilRole.POPULATION_ADVERSARY
    shc = CouncilRole.SHORTCUT_ATTACKER
    fea = CouncilRole.FEASIBILITY_REVIEWER
    triggers = {
        amb: (
            (_present("contributes its fee three times"), _finding(
                "fee_total's column description contradicts the visit-grain rule.",
                "The mart schema sums fee over every procedure row while rule 7 "
                "sums it over the DISTINCT visits in scope at the visit grain; "
                "nothing says which wins, and multi-procedure visits are declared "
                "possible.", severity="fatal")),
            (_present("from two attended visits upward"), _finding(
                "caseload_band's column description contradicts the surviving "
                "thresholds rule.",
                "The mart schema makes 'busy' start at two attended visits while "
                "rule 9 starts it at three; a practitioner with exactly two "
                "attended visits has two readings.", severity="fatal")),
            (_absent("coalesce"), _finding(
                "Ambiguous null handling: fee_total and the other measures for "
                "practitioners with no attended visits could be NULL or zero.",
                "The prose omits the COALESCE-to-zero rule for attended_visit_count, "
                "walk_in_visit_count, procedure_units and fee_total.",
                severity="major")),
            (_absent("deduplicate"), _finding(
                "Duplicate visit rows are never mentioned: attended_visit_count is "
                "ambiguous.",
                "Without a dedupe rule the same visit_id counted twice is as "
                "defensible as counting it once.", severity="major")),
            (_absent("status = 'attended'"), _finding(
                "The prose never says which visits count: the status filter is not "
                "stated.",
                "attended_visit_count could mean all visits or only non-cancelled "
                "ones; two readings, both defensible.", severity="major")),
            (_absent("grouped by visit_id"), _finding(
                "How a visit's procedure units are built from its procedure rows is "
                "not specified.",
                "procedure_units depends on summing units over procedures, and the "
                "prose never states at which grain or whether repeated rows count. "
                "Ambiguous.", severity="major")),
            (_absent("onto practitioners"), _finding(
                "Whether practitioners with no attended visits appear at all is "
                "unspecified.",
                "Nothing states that practitioners with no visits are retained, so "
                "an implementer could emit them or omit them; the "
                "practitioner_activity row set is ambiguous.", severity="major")),
        ),
        pop: (
            (_conditions_absent("no visits", "never seen a single patient",
                                "no attended visits"), _finding(
                "An INNER JOIN is indistinguishable from the required LEFT JOIN: no "
                "graded population contains a practitioner without visits.",
                "Every population (primary, resampled, counterfactual) now matches, "
                "so wrong join logic would score full reward.",
                severity="major", suggested_attack="inner_join")),
            (_conditions_absent("cancelled"), _finding(
                "No population contains a cancelled visit, so the status filter is "
                "never exercised.",
                "A solver that skips the attended-status filter is "
                "indistinguishable from a correct one here.", severity="major")),
            (_conditions_absent("duplicate visit row"), _finding(
                "No population declares duplicate visit rows any more — the stress "
                "population dropped them.",
                "The dedupe rule is never exercised, so omitting DISTINCT over "
                "visit_id is indistinguishable from correct logic.",
                severity="major")),
            (_conditions_absent("visits have null practitioner_id"), _finding(
                "No graded population states that any visit carries a NULL "
                "practitioner_id.",
                "primary, resampled, stress and counterfactual all guarantee a "
                "practitioner for every visit, so a solver that attributes unowned "
                "visits would still score full reward — the exclusion rule is "
                "never exercised.", severity="major")),
            (_conditions_absent("several procedure rows"), _finding(
                "No graded population guarantees a visit with more than one "
                "procedure row.",
                "primary, stress and counterfactual all give every visit a single "
                "procedure row, so summing fee_total at the procedure-row grain "
                "cannot be distinguished from the visit grain; both score full "
                "reward.", severity="major")),
            (_conditions_present("carries a practitioner_id, and appears once"),
             _finding(
                "Every discriminating condition sits in development, the split the "
                "solver can see.",
                "primary, resampled, stress and counterfactual are all stated to be "
                "uniform — attended, owned, unique, single-procedure — so no GRADED "
                "population can distinguish wrong logic; it would still score full "
                "reward.", severity="major")),
        ),
        shc: (
            (_present("always exactly the same value"), _finding(
                "fee_total and attended_visit_count are declared constant: "
                "hard-code them for full reward.",
                "practitioner_activity can be produced without solving.",
                severity="major", suggested_attack="constants")),
            (_present("equals the practitioner_id itself"), _finding(
                "fee_total is a function of practitioner_id alone: copy the key, "
                "skip the computation.",
                "attended_visit_count is hard-codeable from the key alone; no "
                "extraction is needed.", severity="major",
                suggested_attack="constants")),
            (_present("exactly 1 for every practitioner"), _finding(
                "attended_visit_count is declared constant: hard-code it and skip "
                "part of the computation.",
                "Part of practitioner_activity can be produced without solving "
                "anything.", severity="major", suggested_attack="constants")),
            (_present("ten times attended_visit_count"), _finding(
                "fee_total is a fixed multiple of attended_visit_count: derive it, "
                "never compute it.",
                "One measure determines the other, so the sources need never be "
                "read for it; it is hard-codeable from the other output column.",
                severity="major", suggested_attack="constants")),
            _tiny_population_findings(
                "practitioners", "three practitioners", "four practitioners",
                "seven practitioners"),
        ),
        fea: (
            (_absent("- status ("), _finding(
                "The public schema lacks the visits.status column the "
                "attended-visit filter requires.",
                "status is missing from the schema; the task is not solvable from "
                "public information.", severity="major")),
            (_absent("- procedures ("), _finding(
                "The procedures source table is missing from the public schema; "
                "procedure_units cannot be computed.",
                "Neither units nor any procedure row is available.",
                severity="major")),
            (lambda low, c: "- procedures (" in low and "- units (" not in low,
             _finding(
                "procedures.units is missing from the public schema.",
                "procedure_units cannot be computed without units; the task is not "
                "solvable.", severity="major")),
            (_absent("- fee ("), _finding(
                "visits.fee is missing from the public schema.",
                "fee_total cannot be computed without the fee; the task is not "
                "solvable.", severity="major")),
            (_absent("clinic the practitioner belongs to"), _finding(
                "practitioners.clinic_id is missing from the public schema.",
                "No practitioner can be joined to a clinic, so clinic_name is "
                "unreachable; the task is not solvable.", severity="major")),
            (_present("currency_rate column of the visits table"), _finding(
                "fee_total is specified as using visits.currency_rate, a column the "
                "public schema never publishes.",
                "The visits table publishes visit_id, practitioner_id, status, "
                "booking and fee only; currency_rate is absent, so fee_total cannot "
                "be computed from public material.", severity="major")),
            (_present("referral_source column on the visits table"), _finding(
                "walk_in_visit_count is restricted by visits.referral_source, a "
                "column the public schema never publishes.",
                "referral_source is missing from the visits schema, so the "
                "restriction cannot be applied and walk_in_visit_count is not "
                "computable.", severity="major")),
        ),
    }
    probe = _finding(
        "Diligence probe: hard-coded constants over practitioner_activity should "
        "lose reward on a hidden population.",
        "Emit fixed values for fee_total and attended_visit_count; expect the "
        "hidden graded populations to defeat this probe. Compile and run it as "
        "standing evidence.",
        severity="minor",
        suggested_attack="constants",
    )
    return FamilyOracleProvider(triggers, probe)


def _stock_oracle() -> FamilyOracleProvider:
    amb = CouncilRole.AMBIGUITY_CRITIC
    pop = CouncilRole.POPULATION_ADVERSARY
    shc = CouncilRole.SHORTCUT_ATTACKER
    fea = CouncilRole.FEASIBILITY_REVIEWER
    triggers = {
        amb: (
            (_present("strictly below"), _finding(
                "stock_state's column description contradicts the "
                "inclusive-threshold rule.",
                "Rule 6 makes net_units <= reorder_point 'reorder' while the mart "
                "schema says strictly below; a product exactly on its threshold "
                "has two readings.", severity="fatal")),
            (_present("adjustments never enter"), _finding(
                "net_units's column description contradicts the adjustment rule.",
                "Rule 4 adds adjustment_units into net_units while the mart schema "
                "says adjustments never enter it; nothing says which wins, and "
                "adjustment moves are declared present.", severity="fatal")),
            (_absent("deduplicate"), _finding(
                "Duplicate move rows are never mentioned: move_count and the "
                "quantities are ambiguous.",
                "Without a dedupe rule the same move_id summed twice is as "
                "defensible as summing it once.", severity="major")),
            (_absent("net_units = inbound_units"), _finding(
                "How net_units is computed is not specified: whether adjustment "
                "moves enter it has two readings.",
                "adjustment_units is computed but never used by any stated rule; "
                "net_units could be inbound_units - outbound_units with or without "
                "the adjustment term.", severity="major")),
            (_absent("onto products"), _finding(
                "Whether products with no moves appear at all is unspecified.",
                "Nothing states that products with no moves are retained, so an "
                "implementer could emit them or omit them; the product_stock row "
                "set is ambiguous.", severity="major")),
            (_absent("coalesce"), _finding(
                "Ambiguous null handling: inbound_units, outbound_units, net_units "
                "and move_count for products with no moves could be NULL or zero.",
                "The prose omits the COALESCE-to-zero rule for the four "
                "quantities.", severity="major")),
            (_absent("stock_state = 'reorder'"), _finding(
                "stock_state is never defined: no rule names its values or the "
                "threshold that decides them.",
                "reorder_point is published but no stated rule compares net_units "
                "to it; stock_state is unspecified.", severity="major")),
        ),
        pop: (
            (_conditions_absent("no moves", "never had a single movement"), _finding(
                "An INNER JOIN is indistinguishable from the required LEFT JOIN: no "
                "graded population contains a product without moves.",
                "Every population (primary, resampled, counterfactual) now has a "
                "move for every product, so wrong join logic would score full "
                "reward.", severity="major", suggested_attack="inner_join")),
            (_conditions_absent("adjustment"), _finding(
                "No population declares an adjustment move, so the adjustment term "
                "of net_units is never exercised.",
                "A solver whose net_units ignores adjustments is indistinguishable "
                "from a correct one; it would still score full reward.",
                severity="major")),
            (_conditions_absent("duplicate stock_moves row"), _finding(
                "No population declares duplicate stock_moves rows any more — the "
                "stress population dropped them.",
                "The dedupe rule is never exercised, so omitting DISTINCT over "
                "move_id is indistinguishable from correct logic.",
                severity="major")),
            (_conditions_absent("moves have null sku"), _finding(
                "No graded population states that any move carries a NULL sku.",
                "primary, resampled, stress and counterfactual all guarantee a "
                "product for every move, so a solver that attributes unowned moves "
                "would still score full reward — the exclusion rule is never "
                "exercised.", severity="major")),
            (_conditions_absent("the inclusive boundary"), _finding(
                "No population puts a product's net_units exactly on its "
                "reorder_point, so the inclusive boundary of stock_state is never "
                "exercised.",
                "A solver reading the threshold as strict (net_units < "
                "reorder_point) is indistinguishable from the required <=; it "
                "would still score full reward on every graded population.",
                severity="major")),
            (_conditions_present("carries a sku, and appears once"), _finding(
                "Every discriminating condition sits in development, the split the "
                "solver can see.",
                "primary, resampled, stress and counterfactual are all stated to be "
                "uniform — inbound or outbound, owned, unique, off-threshold — so "
                "no GRADED population can distinguish wrong logic; it would still "
                "score full reward.", severity="major")),
        ),
        shc: (
            (_present("always exactly the same value"), _finding(
                "net_units, move_count and stock_state are declared constant: "
                "hard-code them for full reward.",
                "product_stock can be produced without solving.",
                severity="major", suggested_attack="constants")),
            (_present("determined by the sku alone"), _finding(
                "net_units is a function of sku alone: copy the key, skip the "
                "computation.",
                "move_count is hard-codeable from the key alone; no extraction is "
                "needed.", severity="major", suggested_attack="constants")),
            (_present("exactly 1 for every product"), _finding(
                "move_count is declared constant: hard-code it and skip part of the "
                "computation.",
                "Part of product_stock can be produced without solving anything.",
                severity="major", suggested_attack="constants")),
            (_present("exactly half of inbound_units"), _finding(
                "outbound_units is a fixed fraction of inbound_units: derive it, "
                "never compute it.",
                "One measure determines the other, so the sources need never be "
                "read for it; it is hard-codeable from the other output column.",
                severity="major", suggested_attack="constants")),
            _tiny_population_findings(
                "products", "three products", "three products", "six products"),
        ),
        fea: (
            (_absent("- move_type ("), _finding(
                "The public schema lacks the stock_moves.move_type column the "
                "per-kind sums require.",
                "move_type is missing from the schema, so inbound_units and "
                "outbound_units cannot be computed; not solvable from public "
                "information.", severity="major")),
            (_absent("- reorder_point ("), _finding(
                "products.reorder_point is missing from the public schema.",
                "stock_state cannot be decided without the reorder point; the task "
                "is not solvable.", severity="major")),
            (_absent("- quantity ("), _finding(
                "stock_moves.quantity is missing from the public schema.",
                "inbound_units, outbound_units and net_units cannot be computed "
                "without the quantity; not solvable.", severity="major")),
            (_absent("- moved_on ("), _finding(
                "stock_moves.moved_on is missing from the public schema.",
                "last_moved_on cannot be computed without the movement date; not "
                "solvable.", severity="major")),
            (_absent("- category (text; one of"), _finding(
                "products.category is missing from the public schema.",
                "The category passthrough cannot be produced; not solvable.",
                severity="major")),
            (_present("pack_size column of the products table"), _finding(
                "inbound_units is specified as using products.pack_size, a column "
                "the public schema never publishes.",
                "pack_size is absent, so inbound_units cannot be computed from "
                "public material.", severity="major")),
            (_present("warehouse_code column on the stock_moves table"), _finding(
                "move_count is restricted by stock_moves.warehouse_code, a column "
                "the public schema never publishes.",
                "warehouse_code is missing from the stock_moves schema, so the "
                "restriction cannot be applied and move_count is not computable.",
                severity="major")),
        ),
    }
    probe = _finding(
        "Diligence probe: hard-coded constants over product_stock should lose "
        "reward on a hidden population.",
        "Emit fixed values for net_units and move_count; expect the hidden graded "
        "populations to defeat this probe. Compile and run it as standing "
        "evidence.",
        severity="minor",
        suggested_attack="constants",
    )
    return FamilyOracleProvider(triggers, probe)


def oracle_for(family_name: str):
    """The diligent reader of one family (the demo's is the metrology suite's)."""
    return {
        "demo": OracleProvider,
        "clinic_visits": _clinic_oracle,
        "stock_ledger": _stock_oracle,
    }[family_name]()


# ---------------------------------------------------------------------------
# The API metrology codes against
# ---------------------------------------------------------------------------


class FamilyApiTest(unittest.TestCase):
    def test_families_mapping_has_demo_and_at_least_two_new_families(self):
        self.assertIn("demo", FAMILIES)
        self.assertGreaterEqual(len([n for n in FAMILIES if n != "demo"]), 2)
        self.assertEqual(tuple(FAMILIES), FAMILY_NAMES)
        self.assertEqual(len(FAMILIES), len(FAMILY_NAMES))
        for name in FAMILIES:
            self.assertIs(FAMILIES[name], FAMILIES[name], name)  # memoized
        with self.assertRaises(KeyError):
            FAMILIES["no_such_family"]

    def test_every_family_has_the_declared_api_shape(self):
        guids = set()
        for name, family in FAMILIES.items():
            with self.subTest(family=name):
                self.assertIsInstance(family, FixtureFamily)
                self.assertEqual(family.name, name)
                self.assertIsInstance(family.task(), TaskIR)
                self.assertIsNot(family.task(), family.task())  # fresh per call
                self.assertEqual(set(family.specimen_builders), set(SPECIMEN_KINDS))
                self.assertTrue(
                    set(family.anchors) <= {r.value for r in CRITIC_ROLES}, family.anchors
                )
                for role in CRITIC_ROLES:
                    self.assertTrue(family.anchors_for(role), role.value)
                self.assertRegex(
                    family.canary_guid,
                    r"^elt-taskgen-metrology-family-canary:[a-z_]+:[0-9a-f-]{36}$",
                )
                guids.add(family.canary_guid)
        self.assertEqual(len(guids), len(FAMILIES))
        self.assertNotIn(metrology_mod.METROLOGY_CANARY, guids)

    def test_every_anchor_resolves_in_the_clean_task(self):
        for name, family in FAMILIES.items():
            task = family.task()
            for role, anchors in family.anchors.items():
                for anchor in anchors:
                    with self.subTest(family=name, anchor=anchor.target):
                        self.assertIsInstance(anchor, InjectorAnchor)
                        self.assertEqual(anchor.role, role)
                        self.assertIn(anchor.kind, ANCHOR_KINDS)
                        self.assertTrue(anchor.note)
                        resolve_anchor(task, anchor)  # raises on a dangling target
        with self.assertRaises(ValueError):
            resolve_anchor(
                FAMILIES["demo"].task(),
                InjectorAnchor("feasibility_reviewer", "drop_column", "orders.nope", "x"),
            )
        with self.assertRaises(ValueError):
            InjectorAnchor("feasibility_reviewer", "not_a_kind", "orders.status", "x")

    def test_every_specimen_builder_targets_its_anchor_role(self):
        for name, family in FAMILIES.items():
            for kind, role in KIND_ROLES.items():
                specimens = family.specimen_builders[kind]()
                with self.subTest(family=name, kind=kind):
                    self.assertTrue(specimens)
                    self.assertEqual({s.kind for s in specimens}, {"tampered"})
                    self.assertEqual({s.target_role for s in specimens}, {role})
                    self.assertTrue(family.anchors_for(role))
            cleans = family.specimen_builders["clean"]()
            self.assertEqual({s.kind for s in cleans}, {"clean"})

    def test_demo_family_wraps_the_existing_pool_byte_for_byte(self):
        def key(s):
            return (s.name, s.task.content_hash(), s.task.solver_prompt,
                    tuple(v.content_hash() for v in s.variants))

        wrapped = sorted(key(s) for s in FAMILIES["demo"].specimens())
        # The pool metrology draws from carries every family; the demo family
        # is exactly its demo-named members, unchanged.
        pool = sorted(
            key(s) for s in metrology_mod.specimen_pool() if family_of(s.name) == "demo"
        )
        self.assertEqual(wrapped, pool)
        self.assertEqual(
            FAMILIES["demo"].task().content_hash(),
            metrology_mod.demo_fixture.demo_task().content_hash(),
        )
        for kind, builder in (
            ("clean", metrology_mod.clean_specimens),
            ("ambiguity", metrology_mod.ambiguity_specimens),
            ("population", metrology_mod.population_specimens),
            ("shortcut", metrology_mod.shortcut_specimens),
            ("feasibility", metrology_mod.feasibility_specimens),
        ):
            self.assertEqual(
                [key(s) for s in FAMILIES["demo"].specimen_builders[kind]()],
                [key(s) for s in builder()],
                kind,
            )

    def test_demo_anchors_match_the_metrology_injector_sources(self):
        family = FAMILIES["demo"]
        omitted = {
            int(a.target.split("ops[")[1][:-1])
            for a in family.anchors_for(CouncilRole.AMBIGUITY_CRITIC)
            if a.kind == "omit_op"
        }
        declared = set()
        for _, omit, _, _, _, _ in metrology_mod._AMBIGUITY_VARIANTS:
            declared |= set(omit)
        self.assertEqual(omitted, declared)
        drops = {
            (a.kind, a.target)
            for a in family.anchors_for(CouncilRole.FEASIBILITY_REVIEWER)
            if a.kind in ("drop_column", "drop_table")
        }
        expected = {
            ("drop_table", table) if column is None else ("drop_column", f"{table}.{column}")
            for _, table, column, _, _ in metrology_mod._FEASIBILITY_VARIANTS
        }
        self.assertEqual(drops, expected)

    def test_family_of_parses_the_name_suffix(self):
        self.assertEqual(family_of("clean-plain"), "demo")
        self.assertEqual(
            specimen_name("clinic_visits", "clean-plain"), "clean-plain@clinic_visits"
        )
        self.assertEqual(family_of(specimen_name("clinic_visits", "clean-plain")), "clinic_visits")
        for name, family in FAMILIES.items():
            for s in family.specimens():
                self.assertEqual(family_of(s.name), name, s.name)

    def test_specimen_names_keep_the_class_prefix_and_are_unique_across_families(self):
        """Pool consumers key on the CLASS prefix (`feasibility-...`, e.g.
        tests/test_structural_completeness.py), so the family rides as a
        suffix and demo names are untouched."""
        names: list[str] = []
        for name, family in FAMILIES.items():
            for s in family.specimens():
                names.append(s.name)
                own = s.name.split(FAMILY_SEPARATOR)[0]
                self.assertTrue(
                    own.startswith(tuple(f"{kind}-" for kind in SPECIMEN_KINDS)), s.name
                )
                if name != "demo":
                    self.assertTrue(s.name.endswith(FAMILY_SEPARATOR + name), s.name)
                else:
                    self.assertNotIn(FAMILY_SEPARATOR, s.name)
        self.assertEqual(len(names), len(set(names)))

    def test_build_prose_port_is_byte_identical_to_metrology_on_the_demo(self):
        """The families' count-neutral `build_prose` reproduces metrology's
        exactly where metrology's fixed "three" is right (the demo)."""
        task = FAMILIES["demo"].task()
        for variant in range(metrology_mod.PROSE_VARIANTS):
            for omit in (frozenset(), frozenset({0}), frozenset({1, 6})):
                self.assertEqual(
                    build_prose(task, omit_op_indices=omit, variant=variant),
                    metrology_mod.build_prose(task, omit_op_indices=omit, variant=variant),
                )
        clinic = FAMILIES["clinic_visits"].task()
        self.assertIn("four operational sources", build_prose(clinic).lower())
        self.assertNotIn("three operational", build_prose(clinic).lower())


# ---------------------------------------------------------------------------
# Every new family passes the gates the demo fixture passes
# ---------------------------------------------------------------------------


class FamilyGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = {name: family.task() for name, family in _new_families()}
        cls.rows = {
            name: {pop: source_data.generate_rows(task, pop) for pop in P}
            for name, task in cls.tasks.items()
        }

    def test_task_validates_and_is_complete(self):
        demo_hash = FAMILIES["demo"].task().content_hash()
        seen_ids = {FAMILIES["demo"].task().task_id}
        for name, task in self.tasks.items():
            with self.subTest(family=name):
                TaskIR.model_validate(task.model_dump())
                self.assertTrue(task.has_all_populations())
                self.assertEqual(len(task.marts), 1)
                self.assertEqual(set(task.reference.sql_by_mart), {task.marts[0].name})
                self.assertNotEqual(task.content_hash(), demo_hash)
                self.assertNotIn(task.task_id, seen_ids)
                seen_ids.add(task.task_id)
                required_el = {
                    c.name for c in task.attack_cases
                    if c.mutation.startswith("directive:load:") and c.required
                }
                self.assertTrue({"partial_backend", "duplicate_on_load"} <= required_el)
                self.assertGreaterEqual(
                    len([c for c in task.attack_cases if not c.mutation.startswith("directive:")]),
                    5,
                )

    def test_families_are_distinct_star_schemas(self):
        """Different domains, table counts, key shapes and mart plans — never a
        renamed copy of the demo or of each other."""
        shapes = {}
        for name, family in FAMILIES.items():
            task = family.task()
            mart = task.marts[0]
            shapes[name] = (
                len(task.tables),
                tuple(sorted(t.name for t in task.tables)),
                tuple(c.type for t in task.tables for c in t.columns if c.name in t.primary_key),
                tuple(op.kind for op in mart.plan.ops),
                tuple(c.name for c in mart.columns),
            )
        self.assertEqual(len({s[0] for s in shapes.values()}), len(shapes))  # table counts
        self.assertEqual(len({s[3] for s in shapes.values()}), len(shapes))  # plan shapes
        self.assertEqual(len({s[4] for s in shapes.values()}), len(shapes))  # mart columns
        all_tables = [t for s in shapes.values() for t in s[1]]
        self.assertEqual(len(all_tables), len(set(all_tables)))  # no shared table names
        self.assertIn(ColumnType.TEXT, shapes["stock_ledger"][2])  # a TEXT key shape

    def test_task_passes_population_coverage(self):
        for name, task in self.tasks.items():
            with self.subTest(family=name):
                self.assertEqual(populations.validate_population_coverage(task), [])

    def test_task_is_structurally_complete(self):
        for name, task in self.tasks.items():
            with self.subTest(family=name):
                self.assertEqual(check_structural_completeness(task), [])

    def test_plan_is_task_coherent(self):
        """Same acceptance as the demo: every table declared, every column
        resolves, every mart column produced, every join FK-backed."""
        for name, task in self.tasks.items():
            with self.subTest(family=name):
                problems = mp.validate_plan(task, task.marts[0].plan)
                for marker in ("unknown tables", "not backed by", "not produced by any op",
                               "not found in referenced tables"):
                    self.assertFalse([p for p in problems if marker in p], problems)

    def test_private_sql_is_scannable(self):
        for name, task in self.tasks.items():
            with self.subTest(family=name):
                self.assertEqual(council_mod._unscannable_private_sources(task), [])

    def test_reference_reproduces_the_counterfactual_expectation(self):
        for name, task in self.tasks.items():
            mod = NEW_FAMILY_MODULES[name]
            with self.subTest(family=name):
                cf = task.population(P.COUNTERFACTUAL)
                self.assertEqual(cf.literal_rows, mod.COUNTERFACTUAL_LITERAL_ROWS)
                got = _run_sql(task, self.rows[name][P.COUNTERFACTUAL], mod.REFERENCE_SQL)
                columns = [c.name for c in task.marts[0].columns]
                expected = [tuple(r[c] for c in columns) for r in mod.COUNTERFACTUAL_EXPECTED_MART]
                self.assertTrue(_rows_close(got, expected), f"got {got}")

    def test_reference_executes_on_every_generated_population(self):
        for name, task in self.tasks.items():
            mod = NEW_FAMILY_MODULES[name]
            for pop in P:
                with self.subTest(family=name, population=pop.value):
                    rows = _run_sql(task, self.rows[name][pop], mod.REFERENCE_SQL)
                    key = [c.name for c in task.marts[0].columns].index(task.marts[0].key_columns[0])
                    keys = [r[key] for r in rows]
                    self.assertEqual(len(keys), len(set(keys)))
                    grain_table = next(
                        t.name for t in task.tables
                        if t.primary_key == task.marts[0].key_columns
                    )
                    self.assertEqual(len(rows), len(self.rows[name][pop][grain_table]))

    def test_sql_attack_matrix_reproduced_on_generated_data(self):
        for name, task in self.tasks.items():
            mod = NEW_FAMILY_MODULES[name]
            reference = {pop: _run_sql(task, self.rows[name][pop], mod.REFERENCE_SQL) for pop in P}
            for case in task.attack_cases:
                if case.mutation.startswith("directive:"):
                    continue
                self.assertTrue(case.expected_pass, case.name)
                for pop, should_pass in case.expected_pass.items():
                    with self.subTest(family=name, attack=case.name, population=pop.value):
                        mutant = _run_sql(task, self.rows[name][pop], case.mutation)
                        self.assertEqual(
                            _rows_close(reference[pop], mutant),
                            should_pass,
                            f"{name}/{case.name} on {pop.value}",
                        )

    def _clinic_stats(self, rows):
        by_pr = defaultdict(set)
        for v in rows["visits"]:
            if v["practitioner_id"] is not None:
                by_pr[v["practitioner_id"]].add(v["status"])
        practitioners = [p["practitioner_id"] for p in rows["practitioners"]]
        procs = Counter(x["visit_id"] for x in rows["procedures"])
        return {
            "no_visits": sum(1 for p in practitioners if p not in by_pr),
            "no_attended": sum(1 for p in practitioners if p in by_pr and "attended" not in by_pr[p]),
            "all_attended": all("attended" in by_pr.get(p, set()) for p in practitioners),
            "null_attended": sum(
                1 for v in rows["visits"] if v["practitioner_id"] is None and v["status"] == "attended"
            ),
            "null_any": sum(1 for v in rows["visits"] if v["practitioner_id"] is None),
            "multi_procedure": sum(1 for c in procs.values() if c > 1),
            "dup_visits": len(rows["visits"]) - len({tuple(sorted(v.items())) for v in rows["visits"]}),
            "dup_procedures": len(rows["procedures"])
            - len({tuple(sorted(v.items())) for v in rows["procedures"]}),
            "cancelled": sum(1 for v in rows["visits"] if v["status"] == "cancelled"),
        }

    def _stock_stats(self, rows):
        by_sku = defaultdict(set)
        for v in rows["stock_moves"]:
            if v["sku"] is not None:
                by_sku[v["sku"]].add(v["move_type"])
        skus = [p["sku"] for p in rows["products"]]
        return {
            "no_moves": sum(1 for s in skus if s not in by_sku),
            "all_three_kinds": all(by_sku.get(s) == {"inbound", "outbound", "adjustment"} for s in skus),
            "null_sku": sum(1 for v in rows["stock_moves"] if v["sku"] is None),
            "adjustments": sum(1 for v in rows["stock_moves"] if v["move_type"] == "adjustment"),
            "dups": len(rows["stock_moves"])
            - len({tuple(sorted(v.items())) for v in rows["stock_moves"]}),
        }

    def test_declared_conditions_hold_on_generated_rows(self):
        clinic = {pop: self._clinic_stats(r) for pop, r in self.rows["clinic_visits"].items()}
        primary, stress, dev = clinic[P.PRIMARY], clinic[P.STRESS], clinic[P.DEVELOPMENT]
        self.assertGreater(primary["no_visits"], 0)
        self.assertGreater(primary["no_attended"], 0)
        self.assertGreater(primary["null_attended"], 0)
        self.assertGreater(primary["multi_procedure"], 0)
        self.assertGreater(primary["cancelled"], 0)
        self.assertGreater(stress["dup_visits"], 0)
        self.assertGreater(stress["dup_procedures"], 0)
        self.assertTrue(stress["all_attended"])
        self.assertTrue(dev["all_attended"])
        self.assertEqual(dev["null_any"], 0)
        self.assertEqual(dev["dup_visits"], 0)
        self.assertGreater(dev["multi_procedure"], 0)
        self.assertEqual(
            {t: len(r) for t, r in self.rows["clinic_visits"][P.DEVELOPMENT].items()},
            {"clinics": 2, "practitioners": 3, "visits": 12, "procedures": 20},
        )
        stock = {pop: self._stock_stats(r) for pop, r in self.rows["stock_ledger"].items()}
        primary, stress, dev = stock[P.PRIMARY], stock[P.STRESS], stock[P.DEVELOPMENT]
        self.assertGreater(primary["no_moves"], 0)
        self.assertGreater(primary["null_sku"], 0)
        self.assertGreater(primary["adjustments"], 0)
        states = {
            row[-1]
            for row in _run_sql(
                self.tasks["stock_ledger"], self.rows["stock_ledger"][P.PRIMARY],
                stock_ledger.REFERENCE_SQL,
            )
        }
        self.assertEqual(states, {"reorder", "stocked"})
        self.assertGreater(stress["dups"], 0)
        self.assertTrue(stress["all_three_kinds"])
        self.assertTrue(dev["all_three_kinds"])
        self.assertEqual(dev["null_sku"], 0)
        self.assertEqual(dev["dups"], 0)
        self.assertEqual(
            {t: len(r) for t, r in self.rows["stock_ledger"][P.DEVELOPMENT].items()},
            {"products": 3, "stock_moves": 12},
        )


# ---------------------------------------------------------------------------
# Every family's pool is well formed
# ---------------------------------------------------------------------------


#: Per family: omission specimen -> the ONE rule string it strips from the
#: prose (every other specimen keeps it), and the contradiction specimens.
_STRIPPED = {
    "clinic_visits": {
        "ambiguity-no-null-rule": "coalesce",
        "ambiguity-no-dedupe": "deduplicate",
        "ambiguity-no-filter": "status = 'attended'",
        "ambiguity-no-procedure-fanout-rule": "grouped by visit_id",
        "ambiguity-no-left-join-rule": "onto practitioners",
    },
    "stock_ledger": {
        "ambiguity-no-dedupe": "deduplicate",
        "ambiguity-no-adjustment-rule": "net_units = inbound_units",
        "ambiguity-no-left-join-rule": "onto products",
        "ambiguity-no-null-rule": "coalesce",
        "ambiguity-no-threshold-rule": "stock_state = 'reorder'",
    },
}

#: Per family: omission specimen -> phrases that RESTATED the stripped rule in
#: the critic view and must be gone from it.
_RESTATEMENTS = {
    "clinic_visits": {
        "ambiguity-no-dedupe": ("distinct",),
        "ambiguity-no-null-rule": ("0 if none", "coalesce"),
        "ambiguity-no-procedure-fanout-rule": ("sum of units over the procedure rows",),
        "ambiguity-no-left-join-rule": (
            "including practitioners with no visits", "0 if none", "'idle'",
        ),
    },
    "stock_ledger": {
        "ambiguity-no-dedupe": ("distinct",),
        "ambiguity-no-null-rule": ("0 if none", "coalesce", "null for a product with no moves"),
        "ambiguity-no-left-join-rule": (
            "including products with no moves", "0 if none", "null for a product with no moves",
        ),
        "ambiguity-no-adjustment-rule": (
            "plus the product's adjustment quantity", "inbound_units - outbound_units",
        ),
        "ambiguity-no-threshold-rule": ("<= reorder_point", "'reorder' or 'stocked'", "inclusive"),
    },
}

#: Per family: contradiction specimen -> (the rewritten description, the rule
#: strings it disagrees with, which survive verbatim in the prose).
_CONTRADICTIONS = {
    "clinic_visits": {
        "ambiguity-fee-contradicts-grain": (
            "contributes its fee three times",
            ("at the visit grain and never at the procedure-row grain",),
        ),
        "ambiguity-band-contradicts-thresholds": (
            "from two attended visits upward",
            ("'busy' when attended_visit_count >= 3",),
        ),
    },
    "stock_ledger": {
        "ambiguity-state-contradicts-threshold": (
            "strictly below",
            ("net_units <= reorder_point",),
        ),
        "ambiguity-net-contradicts-adjustments": (
            "adjustments never enter it",
            ("net_units = inbound_units - outbound_units + adjustment_units",),
        ),
    },
}

#: Per family: population specimen -> the discriminator phrase its conditions
#: LOSE (present in the clean conditions); the development-only specimen is
#: the presence check below.
_DISCRIMINATORS = {
    "clinic_visits": {
        "population-dropped-counterfactual": "no visits",
        "population-neutered-counterfactual": "no visits",
        "population-no-cancelled-visits": "cancelled",
        "population-no-duplicate-visits": "duplicate visit row",
        "population-no-null-practitioner-visits": "visits have null practitioner_id",
        "population-single-procedure-visits": "several procedure rows",
    },
    "stock_ledger": {
        "population-dropped-counterfactual": "no moves",
        "population-neutered-counterfactual": "no moves",
        "population-no-adjustment-moves": "adjustment",
        "population-no-duplicate-moves": "duplicate stock_moves row",
        "population-no-null-sku-moves": "moves have null sku",
        "population-never-on-threshold": "the inclusive boundary",
    },
}
_DEVELOPMENT_ONLY_MARKER = {
    "clinic_visits": "carries a practitioner_id, and appears once",
    "stock_ledger": "carries a sku, and appears once",
}

#: Per family: the rule phrases the clean prose states (what injectors strip).
_CLEAN_RULES = {
    "clinic_visits": ("sort by practitioner_id", "coalesce", "deduplicate",
                      "status = 'attended'", "- status (", "grouped by visit_id"),
    "stock_ledger": ("sort by sku", "coalesce", "deduplicate", "- move_type (",
                     "net_units = inbound_units", "stock_state = 'reorder'"),
}


class FamilyPoolTest(unittest.TestCase):
    """The POOL of every new family is well formed (the demo's pool keeps its
    own tests in tests/test_council_efficacy.py)."""

    def test_pool_is_deterministic_uniquely_named_and_holds_specimens_back(self):
        for name, family in _new_families():
            with self.subTest(family=name):
                first = family.specimens()
                second = family.specimens()
                names = [s.name for s in first]
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(
                    [(s.name, s.task.content_hash(), s.task.solver_prompt) for s in first],
                    [(s.name, s.task.content_hash(), s.task.solver_prompt) for s in second],
                )

                cleans = [s for s in first if s.kind == "clean"]
                self.assertGreaterEqual(
                    len(cleans), metrology_mod.CLEAN_PER_RUN + metrology_mod.POOL_HOLDOUT
                )
                for role in CRITIC_ROLES:
                    candidates = [s for s in first if s.target_role is role]
                    self.assertGreaterEqual(
                        len(candidates),
                        metrology_mod.TAMPERED_PER_ROLE + metrology_mod.POOL_HOLDOUT,
                        role.value,
                    )
                for s in first:
                    self.assertEqual(len(s.variants), metrology_mod.PROSE_VARIANTS, s.name)
                    self.assertIs(s.task, s.variants[0])

    def test_stock_per_type_and_no_group_zero_defaults_are_distinct_and_public(self):
        """A conditional SUM's empty-type case is not the LEFT JOIN empty-group case.

        The clean fixture used to publish only the latter.  That left two valid
        SQL readings for a product with inbound/outbound moves but no adjustment
        move, while the private reference silently chose CASE ... ELSE 0.  Pin
        both public rules, with disjoint scopes, so a clean task never depends on
        hidden implementation intent and the no-null tamper can still remove the
        separate no-group default.
        """
        task = FAMILIES["stock_ledger"].task()
        aggregate = task.marts[0].plan.ops[1].description
        no_group = task.marts[0].plan.ops[4].description
        phrase = (
            "Within an existing sku group, a missing move type contributes 0 "
            "to its per-type sum."
        )
        self.assertIn(phrase, aggregate)
        self.assertNotIn("products without moves", aggregate)
        self.assertIn("products without moves", no_group)
        self.assertIn("adjustment_units = 0", no_group)
        self.assertIn("yielding net_units = 0", no_group)
        self.assertIn("adjustment_units is an internal term, not a mart output", no_group)
        self.assertIn("The COALESCE list names only the published", no_group)
        self.assertIn("stock_state from this final net_units", no_group)
        clean = next(
            specimen
            for specimen in FAMILIES["stock_ledger"].specimen_builders["clean"]()
            if specimen.name.endswith("clean-decoy-b@stock_ledger")
        )
        self.assertIn(phrase, council_mod._critic_view(clean.task))

        no_null = _specimens_by_kind(FAMILIES["stock_ledger"], "ambiguity")[
            specimen_name("stock_ledger", "ambiguity-no-null-rule")
        ]
        tampered_view = council_mod._critic_view(no_null.task)
        self.assertIn(phrase, tampered_view)
        self.assertNotIn("adjustment_units = 0", tampered_view)
        self.assertNotIn("internal term, not a mart output", tampered_view)
        self.assertNotIn("stock_state from this final net_units", tampered_view)

        # The no-LEFT-JOIN specimen must scrub the new no-group wording without
        # deleting its independent arithmetic/default semantics. This exact
        # rewrite is deliberately pinned because fixture rewrites fail closed
        # when the clean rule's source sentence changes.
        no_left = _specimens_by_kind(FAMILIES["stock_ledger"], "ambiguity")[
            specimen_name("stock_ledger", "ambiguity-no-left-join-rule")
        ]
        no_left_view = council_mod._critic_view(no_left.task)
        self.assertNotIn("For products without moves", no_left_view)
        self.assertIn("Wherever the per-sku aggregate would be NULL", no_left_view)
        self.assertIn("adjustment_units = 0", no_left_view)
        self.assertIn("stock_state from this final net_units", no_left_view)

    def test_every_tampered_specimen_declares_both_scoring_axes(self):
        for name, family in _new_families():
            for specimen in family.specimens():
                with self.subTest(family=name, specimen=specimen.name):
                    if specimen.kind == "clean":
                        self.assertEqual(specimen.detection_terms, ())
                        self.assertEqual(specimen.anchor_terms, ())
                        continue
                    self.assertTrue(specimen.detection_terms)
                    self.assertTrue(specimen.anchor_terms)

    def test_clean_prose_contains_the_rules_the_injectors_strip(self):
        for name, family in _new_families():
            for specimen in family.specimen_builders["clean"]():
                low = specimen.task.solver_prompt.lower()
                for rule in _CLEAN_RULES[name]:
                    self.assertIn(rule, low, f"{specimen.name}: {rule}")

    def test_adversarial_cleans_carry_defect_vocabulary_while_staying_clean(self):
        for name, family in _new_families():
            cleans = _specimens_by_kind(family, "clean")
            decoys = [s for n, s in cleans.items() if "clean-decoy" in n]
            self.assertEqual(len(decoys), 4, name)
            for specimen in decoys:
                low = specimen.task.solver_prompt.lower()
                for bait in ("tie", "null", "constant", "missing"):
                    self.assertIn(bait, low, f"{specimen.name}: {bait}")
                for rule in _CLEAN_RULES[name]:
                    self.assertIn(rule, low)
                self.assertEqual(
                    specimen.task.model_copy(update={"solver_prompt": ""}).content_hash(),
                    family.task().content_hash(),
                    f"{specimen.name}: a decoy edits nothing but the prose",
                )
            reworded = cleans[specimen_name(name, "clean-reworded-counterfactual")]
            plain = cleans[specimen_name(name, "clean-plain")]
            grep_phrase = _DISCRIMINATORS[name]["population-dropped-counterfactual"]
            reworded_cf = " ".join(reworded.task.population(P.COUNTERFACTUAL).conditions).lower()
            plain_cf = " ".join(plain.task.population(P.COUNTERFACTUAL).conditions).lower()
            self.assertIn(grep_phrase, plain_cf, name)
            self.assertNotIn(grep_phrase, reworded_cf, name)
            self.assertEqual(
                reworded.task.population(P.COUNTERFACTUAL).literal_rows,
                plain.task.population(P.COUNTERFACTUAL).literal_rows,
            )

    def test_ambiguity_omissions_strip_exactly_one_rule_each(self):
        for name, family in _new_families():
            by_name = _specimens_by_kind(family, "ambiguity")
            stripped = {specimen_name(name, k): v for k, v in _STRIPPED[name].items()}
            contradictions = {specimen_name(name, k) for k in _CONTRADICTIONS[name]}
            self.assertEqual(set(by_name), set(stripped) | contradictions, name)
            for cname in contradictions:
                low = by_name[cname].task.solver_prompt.lower()
                for kept in stripped.values():
                    self.assertIn(kept, low, f"{cname} lost the rule {kept!r}")
            for sname, gone in stripped.items():
                low = by_name[sname].task.solver_prompt.lower()
                self.assertNotIn(gone, low, sname)
                for other, kept in stripped.items():
                    if other != sname:
                        self.assertIn(kept, low, f"{sname} also lost {kept}")

    def test_ambiguity_omissions_also_strip_every_restatement_in_the_view(self):
        for name, family in _new_families():
            by_name = _specimens_by_kind(family, "ambiguity")
            clean_view = council_mod._critic_view(
                _specimens_by_kind(family, "clean")[specimen_name(name, "clean-plain")].task
            ).lower()
            for sname, gone in _RESTATEMENTS[name].items():
                view = council_mod._critic_view(by_name[specimen_name(name, sname)].task).lower()
                for phrase in gone:
                    self.assertIn(phrase, clean_view, f"{name}: clean never states {phrase!r}")
                    self.assertNotIn(
                        phrase, view,
                        f"{sname}: the critic VIEW still restates {phrase!r}",
                    )

    def test_ambiguity_contradictions_leave_both_halves_on_screen(self):
        for name, family in _new_families():
            by_name = _specimens_by_kind(family, "ambiguity")
            clean_view = council_mod._critic_view(
                _specimens_by_kind(family, "clean")[specimen_name(name, "clean-plain")].task
            ).lower()
            for sname, (rewritten, rules) in _CONTRADICTIONS[name].items():
                view = council_mod._critic_view(by_name[specimen_name(name, sname)].task).lower()
                prose, schema = view.split("mart output schemas:", 1)
                self.assertIn(rewritten, schema, sname)
                self.assertNotIn(rewritten, clean_view, f"{sname}: not a tamper at all")
                for rule in rules:
                    self.assertIn(rule, prose, f"{sname}: lost the contradicted rule")

    def test_stripping_the_filter_rule_leaves_scope_unresolvable(self):
        """clinic_visits: after the strip no surviving surface names which
        visits are in scope (the enum domain and the column identifier are
        the only permitted survivals, exactly as for the demo)."""
        family = FAMILIES["clinic_visits"]
        specimen = _specimens_by_kind(family, "ambiguity")[
            specimen_name("clinic_visits", "ambiguity-no-filter")
        ]
        allowed_by_view = {
            "prose": (
                specimen.task.solver_prompt.lower(),
                ("attended_visit_count", "status (text; one of attended, cancelled)"),
            ),
            "critic view": (
                council_mod._critic_view(specimen.task).lower(),
                (
                    "attended_visit_count",
                    "status (text; one of attended, cancelled)",
                    "always exactly one of 'attended', 'cancelled'",
                    "one of: attended, cancelled",
                ),
            ),
        }
        for view_name, (text, allowed_terms) in allowed_by_view.items():
            residue = text
            for allowed in allowed_terms:
                self.assertIn(allowed, residue, f"{view_name}: missing {allowed}")
                residue = residue.replace(allowed, "")
            self.assertNotIn("attended", residue, f"{view_name}: the scope is repaired by context")
            self.assertNotIn("cancelled", residue, view_name)
        self.assertIn("status = 'attended'", family.task().marts[0].plan.ops[0].description)

    def test_population_injections_blind_exactly_one_discriminator(self):
        for name, family in _new_families():
            by_name = _specimens_by_kind(family, "population")
            clean = _conditions_of(family.task())
            for phrase in set(_DISCRIMINATORS[name].values()):
                self.assertIn(phrase, clean, f"{name}: clean conditions never state {phrase!r}")
            self.assertNotIn(_DEVELOPMENT_ONLY_MARKER[name], clean)
            for sname, phrase in _DISCRIMINATORS[name].items():
                conditions = _conditions_of(by_name[specimen_name(name, sname)].task)
                self.assertNotIn(phrase, conditions, sname)
                self.assertNotIn(_DEVELOPMENT_ONLY_MARKER[name], conditions, sname)
            dev_only = by_name[specimen_name(name, "population-discriminators-in-development")]
            conditions = _conditions_of(dev_only.task)
            self.assertIn(_DEVELOPMENT_ONLY_MARKER[name], conditions)
            for phrase in set(_DISCRIMINATORS[name].values()):
                self.assertIn(phrase, conditions, f"{name}: development lost {phrase!r}")
            dropped = by_name[specimen_name(name, "population-dropped-counterfactual")]
            self.assertFalse(dropped.task.has_all_populations())
            self.assertNotIn("population counterfactual", _conditions_of(dropped.task))

    def test_shortcut_injections_make_the_graded_surface_scoreable(self):
        for name, family in _new_families():
            by_name = _specimens_by_kind(family, "shortcut")
            key = family.task().marts[0].key_columns[0]
            constant = by_name[specimen_name(name, "shortcut-constant-columns")]
            view = council_mod._critic_view(constant.task).lower()
            self.assertIn("always exactly the same value", view)
            key_line = next(
                line for line in view.splitlines() if line.strip().startswith(f"- {key} (")
            )
            self.assertNotIn("always exactly the same value", key_line, name)
            self.assertIn(
                "key alone",
                council_mod._critic_view(by_name[specimen_name(name, "shortcut-identity-columns")].task),
            )
            dev_only = council_mod._critic_view(
                by_name[specimen_name(name, "shortcut-dev-only-population")].task
            )
            self.assertNotIn("population primary", dev_only)
            self.assertIn("population development", dev_only)
            cf_only = council_mod._critic_view(
                by_name[specimen_name(name, "shortcut-counterfactual-only-population")].task
            )
            self.assertNotIn("population primary", cf_only)
            self.assertIn("population counterfactual", cf_only)

    def test_feasibility_injections_drop_public_schema_elements(self):
        clinic = _specimens_by_kind(FAMILIES["clinic_visits"], "feasibility")
        missing_status = clinic[specimen_name("clinic_visits", "feasibility-missing-status")]
        self.assertNotIn("- status (", missing_status.task.solver_prompt.lower())
        self.assertIn("status = 'attended'", missing_status.task.solver_prompt)
        no_table = clinic[specimen_name("clinic_visits", "feasibility-missing-procedures")]
        self.assertNotIn("- procedures (", no_table.task.solver_prompt)
        self.assertNotIn("### procedures", council_mod._critic_view(no_table.task))
        self.assertIn("grouped by visit_id", no_table.task.solver_prompt)
        stock = _specimens_by_kind(FAMILIES["stock_ledger"], "feasibility")
        missing_type = stock[specimen_name("stock_ledger", "feasibility-missing-move-type")]
        self.assertNotIn("- move_type (", missing_type.task.solver_prompt)
        self.assertIn("move_type = 'inbound'", missing_type.task.solver_prompt)
        for name, family in _new_families():
            for anchor in family.anchors_for(CouncilRole.FEASIBILITY_REVIEWER):
                if anchor.kind != "prose_reference":
                    continue
                column = anchor.target.split(".", 1)[1]
                specimens = [
                    s for s in family.specimen_builders["feasibility"]()
                    if column in s.anchor_terms and "unpublished" in s.name
                ]
                self.assertTrue(specimens, anchor.target)
                for s in specimens:
                    unpublished = s.anchor_terms[0]
                    self.assertNotIn(unpublished, s.task.solver_prompt, s.name)
                    self.assertIn(unpublished, council_mod._critic_view(s.task), s.name)
                    self.assertNotIn(
                        unpublished, {c.name for t in s.task.tables for c in t.columns}
                    )

    def test_dropping_a_column_unpublishes_its_keys_and_relationships_too(self):
        base = FAMILIES["clinic_visits"].task()
        task = metrology_mod._drop_column(base, "practitioners", "clinic_id")
        spec = task.table("practitioners")
        self.assertNotIn("clinic_id", [c.name for c in spec.columns])
        for rel in task.relationships:
            self.assertFalse(rel.child_table == "practitioners" and "clinic_id" in rel.child_columns)
        view = council_mod._critic_view(task)
        block = view.split("PUBLIC SOURCE SCHEMAS", 1)[1].split("MART OUTPUT SCHEMAS", 1)[0]
        table_block = block.split("### practitioners", 1)[1].split("###", 1)[0]
        self.assertNotIn("clinic_id", table_block)
        TaskIR.model_validate(task.model_dump())
        self.assertIn("practitioners(clinic_id) -> clinics(clinic_id)", council_mod._critic_view(base))

    def test_a_tamper_that_leaves_an_invalid_ir_raises_instead_of_shipping(self):
        for name, family in _new_families():
            clean = family.task()
            with self.assertRaises(ValueError):
                metrology_mod._drop_column(clean, clean.tables[0].name, "no_such_column")
            with self.assertRaises(ValueError):
                metrology_mod._drop_table(clean, "no_such_table")
            with self.assertRaises(ValueError):
                metrology_mod._rewrite_mart(clean, grain=("no such grain text", ""))
            with self.assertRaises(ValueError):
                metrology_mod._describe_one_mart_column(clean, "no_such_column", " x")

    def test_surface_variants_change_the_bytes_and_nothing_else(self):
        for name, family in _new_families():
            for specimen in family.specimens():
                role = specimen.target_role or CouncilRole.AMBIGUITY_CRITIC
                views = [render_view(role, task) for task in specimen.variants]
                self.assertEqual(len(set(views)), len(views), specimen.name)
                hashes = {
                    task.model_copy(update={"solver_prompt": ""}).content_hash()
                    for task in specimen.variants
                }
                self.assertEqual(len(hashes), 1, specimen.name)

    def test_no_family_embeds_private_material_in_its_public_view(self):
        for name, family in FAMILIES.items():
            task = family.task()
            private = [task.reference.sql_by_mart[m.name] for m in task.marts] + [
                c.mutation for c in task.attack_cases if not c.mutation.startswith("directive:")
            ]
            for specimen in family.specimens():
                for role in CRITIC_ROLES:
                    view = render_view(role, specimen.task)
                    with self.subTest(family=name, specimen=specimen.name, role=role.value):
                        self.assertNotIn(family.canary_guid, view)
                        self.assertNotIn(metrology_mod.METROLOGY_CANARY, view)
                        self.assertNotIn("literal_rows", view)
                        for sql in private:
                            self.assertNotIn(" ".join(sql.split()), " ".join(view.split()))
                        for pop in specimen.task.populations:
                            for rows in pop.literal_rows.values():
                                self.assertNotIn(json.dumps(list(rows)), view)


# ---------------------------------------------------------------------------
# The roadmap's Pool tests, on every family
# ---------------------------------------------------------------------------


class FamilyPoolRoadmapTest(unittest.TestCase):
    def test_every_specimen_is_leak_free_and_target_view_is_distinct(self):
        for name, family in FAMILIES.items():
            specimens = family.specimens()
            cleans = [s for s in specimens if s.kind == "clean"]
            for specimen in specimens:
                with self.subTest(family=name, specimen=specimen.name):
                    for task in specimen.variants:
                        self.assertEqual(leak_findings(task), [], specimen.name)
                    if specimen.kind != "tampered":
                        continue
                    tampered_view = render_view(specimen.target_role, specimen.task)
                    for clean in cleans:
                        self.assertNotEqual(
                            tampered_view,
                            render_view(specimen.target_role, clean.task),
                            f"{specimen.name}: defect invisible to its target role",
                        )

    def test_a_diligent_reader_detects_every_variant_of_every_specimen(self):
        """Not just variant 0: a wording rotation must not dissolve a defect,
        on ANY family, and the reader raises no false alarm on any clean."""
        for name, family in FAMILIES.items():
            every_variant = tuple(
                metrology_mod.Trial(
                    specimen=s,
                    variant=v,
                    replicate=v,
                    roles=tuple(CRITIC_ROLES) if s.kind == "clean" else (s.target_role,),
                )
                for s in family.specimens()
                for v in range(metrology_mod.PROSE_VARIANTS)
            )
            report = metrology_mod.run_metrology(oracle_for(name), trials=every_variant)
            with self.subTest(family=name):
                missed = [
                    (s.name, s.variant)
                    for s in report.specimens
                    if s.kind == "tampered" and not s.detected
                ]
                self.assertEqual(missed, [])
                noisy = [
                    s.name
                    for s in report.specimens
                    if s.kind == "clean"
                    and set(s.findings_by_role) - {CouncilRole.SHORTCUT_ATTACKER.value}
                ]
                self.assertEqual(noisy, [])
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    self.assertEqual(metrics.false_alarms, 0, role.value)
                    self.assertEqual(metrics.recall, 1.0, role.value)
                    self.assertGreaterEqual(
                        metrics.distinct_specimens,
                        metrology_mod.TAMPERED_PER_ROLE + metrology_mod.POOL_HOLDOUT,
                    )


# ---------------------------------------------------------------------------
# Frozen
# ---------------------------------------------------------------------------


#: Per-family content digests. Re-pin only after an intentional family change;
#: doing so also invalidates pool and admission identity.
FAMILY_DIGESTS = {
    "demo": "15d80d88aeb44f8c92179265e2864f877d2c3232674746bd858851da692882cc",
    "clinic_visits": "f5f549598a0b3f2670a7bfc5cb68c720694fe58aafb154a1a460c6179fef66d5",
    "stock_ledger": "a642f92c197580405e0ec9bff9295cb766f5ebda624a9c48b862bad39ac0764e",
}

#: The clean task of every family, by content hash.
FAMILY_TASK_HASHES = {
    "demo": "c2e0744cfc85657f6b1680600cc35ff9bfaf67a8548b21d9d8c57ea65d56ee44",
    "clinic_visits": "546925d53e961d5dd83e1459e55511b3934aeab7a942f8f76c4ef9bab23304fb",
    "stock_ledger": "2ecb7728f1d0d9af498805f2f76e58311ad92fad6691caa37dad0d8ac3765d84",
}


class FamilyFrozenTest(unittest.TestCase):
    def test_family_task_content_hashes_are_pinned(self):
        got = {name: FAMILIES[name].task().content_hash() for name in FAMILY_NAMES}
        self.assertEqual(got, FAMILY_TASK_HASHES)

    def test_family_digests_are_pinned(self):
        got = {name: family_digest(FAMILIES[name]) for name in FAMILY_NAMES}
        self.assertEqual(got, FAMILY_DIGESTS)

    def test_family_digest_moves_when_a_family_changes(self):
        family = FAMILIES["stock_ledger"]
        edited = FixtureFamily(
            name=family.name,
            task=family.task,
            anchors=family.anchors,
            specimen_builders=family.specimen_builders,
            canary_guid=family.canary_guid + "-edited",
        )
        self.assertNotEqual(family_digest(edited), family_digest(family))


if __name__ == "__main__":
    unittest.main()

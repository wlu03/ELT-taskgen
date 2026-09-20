"""THE DESCRIPTION CONTRACT: every builder-emitted MartOp.description must be
a rule a compliant author can restate and pass the prose gates with.

WHY THIS FILE EXISTS
`review/prose_fidelity.py` turns each MartOp.description into a CHECKABLE
demand on the authored solver prose: every identifier-like token (contains
'_') must appear VERBATIM in one two-sentence passage, at least
RULE_TERM_COVERAGE of the remaining content words must appear (with
`_DECLARATIVE_EQUIVALENTS` as the only loosening), and no passage may name a
term that CONTRADICTS one the rule states (`_EXCLUSIVE_TERM_GROUPS`).
Meanwhile `review/declarative_prose.py` BANS relational-operator vocabulary
in that same prose. The description is therefore not documentation — it is
the source text of a contract two gates enforce against a third party (the
author), and a description written as ENGINEER documentation poisons that
contract three ways, all measured on the live-authored task at
/tmp/run-synsql (synsql__financial_stock_market_data_analysis_387890__
stocks_stock_prices_top):

  (a) PLAN-INTERNAL ALIASES ('top_measure = MAX("f_measure")'): the checker
      demands 'f_measure' verbatim in prose, but 'f_measure' names nothing
      the solver can see — an author naming it writes spec-noise, an author
      naming the REAL column fails the gate.
  (b) CAUTIONARY OPERATOR CLAUSES ('LEFT JOIN ... RETAINED; an INNER join at
      this hop silently drops them'): the warning injects 'inner' into the
      rule's wanted exclusive terms, so every candidate passage must now
      assert BOTH members of a mutually-exclusive pair — and 'inner' has no
      declarative equivalent, so the only way to satisfy it is to write the
      wrong implementation into the spec.
  (c) META-PREAMBLES ('CASE ladders (every one carries an ELSE, ...)'):
      constant boilerplate about the plan machinery inflates the term set the
      75% coverage is measured over, so outcome-focused prose fails on words
      that describe the BUILDER, not the rule.

THE CONTRACT, one test per clause, over every plan the pool builders can
produce (the five library shapes driven over the synthetic schema
`test_plan_library` proves execution on, evidence variants, the legacy
`build_star` path via the WikiDBs adapter fixture, and the library rebuilt
over real on-disk synsql pool schemas):

  1. SAYABLE IDENTIFIERS — every '_'-bearing token of a description is part
     of the task's PUBLIC contract: a table name, a table column, a declared
     enum value, the mart name, a mart/key column, or an identifier token the
     mart's own shipped text (grain, column descriptions) already carries.
     The sayable set is computed from the TaskIR/MartSpec, never hand-listed.
  2. NO UNSATISFIABLE EXCLUSIVES — for each group in
     `prose_fidelity._EXCLUSIVE_TERM_GROUPS` (imported, not copied), a
     description names AT MOST ONE member: the one the rule wants.
  3. NO BANNED OPERATOR VOCABULARY — each description passes
     `declarative_prose.operator_problems` (the same lexicon the authored
     prose is judged by; it is a public pure function over (text,
     identifiers), so descriptions are scanned with the identical rules the
     prose faces). NO NARROWING was needed: the lexicon already permits exact
     predicate values ("status = 'completed'" never fires) and plain English
     that collides with SQL keywords, so anything it flags in a description
     is vocabulary the author could not echo even if they wanted to.
  4. SATISFIABILITY SMOKE — a mechanically-derived compliant prose (one
     sentence per rule, built from the description's own significant terms
     with declarative equivalents substituted and unsayable-in-any-context
     words dropped) passes `check_prose_fidelity` for the whole task,
     proving a diligent author CAN pass the joint gate.

Defect (c) is covered indirectly: its boilerplate is caught where it uses
banned vocabulary (clause 3) or plan-internal aliases (clause 1), and clause
4 keeps whatever remains satisfiable. Pure filler that is sayable English is
a quality problem the description sweep removes but no deterministic check
can define without a hand-list.

WHAT IS DELIBERATELY OUT OF SCOPE
  * `demo_fixture.demo_mart_plan()` — hand-written, hash-pinned by
    tests/test_models_round3.py, and already proven jointly satisfiable by
    tests/fixtures/declarative_prose.txt.
  * MartColumn descriptions — they are checked by prose_fidelity at
    COLUMN_DESC_COVERAGE with no exclusive-term or verbatim-identifier
    demands, so they cannot make the contract unsatisfiable the way op
    descriptions can (their register is a separate concern).

This file was written to FAIL against the then-current builders; its
failure list, grouped by builder, was the inventory the description sweep
worked from.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from elt_taskgen.adapters import evidence as ev
from elt_taskgen.models import MartSpec, TaskIR
from elt_taskgen.review import declarative_prose, prose_fidelity

# Sibling-module import works under `discover -s tests` (which puts tests/
# on sys.path) AND under `unittest tests.test_plan_descriptions` (which does
# not) — both invocation styles are used in this repo.
try:
    import test_plan_library as tpl
except ModuleNotFoundError:  # package-style invocation
    from tests import test_plan_library as tpl

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The live-authored synsql workspace this contract was originally measured
#: on (/tmp/run-synsql). READ ONLY; long gone on most machines, kept as a
#: bonus source when it happens to exist.
RUN_SYNSQL_TASK_IR = Path(
    "/tmp/run-synsql/tasks/"
    "synsql__financial_stock_market_data_analysis_387890__"
    "stocks_stock_prices_top/task_ir.json"
)

#: Real on-disk synsql pool schemas. Was ws-difficulty-synsql/ until that
#: tree was pruned; the canonical synsql drive under runs/ now
#: carries the pool's frozen IR. The BUILDERS are driven over these schemas
#: fresh — the frozen task files' own descriptions are history, not the
#: contract.
WS_SYNSQL_SAMPLES = sorted(
    (REPO_ROOT / "runs").glob("synsql*/tasks/*/task_ir.json")
)[:3]


# ---------------------------------------------------------------------------
# Case construction: every plan a pool builder can produce
# ---------------------------------------------------------------------------

def _library_cases() -> list[tuple[str, TaskIR]]:
    """The five shapes over the synthetic schema, plus the dedupe variant
    (bridge with no upstream PK) that makes build_rollup emit a DEDUPE op."""
    cases: list[tuple[str, TaskIR]] = []
    for name, builder in tpl.SHAPES:
        built = builder(tpl.EVIDENCE, mart=f"{name}_mart")
        cases.append((f"library::{name}", tpl.build_task(built, f"proof__{name}")))
    dedupe_evidence = replace(tpl.EVIDENCE, bridge_needs_dedupe=True)
    # Adapters set bridge_needs_dedupe = not bridge.primary_key
    # (adapters/evidence.py), so the dedupe fixture drops the bridge PK to
    # match — a dedupe claim over a PK-bearing bridge is a contradiction the
    # witness planter refuses (see build_task's docstring).
    dedupe_tables = tuple(
        t.model_copy(update={"primary_key": ()})
        if t.name == tpl.EVIDENCE.bridge
        else t
        for t in tpl.TABLES
    )
    for name, builder in tpl.SHAPES:
        if name not in ("fan_out_rollup", "categorical_ladder"):
            continue
        built = builder(dedupe_evidence, mart=f"{name}_dedupe_mart")
        cases.append(
            (
                f"library-dedupe::{name}",
                tpl.build_task(built, f"proof__{name}_dd", tables=dedupe_tables),
            )
        )
    # The countable-key FALLBACK (bridge_key == bridge_parent_fk, the dlt
    # transformer-child case): argmax without top_row_id, prose without the
    # row-identifier clauses.
    fk_key = replace(tpl.EVIDENCE, bridge_key=tpl.EVIDENCE.bridge_parent_fk)
    built = tpl.mp.argmax_profile(fk_key, mart="argmax_profile_fk_mart")
    cases.append(
        ("library-fk-key::argmax_profile", tpl.build_task(built, "proof__argmax_fk"))
    )
    return cases


def _rebuilt_pool_case(path: Path, label: str) -> tuple[str, TaskIR] | None:
    """The library builders driven over a real pool schema, exactly the way
    the adapters drive them (chain_candidates -> build_marts)."""
    base = TaskIR.model_validate_json(path.read_text(encoding="utf-8"))
    candidates = ev.chain_candidates(base.tables, base.relationships)
    marts, _shapes, names = ev.build_marts(
        candidates,
        prefix=lambda e, suffix: f"{e.parent}_{e.bridge}_{suffix}",
        max_marts=2,
    )
    if not marts:
        return None
    task = base.model_copy(
        update={
            "marts": marts,
            "solver_prompt": "",
            "reference": None,
            "attack_cases": (),
            "populations": (),
        }
    )
    return (f"{label}::{'+'.join(names)}", task)


def _wikidbs_fixture_case() -> tuple[str, TaskIR]:
    """The WikiDBs adapter fixture other suites already use. Its tiny schema
    funds no library shape, so `to_task_ir` takes the legacy `build_star`
    fallback — which keeps build_star's emitted descriptions inside this
    contract (it is a live pool builder, whatever its age)."""
    try:
        import test_adapters_wikidbs as taw
    except ModuleNotFoundError:  # package-style invocation
        from tests import test_adapters_wikidbs as taw
    from elt_taskgen.adapters import wikidbs

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = taw._write_db(root)
        map_path = taw._fixture_map(root)
        task = wikidbs.to_task_ir(db, family_map_path=map_path)
    return ("pool::wikidbs::fixture", task)


def _all_cases() -> list[tuple[str, TaskIR]]:
    cases = _library_cases()
    cases.append(_wikidbs_fixture_case())
    if RUN_SYNSQL_TASK_IR.exists():
        case = _rebuilt_pool_case(RUN_SYNSQL_TASK_IR, "pool::synsql::run-synsql")
        if case is None:
            raise AssertionError(
                "the run-synsql schema stopped funding any library shape — "
                "the live measurement basis of this contract is gone"
            )
        cases.append(case)
    for path in WS_SYNSQL_SAMPLES:
        case = _rebuilt_pool_case(path, f"pool::synsql::{path.parts[-2][:48]}")
        if case is not None:
            cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# Clause helpers
# ---------------------------------------------------------------------------

def _identifier_terms(text: str) -> tuple[str, ...]:
    """The '_'-bearing tokens prose_fidelity will demand VERBATIM in prose —
    derived with the checker's own tokenizer so the two can never disagree."""
    return tuple(t for t in prose_fidelity._significant_terms(text) if "_" in t)


def sayable_identifiers(task: TaskIR, mart: MartSpec) -> frozenset[str]:
    """Every identifier-like token the task's PUBLIC contract carries.

    Computed from the TaskIR/MartSpec, never hand-listed: table names, table
    columns, declared enum values, the mart name, mart columns, key columns,
    and the identifier tokens of the mart's own shipped text (grain and
    column descriptions — prose_fidelity independently forces those into the
    prose, so an op description reusing them adds nothing unsayable). A
    plan-internal alias appears in none of these, which is the point.
    """
    names: set[str] = set()
    for table in task.tables:
        names.add(table.name.lower())
        for column in table.columns:
            names.add(column.name.lower())
            for value in column.enum_values or ():
                if "_" in value:
                    names.add(value.lower())
    names.add(mart.name.lower())
    names.update(k.lower() for k in mart.key_columns)
    names.update(_identifier_terms(mart.grain))
    for column in mart.columns:
        names.add(column.name.lower())
        names.update(_identifier_terms(column.description))
    return frozenset(names)


def exclusive_conflicts(description: str) -> list[list[str]]:
    """Groups of `_EXCLUSIVE_TERM_GROUPS` this description names >1 member of."""
    terms = set(prose_fidelity._significant_terms(description))
    conflicts: list[list[str]] = []
    for group in prose_fidelity._EXCLUSIVE_TERM_GROUPS:
        named = sorted(terms & group)
        if len(named) > 1:
            conflicts.append(named)
    return conflicts


def _spoken(term: str, identifiers: frozenset[str]) -> str | None:
    """The word a compliant author would write to satisfy `term`.

    The declarative-equivalent (imported from prose_fidelity) when one
    exists; the term itself when the declarative lexicon permits it in
    isolation; None when the word is banned in EVERY context (e.g.
    'coalesce') and has no outcome word — such a term can only be paid for
    out of the 25% coverage slack, and the smoke test measures whether the
    slack suffices.
    """
    equivalents = prose_fidelity._DECLARATIVE_EQUIVALENTS.get(term)
    if equivalents:
        return sorted(equivalents)[0]
    if declarative_prose.operator_problems(term, identifiers):
        return None
    return term


def compliant_prose(task: TaskIR) -> str:
    """A mechanically-derived prose a diligent author could have written.

    One source/backend passage per source, one endpoint/key/requiredness
    passage per relationship, then one clearly labelled section per mart.
    Each mart section carries its description in the declaration and one
    passage per grain, output column, and plan rule.  Rule words are built
    with `_spoken`; grain terms are emitted literally because the gate admits
    no general equivalents there. Deterministic pure function of the task.
    """
    identifiers = declarative_prose._task_identifiers(task)
    lines: list[str] = []
    for table in task.tables:
        backend = task.backend_for(table.name).backend.value
        lines.append(f"- Source table {table.name} uses backend {backend}.")
    for relationship in task.relationships:
        requiredness = "required" if relationship.required else "optional"
        child = ", ".join(relationship.child_columns)
        parent = ", ".join(relationship.parent_columns)
        lines.append(
            f"- Relationship {relationship.child_table} ({child}) to "
            f"{relationship.parent_table} ({parent}) is {requiredness}."
        )
    if lines:
        lines.append("")
    source_tables = frozenset(table.name.lower() for table in task.tables)
    source_columns = frozenset(
        column.name.lower() for table in task.tables for column in table.columns
    )
    for mart in task.marts:
        lines.append(f"## Mart {mart.name}: {mart.description}")
        grain_words = [*prose_fidelity._significant_terms(mart.grain)]
        grain_words += [k for k in mart.key_columns if k not in grain_words]
        lines.append("- " + " ".join(grain_words) + ".")
        for column in mart.columns:
            words = [column.name]
            for term in prose_fidelity._significant_terms(column.description):
                said = _spoken(term, identifiers)
                if said is not None and said not in words:
                    words.append(said)
            lines.append("- " + " ".join(words) + ".")
        public_columns = source_columns | frozenset(
            column.name.lower() for column in mart.columns
        )
        for op in mart.plan.ops:
            words: list[str] = []
            for term in prose_fidelity._significant_terms(op.description):
                said = _spoken(term, identifiers)
                if said is not None and said not in words:
                    words.append(said)
            exact, semantic, _literals = prose_fidelity._structured_op_requirements(
                op,
                source_tables=source_tables,
                public_columns=public_columns,
            )
            for term in exact:
                if term not in words:
                    words.append(term)
            for term in semantic:
                said = _spoken(term, identifiers)
                if said is not None and said not in words:
                    words.append(said)
            lines.append("- " + " ".join(words) + ".")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

class PlanDescriptionContract(unittest.TestCase):
    """Every builder-emitted op description must be author-satisfiable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = _all_cases()

    # -- the corpus itself ---------------------------------------------------

    def test_the_corpus_covers_every_pool_builder(self) -> None:
        """All five library shapes, the legacy build_star path, and at least
        one real pool schema — a contract proven over a subset is not the
        contract."""
        labels = [label for label, _ in self.cases]
        for shape, _ in tpl.SHAPES:
            self.assertTrue(
                any(shape in label for label in labels),
                f"shape {shape!r} fell out of the description contract: {labels}",
            )
        self.assertIn("pool::wikidbs::fixture", labels)
        if not any(label.startswith("pool::synsql::") for label in labels) and not (
            RUN_SYNSQL_TASK_IR.exists() or WS_SYNSQL_SAMPLES
        ):
            # The real synsql pool schemas live under runs/, which an operator
            # may have cleared; without them the pool half of the contract is
            # unmeasurable here, not violated (the library half still ran).
            self.skipTest("no synsql drive under runs/: pool schemas not on disk")
        self.assertTrue(
            any(label.startswith("pool::synsql::") for label in labels),
            f"no real synsql pool schema in the corpus: {labels}",
        )

    # -- clause 1: sayable identifiers ---------------------------------------

    def test_every_description_identifier_is_sayable(self) -> None:
        """prose_fidelity demands each '_'-bearing description token VERBATIM
        in the authored prose; a token outside the task's public contract
        (a plan-internal alias like 'f_measure' or 'part_max') makes the
        author choose between spec-noise and a red gate."""
        for label, task in self.cases:
            for mart in task.marts:
                sayable = sayable_identifiers(task, mart)
                for index, op in enumerate(mart.plan.ops):
                    with self.subTest(case=label, mart=mart.name, op=index,
                                      kind=op.kind.value):
                        unsayable = sorted(
                            t for t in _identifier_terms(op.description)
                            if t not in sayable
                        )
                        self.assertEqual(
                            [], unsayable,
                            f"op[{index}] ({op.kind.value}) description demands "
                            f"plan-internal identifiers {unsayable} verbatim in "
                            f"prose: {op.description!r}",
                        )

    def test_solver_projection_withholds_every_internal_tie_break_alias(self) -> None:
        """Compiler aliases remain private while real tie semantics survive."""

        seen = 0
        for label, task in self.cases:
            for mart in task.marts:
                public = {
                    *(
                        column.name.casefold()
                        for table in task.tables
                        for column in table.columns
                    ),
                    *(column.name.casefold() for column in mart.columns),
                    *(table.name.casefold() for table in task.tables),
                }
                projected = tpl.mp.solver_safe_plan_requirements(task, mart)
                rendered = repr(projected).casefold()
                for op in mart.plan.ops:
                    tie_break = op.details.get("tie_break", "").strip()
                    if tie_break and tie_break.casefold() not in public:
                        seen += 1
                        with self.subTest(case=label, mart=mart.name, alias=tie_break):
                            self.assertNotIn(tie_break.casefold(), rendered)
                            self.assertIn(op.description, repr(projected))
        self.assertGreater(seen, 0, "the builder corpus lost its internal tie aliases")

    # -- clause 2: no unsatisfiable exclusives -------------------------------

    def test_no_description_names_two_members_of_an_exclusive_group(self) -> None:
        """A description naming two members of one _EXCLUSIVE_TERM_GROUPS
        group (e.g. 'left' AND 'inner') forces every candidate passage to
        assert both sides of a mutual exclusion; the wrong-implementation
        warning belongs in a code comment at the builder, not in checkable
        text."""
        for label, task in self.cases:
            for mart in task.marts:
                for index, op in enumerate(mart.plan.ops):
                    with self.subTest(case=label, mart=mart.name, op=index,
                                      kind=op.kind.value):
                        conflicts = exclusive_conflicts(op.description)
                        self.assertEqual(
                            [], conflicts,
                            f"op[{index}] ({op.kind.value}) description names "
                            f"multiple members of exclusive group(s) {conflicts}: "
                            f"{op.description!r}",
                        )

    # -- clause 3: no banned operator vocabulary -----------------------------

    def test_no_description_uses_banned_operator_vocabulary(self) -> None:
        """Scanned with declarative_prose.operator_problems — the identical
        lexicon the authored prose faces — because a description term the
        author is FORBIDDEN to echo is a term that can only ever count
        against the coverage threshold."""
        for label, task in self.cases:
            identifiers = declarative_prose._task_identifiers(task)
            for mart in task.marts:
                for index, op in enumerate(mart.plan.ops):
                    with self.subTest(case=label, mart=mart.name, op=index,
                                      kind=op.kind.value):
                        problems = declarative_prose.operator_problems(
                            op.description, identifiers
                        )
                        self.assertEqual(
                            [], problems,
                            f"op[{index}] ({op.kind.value}) description uses "
                            f"banned operator vocabulary: {problems}",
                        )

    def test_no_mart_or_column_description_uses_banned_operator_vocabulary(self) -> None:
        """The same scan over the mart and column descriptions. An author who
        copies a column description word for word must not inherit a
        rejection: 'Number of DISTINCT linked <bridge> rows whose ...' and
        'over DISTINCT links whose ...' on a deduplicated fan_out_rollup did
        exactly that (wikidbs__c40096, batch50 2026-09-19)."""
        for label, task in self.cases:
            identifiers = declarative_prose._task_identifiers(task)
            for mart in task.marts:
                texts = [("mart", mart.description)] + [
                    (column.name, column.description) for column in mart.columns
                ]
                for item, text in texts:
                    with self.subTest(case=label, mart=mart.name, item=item):
                        self.assertEqual(
                            [], declarative_prose.operator_problems(text, identifiers)
                        )

    # -- clause 3b: an argmax only claims a row identifier it ships ---------

    def test_which_row_won_is_claimed_only_with_a_row_identifier(self) -> None:
        """N-mart_plan-1: 'identifies WHICH row won' was shipped for a column
        that equalled parent_key on every row. The clause, and the 'then the
        smallest <key>' tie-break clause, may appear only when top_row_id is
        a column of the mart."""
        seen_with, seen_without = 0, 0
        for label, task in self.cases:
            for mart in task.marts:
                if not any(op.kind.value == "extrema" for op in mart.plan.ops):
                    continue
                # The mart names its columns after its own chain, so the
                # row identifier is found where the plan carries it: the
                # extrema op projects the winning row's id alias.
                has_row_id = any(
                    '"f_id" AS' in (op.details.get("select") or "")
                    for op in mart.plan.ops
                    if op.kind.value == "extrema"
                )
                texts = [c.description for c in mart.columns] + [
                    op.description for op in mart.plan.ops
                ]
                claims = [
                    t for t in texts if "WHICH row won" in t or "then the smallest" in t
                ]
                with self.subTest(case=label, mart=mart.name):
                    if has_row_id:
                        seen_with += 1
                    else:
                        seen_without += 1
                        self.assertEqual([], claims)
        self.assertGreater(seen_with, 0)
        self.assertGreater(seen_without, 0, "the fk-key argmax case fell out of the corpus")

    # -- clause 4: satisfiability smoke --------------------------------------

    def test_a_mechanically_compliant_prose_passes_the_joint_gate(self) -> None:
        """The executable proof that a diligent author CAN pass: prose built
        from nothing but the descriptions themselves (equivalents
        substituted, unsayable words dropped) must clear check_prose_fidelity
        — completeness AND declarativeness — for the whole task."""
        for label, task in self.cases:
            with self.subTest(case=label):
                prose = compliant_prose(task)
                probe = task.model_copy(update={"solver_prompt": prose})
                problems = prose_fidelity.check_prose_fidelity(probe)
                self.assertEqual(
                    [], problems,
                    "no compliant author output can satisfy these "
                    f"descriptions; the mechanical best-effort fails with: "
                    f"{problems[:6]}",
                )

    def test_the_compliant_prose_builder_is_deterministic(self) -> None:
        for label, task in self.cases:
            with self.subTest(case=label):
                self.assertEqual(compliant_prose(task), compliant_prose(task))


    def test_cohort_column_states_the_missing_status_case(self):
        """batch50 2026-09-10: each cohort is selected by `link_status IN
        (...)`, which a missing value never satisfies, and 'no_activity' is
        `link_key IS NULL` — no linked row at all. So a linked row whose
        status has no value falls in NO cohort and is counted nowhere. The
        description named only the three branches, leaving that unstated on
        14 of the 15 cohort marts in the batch (their status column is
        nullable), and the ambiguity critic correctly filed it as a fork."""
        import inspect

        from elt_taskgen.generation import mart_plan as mart_plan_mod

        source = inspect.getsource(mart_plan_mod.status_cohort_union)
        self.assertIn("belongs to no cohort", source)
        self.assertIn("not counted in any cell", source)
        # and it must not claim the row makes the parent 'no_activity'
        self.assertIn("does not make", source)

    def test_a_parent_carried_measure_is_not_called_a_summary_of_matches(self):
        """batch50 2026-09-11: a measure over a column carried from the PARENT
        is constant inside its group, so the aggregate returns the parent
        row's own value and returns it whether or not that row matched
        anything. The AGGREGATE description lumped it in with the real
        roll-ups ("reporting deals_flow_count, last_update_time for that
        row's matching rows"), which tells a solver to read the value off the
        joined table and to report nothing for an unmatched parent row. The
        ambiguity critic filed it as a fork on dlt__pipedrive and
        dlt__personio, the two tasks in the pool with a parent cursor."""
        from elt_taskgen.generation.mart_plan import (
            MartOpKind,
            Measure,
            StarJoin,
            build_star,
        )
        from elt_taskgen.models import ColumnType

        join = StarJoin(
            table="deals_flow",
            on_pairs=(("id", "_deals_id"),),
            carry=(("_deals_id", "deals_flow___deals_id"),),
            rel_columns=("_deals_id",),
        )
        built = build_star(
            mart="dim_deals",
            parent="deals",
            parent_keys=("id",),
            key_columns=("id",),
            parent_carry=(("update_time", "deals__update_time"),),
            joins=(join,),
            measures=(
                Measure(column="deals_flow_count",
                        expr='COUNT("deals_flow___deals_id")'),
                Measure(column="last_update_time",
                        expr='MAX("deals__update_time")',
                        type=ColumnType.TIMESTAMP),
            ),
        )
        agg = next(op for op in built.plan.ops if op.kind is MartOpKind.AGGREGATE)
        # The roll-up over the joined rows keeps the matching-rows wording ...
        self.assertIn(
            "reporting deals_flow_count for that row's matching rows",
            agg.description,
        )
        # ... and the parent-carried one is stated apart, with its source
        # column named and the no-match case settled.
        self.assertIn("last_update_time is taken from the deals side only",
                      agg.description)
        self.assertIn("never from the matching rows", agg.description)
        self.assertIn("A deals row with no matching rows still reports",
                      agg.description)
        # The measure names must never share one "for that row's matching
        # rows" clause again.
        self.assertNotIn("deals_flow_count, last_update_time", agg.description)
        # The SQL is untouched: this is a description-only distinction.
        self.assertEqual(agg.details["m_1"], 'MAX("deals__update_time")')

    def test_a_mart_with_no_parent_carry_keeps_the_original_wording(self):
        """The split must not disturb the 146 marts of the pool that carry
        nothing from the parent: their AGGREGATE sentence is unchanged."""
        from elt_taskgen.generation.mart_plan import (
            MartOpKind,
            Measure,
            StarJoin,
            build_star,
        )

        built = build_star(
            mart="fct_orders",
            parent="customers",
            parent_keys=("id",),
            key_columns=("id",),
            joins=(StarJoin(table="orders", on_pairs=(("id", "customer_id"),),
                            carry=(("amount", "orders__amount"),),
                            rel_columns=("customer_id",)),),
            measures=(
                Measure(column="order_count", expr='COUNT("orders__amount")'),
                Measure(column="total_amount", expr='SUM("orders__amount")',
                        null_default="0"),
            ),
        )
        agg = next(op for op in built.plan.ops if op.kind is MartOpKind.AGGREGATE)
        self.assertEqual(
            agg.description,
            "One output row per id, reporting order_count, total_amount "
            "for that row's matching rows.",
        )

    def test_a_measure_touching_both_sides_stays_a_roll_up(self):
        """A measure whose expression names a joined alias as well as a
        parent-carried one DOES depend on the matching rows, so it keeps the
        matching-rows wording; so does a measure that names no column at
        all (COUNT(*))."""
        from elt_taskgen.generation.mart_plan import (
            MartOpKind,
            Measure,
            StarJoin,
            build_star,
        )

        built = build_star(
            mart="fct_lag",
            parent="deals",
            parent_keys=("id",),
            key_columns=("id",),
            parent_carry=(("update_time", "deals__update_time"),),
            joins=(StarJoin(table="deals_flow", on_pairs=(("id", "_deals_id"),),
                            carry=(("log_time", "deals_flow__log_time"),),
                            rel_columns=("_deals_id",)),),
            measures=(
                Measure(column="row_count", expr="COUNT(*)"),
                Measure(
                    column="max_lag_seconds",
                    expr='MAX(DATE_DIFF(\'second\', "deals__update_time", '
                         '"deals_flow__log_time"))',
                ),
            ),
        )
        agg = next(op for op in built.plan.ops if op.kind is MartOpKind.AGGREGATE)
        self.assertEqual(
            agg.description,
            "One output row per id, reporting row_count, max_lag_seconds "
            "for that row's matching rows.",
        )
        self.assertNotIn("taken from the deals side only", agg.description)


class BoundaryAndDefaultStatements(unittest.TestCase):
    """batch10 2026-09-11: three generator sentences the ambiguity critic
    blocked tasks on, each now derived from the SQL it describes."""

    def test_the_boundary_statement_follows_each_ladder_comparison(self):
        """One shared sentence said "INCLUSIVE of the lower band" for every
        threshold of every ladder in a builder. categorical_ladder compares
        `>= 0.8`, which puts 0.8 in the HIGHER band; the author restated both
        halves and the critic blocked synsql__3d_motion_tracking on the
        contradiction. has_links and status_group compare against no
        threshold and carried the sentence anyway."""
        from elt_taskgen.generation.mart_plan import _ladder_boundary_statement as stmt

        # The band is NAMED, by evaluating the arms in order at the boundary
        # value the way the SQL does: "takes the LOWER band" beside "'small'
        # for 1-2 INCLUSIVE of 2" was still read as a tie-break overriding
        # the range (synsql__3d_object_positioning, run D).
        self.assertEqual(
            stmt("CASE WHEN {n} = 0 THEN 'none' WHEN {r} >= 0.8 THEN 'high' "
                 "WHEN {r} >= 0.5 THEN 'medium' ELSE 'low' END"),
            "a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'",
        )
        self.assertEqual(
            stmt("CASE WHEN {n} = 0 THEN 'none' WHEN {n} <= 2 THEN 'small' "
                 "WHEN {n} <= 5 THEN 'medium' ELSE 'large' END"),
            "a value exactly at 2 is 'small'; a value exactly at 5 is 'medium'",
        )
        # `> t` sends t to the ELSE arm; `< t` likewise.
        self.assertEqual(stmt("CASE WHEN {n} > 0 THEN 'yes' ELSE 'no' END"), "a value exactly at 0 is 'no'")
        self.assertEqual(stmt("CASE WHEN {n} < 3 THEN 'few' ELSE 'many' END"), "a value exactly at 3 is 'many'")
        # An earlier equality arm captures its own value first.
        self.assertEqual(
            stmt("CASE WHEN {n} = 0 THEN 'none' WHEN {n} >= 0 THEN 'some' ELSE 'x' END"),
            "a value exactly at 0 is 'none'",
        )
        # An equality test names one value, not a boundary between bands.
        self.assertEqual(
            stmt("CASE WHEN {s} = 'active' THEN 'active' ELSE 'other' END"),
            "categorical mapping; no numeric boundary",
        )

    def test_every_library_ladder_boundary_agrees_with_its_case(self):
        """The rendered `boundary` detail of every CONDITIONAL op in the
        library agrees with the comparison operator in that op's own CASE."""
        import re

        from elt_taskgen.generation import mart_plan as mp
        from test_plan_library import EVIDENCE, SHAPES

        checked = 0
        for name, builder in SHAPES:
            built = builder(EVIDENCE, mart=f"{name}_mart")
            for op in built.plan.ops:
                if op.kind is not mp.MartOpKind.CONDITIONAL:
                    continue
                column = op.columns[-1]
                select = (op.details or {}).get("select", "")
                case = re.search(r'(CASE .*?END) AS "' + re.escape(column) + '"', select)
                text = (op.details or {}).get("boundary", "")
                for value, band in re.findall(r"at ([0-9.]+) is '([^']*)'", text):
                    self.assertIsNotNone(case, f"{name}.{column}: no CASE in its select")
                    body = case.group(1)
                    arms = re.findall(r"WHEN\s+(.+?)\s+THEN\s+'([^']*)'", body)
                    default = re.search(r"ELSE\s+'([^']*)'", body)
                    v = float(value)
                    truth = default.group(1) if default else None
                    for condition, label in arms:
                        m = re.search(r"(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)", condition)
                        if m is None:
                            continue
                        op, lit = m.group(1), float(m.group(2))
                        if {">=": v >= lit, "<=": v <= lit, ">": v > lit, "<": v < lit, "=": v == lit}[op]:
                            truth = label
                            break
                    self.assertEqual(band, truth, f"{name}.{column} at {value}")
                    checked += 1
        self.assertGreaterEqual(checked, 6)

    def test_a_dangling_link_is_said_to_reach_no_child(self):
        """fan_out_rollup's join op says a dangling link "still counts as a
        link and is RETAINED"; distinct_child_count is over the CHILD rows, so
        such a link reaches none. Unstated, the critic read the two as a fork
        (synsql__3d_object_positioning)."""
        from elt_taskgen.generation import mart_plan as mp
        from test_plan_library import EVIDENCE

        built = mp.fan_out_rollup(EVIDENCE, mart="rollup")
        name = built.column_named("distinct_child_count")
        column = next(c for c in built.columns if c.name == name)
        if EVIDENCE.child_link_optional:
            self.assertIn("reaches no", column.description)
            self.assertIn("adds nothing to this count", column.description)
        else:
            self.assertNotIn("reaches no", column.description)

    def test_zero_substituted_measures_are_named_beside_the_undefaulted_ones(self):
        """twitter_ads: four conversion measures are SUM(COALESCE(x, 0)) in
        gold. The aggregate rule named only the outputs that PRESERVE an empty
        result and sent the reader to "its own declared column rule" for the
        rest, which did not exist."""
        from elt_taskgen.generation.mart_plan import Measure, _zero_substituted_inputs

        self.assertTrue(_zero_substituted_inputs(Measure(
            column="c", expr="SUM(COALESCE(COALESCE(CAST(c AS BIGINT), 0), 0))")))
        self.assertTrue(_zero_substituted_inputs(Measure(column="c", expr="SUM(COALESCE(c, 0.0))")))
        # A default around the WHOLE aggregate is null_default's promise.
        self.assertFalse(_zero_substituted_inputs(Measure(column="c", expr="COALESCE(SUM(c), 0)")))
        self.assertFalse(_zero_substituted_inputs(Measure(column="c", expr="SUM(c)")))
        # A substitute other than 0 is not "counts a missing value as 0".
        self.assertFalse(_zero_substituted_inputs(Measure(column="c", expr="SUM(COALESCE(c, 1))")))


class RenamedKeysAreStated(unittest.TestCase):
    """batch10 run E 2026-09-11, twitter_ads keyword_report: the key
    `keyword` is `segment` copied under another name, and "formed from source
    table line_item_keywords_report" never said which column, so the critic
    read two groupings. A key copied under another name names its source."""

    def test_build_star_names_the_source_column_of_a_renamed_key(self):
        from elt_taskgen.generation.mart_plan import MartOpKind, Measure, StarJoin, build_star

        built = build_star(
            mart="by_keyword", parent="reports", parent_keys=("segment", "account_id"),
            key_columns=("keyword", "account_id"),
            joins=(StarJoin(table="clicks", on_pairs=(("account_id", "account_id"),),
                            carry=(("n", "clicks__n"),), rel_columns=("account_id",)),),
            measures=(Measure(column="click_count", expr='COUNT("clicks__n")'),),
        )
        grain = next(o for o in built.plan.ops if o.kind is MartOpKind.DERIVE)
        self.assertIn("keyword is the value of the segment column of reports.", grain.description)
        self.assertNotIn("account_id is the value", grain.description)


class AbsenceTestsPublishNoLiteral(unittest.TestCase):
    """batch10 2026-09-11, synsql__3d_object_positioning: the placeholder
    filter `"link_key" IS NULL` was published to the author as the literal
    value "null". The fidelity gate then demanded the word in the author's
    rule, and the author pinned it to the wrong column ("the entity whose
    linked action_type is null"), contradicting the cohort column. An IS
    NULL test compares against no value; the description already states the
    absence in words."""

    def test_an_is_null_placeholder_filter_publishes_no_null_literal(self):
        from elt_taskgen.generation import mart_plan as mp
        from test_plan_library import EVIDENCE

        built = mp.status_cohort_union(EVIDENCE, mart="cohorts")
        task = tpl.build_task(built, "proof__cohorts")
        mart = task.marts[0]
        placeholder = next(
            op for op in mart.plan.ops
            if op.kind is mp.MartOpKind.FILTER and "IS NULL" in (op.predicate or "")
        )
        steps = mp.solver_safe_plan_requirements(task, mart)["steps"]
        step = steps[mart.plan.ops.index(placeholder)]
        self.assertEqual(step["description"], placeholder.description)
        literals = (step.get("condition") or {}).get("literal_values") or []
        self.assertNotIn("null", literals)
        # The absence is still stated, in words, where the author reads it.
        self.assertIn("no linked", placeholder.description)


class DescriptionsDoNotFeedExecution(unittest.TestCase):
    """The sweep's safety rail: descriptions are CONTRACT text, not compiler
    input, so rewriting them can never move the gold.

    Verified two ways during the sweep itself (compile_plan_sql output and
    attack_surface for all 13 corpus marts were byte-identical before/after);
    this test keeps the property executable: blanking EVERY description of a
    built plan changes neither the compiled SQL nor the attack surface.

    Library plans only, deliberately — build_star/legacy plans share the same
    compiler, and `attack_surface` documents a legacy TEXT-HINT route
    (_DENOMINATOR_HINTS / 'distinct' / _NULL_DEFAULT_HINTS over `_op_text`,
    which includes the description) that library plans never need because
    their structured details carry the same signal for the AST route.
    """

    def test_blanked_descriptions_compile_and_route_identically(self) -> None:
        from elt_taskgen.generation import mart_plan
        from elt_taskgen.reference import solution

        for label, task in _all_cases():
            if not label.startswith(("library", "pool::synsql")):
                continue
            blanked_marts = []
            for mart in task.marts:
                ops = tuple(
                    op.model_copy(update={"description": "x"})
                    for op in mart.plan.ops
                )
                blanked_marts.append(
                    mart.model_copy(
                        update={"plan": mart.plan.model_copy(update={"ops": ops})}
                    )
                )
            probe = task.model_copy(update={"marts": tuple(blanked_marts)})
            for mart, blank in zip(task.marts, probe.marts):
                with self.subTest(case=label, mart=mart.name):
                    self.assertEqual(
                        solution.compile_plan_sql(task, mart),
                        solution.compile_plan_sql(probe, blank),
                        "compile_plan_sql read an op description",
                    )
                    self.assertEqual(
                        mart_plan.attack_surface(mart.plan),
                        mart_plan.attack_surface(blank.plan),
                        "attack_surface routing depended on description text",
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RunKStatements(unittest.TestCase):
    """batch10 run K (2026-09-11): three generator sentences the ambiguity
    critic or the independent implementer read the wrong way."""

    def test_library_rollups_say_a_carried_column_never_splits_a_group(self):
        """lavestima and wikidbs__c20112: "source rows that agree on the key
        columns but differ in a carried column fall in different output rows"
        beside a grain of one row per parent key read as a second grain. A
        library passthrough is an attribute of the row the key identifies, so
        the shapes say so; a compiler-derived rollup (carried_per_key False)
        keeps the literal GROUP BY sentence."""
        from elt_taskgen.generation import mart_plan as mp
        from elt_taskgen.models import ColumnType, MartOpKind
        from test_plan_library import EVIDENCE, SHAPES

        checked = 0
        for name, builder in SHAPES:
            built = builder(EVIDENCE, mart=f"{name}_mart")
            aggs = [
                op for op in built.plan.ops
                if op.kind in (MartOpKind.AGGREGATE, MartOpKind.FILTERED_AGGREGATE)
            ]
            for op in aggs:
                if "carrying" not in op.description and "carried" not in op.description:
                    continue
                with self.subTest(shape=name):
                    self.assertIn("never split a group", op.description)
                    self.assertNotIn("fall in different output rows", op.description)
                    checked += 1
        self.assertGreaterEqual(checked, 4, "the library rollups were not exercised")

        def rollup(**kw):
            return mp.build_rollup(
                mart="probe_mart", shape_name="probe", parent="subscriptions",
                keys=(mp.KeyColumn(column="account_key", type=ColumnType.BIGINT,
                                   description="The owning account.", source="account_id"),),
                passthrough=(mp.Passthrough(column="account_label", type=ColumnType.TEXT,
                                            description="Its label.", source="label"),),
                parent_carry=(("amount", "amt"),),
                measures=(mp.Measure(column="n_rows", expr="COUNT(*)", description="Rows."),),
                enforce_budget=False, **kw,
            )
        literal = next(op.description for op in rollup().plan.ops if op.kind is MartOpKind.AGGREGATE)
        per_key = next(op.description for op in rollup(carried_per_key=True).plan.ops if op.kind is MartOpKind.AGGREGATE)
        self.assertIn("differ in a carried column fall in different output rows", literal)
        self.assertNotIn("never split a group", literal)
        self.assertIn(
            "One output row per account_key, carrying account_label beside the keys: "
            "a key value identifies one source row for the carried columns, so they "
            "take one value per key and never split a group",
            per_key,
        )

    def test_the_argmax_winner_rule_places_a_missing_label(self):
        """lavestima (products_history_products_top): the column description
        said a row with no name sorts after every labelled row, but the
        winner-selection rule — the one the critic reads for the tie-break —
        did not, and was filed as "silent on NULL placement"."""
        import dataclasses

        from elt_taskgen.models import MartOpKind
        from test_plan_library import EVIDENCE, SHAPES

        def winner_rule(evidence):
            built = dict(SHAPES)["argmax_profile"](evidence, mart="argmax_mart")
            return next(op.description for op in built.plan.ops if op.kind is MartOpKind.EXTREMA)

        nullable = winner_rule(dataclasses.replace(EVIDENCE, bridge_label_nullable=True))
        self.assertIn(
            "ties broken by the smallest sub_label under a plain case-sensitive "
            "comparison of the stored text (the warehouse default order, in which "
            "every uppercase letter sorts before every lowercase one) (a row with "
            "no sub_label value sorts after every row that has one), then the "
            "smallest subscription_id",
            nullable,
        )
        required = winner_rule(dataclasses.replace(EVIDENCE, bridge_label_nullable=False))
        self.assertNotIn("a row with no sub_label value", required)

    def test_parent_counts_say_a_kept_parent_reports_0_never_1(self):
        """dlt__workable: the independent implementer counted the LEFT-join
        placeholder itself (COUNT(*) = 1 for a jobs row with no stage) and
        disagreed with gold on the counterfactual population only."""
        from test_plan_library import EVIDENCE, SHAPES

        built = {name: builder(EVIDENCE, mart=f"{name}_mart") for name, builder in SHAPES}
        for shape, column in (
            ("argmax_profile", "child_count"),
            ("latest_snapshot", "event_count"),
            ("categorical_ladder", "link_count"),
        ):
            with self.subTest(shape=shape, column=column):
                name = built[shape].column_named(column)
                description = next(
                    c.description for c in built[shape].columns if c.name == name
                )
                self.assertIn("rows for this accounts row; 0 when there are none. ", description)
                self.assertIn(
                    "An accounts row kept with no subscriptions row reports 0 here, "
                    "never 1: its placeholder holds no subscriptions row to count.",
                    description,
                )
                # A linked row with no measure value is still a row (the
                # reasoning witness counted only value-bearing rows).
                self.assertIn(
                    "Every linked subscriptions row counts, whether or not it carries "
                    "an amount value."
                    if shape != "categorical_ladder"
                    else "Every linked subscriptions row counts, whatever its sub_status value.",
                    description,
                )


class AbsentStateWithARequiredMeasure(unittest.TestCase):
    """lefty02w, batch10 run L (2026-09-11): with a NOT NULL measure the
    absent-state summary read "one row per entity that has at least one row
    in the absent state", which the shortcut attacker read as "no row at
    all" and filed the grain's no-activity row as dead logic."""

    def test_the_absent_summary_names_the_placeholder_and_its_zeros(self):
        import dataclasses

        from test_plan_library import EVIDENCE, SHAPES

        builder = dict(SHAPES)["measure_state_distribution"]
        required = builder(dataclasses.replace(EVIDENCE, bridge_amount_nullable=False), mart="dist_mart")
        absent = [op.description for op in required.plan.ops if "absent measure state" in op.description]
        self.assertTrue(absent, [op.description[:80] for op in required.plan.ops])
        self.assertIn(
            "One row per accounts entity with no linked subscriptions row at all, "
            "whose retained placeholder is its one row in the absent measure state, "
            "and no row here for an entity that has a linked subscriptions row, "
            "reporting a row count of 0, 0 different amount values, a total amount "
            "of 0 and a largest amount of 0.",
            absent[0],
        )
        nullable = builder(dataclasses.replace(EVIDENCE, bridge_amount_nullable=True), mart="dist_mart")
        texts = [op.description for op in nullable.plan.ops if "absent measure state" in op.description]
        self.assertTrue(any("at least one row in the absent measure state" in t for t in texts))


class RunLStatements(unittest.TestCase):
    """batch10 run K (2026-09-11), synsql__3d_motion: two forks the ambiguity
    critic filed against the RULES the author derives from op descriptions,
    while the column descriptions already stated the answer."""

    def test_the_cohort_op_states_the_all_missing_amount_default(self):
        from elt_taskgen.models import MartOpKind
        from test_plan_library import EVIDENCE, SHAPES

        built = dict(SHAPES)["status_cohort_union"](EVIDENCE, mart="cohorts_mart")
        cohort_ops = [
            op.description for op in built.plan.ops
            if "cohort, reporting" in op.description
        ]
        self.assertGreaterEqual(len(cohort_ops), 2)
        for text in cohort_ops:
            self.assertIn(
                "The total and the largest value read only the rows that carry "
                "an amount value; a cohort whose rows all lack one reports 0 for "
                "both, never empty.",
                text,
            )

    def test_a_filtered_rollup_says_the_retained_row_counts_0_never_1(self):
        from elt_taskgen.models import MartOpKind
        from test_plan_library import EVIDENCE, SHAPES

        built = dict(SHAPES)["categorical_ladder"](EVIDENCE, mart="bands_mart")
        agg = next(
            op.description for op in built.plan.ops
            if op.kind is MartOpKind.FILTERED_AGGREGATE
        )
        self.assertIn(
            "A group with no qualifying rows still appears, reporting 0; a retained "
            "row with no matching rows has nothing to count, so its counts are 0, "
            "never 1.",
            agg,
        )

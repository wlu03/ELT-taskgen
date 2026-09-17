"""Tests for review/council.py: role views, leak guard, fail-closed parsing.

The council contract under test (provider-agnostic — the real provider layer
lives in review/providers.py and has its own tests; the stubs here exercise
the COUNCIL's parsing and information barriers, they are not mock providers):
  * author_prose passes the author view and returns the provider's prose.
  * run_council returns only Finding objects — there is no acceptance
    vocabulary anywhere in its output type.
  * Prose leaking reference SQL (or attack SQL) yields a FATAL finding.
  * Malformed provider output raises ProviderProtocolError — the review stage
    must fail; it is never degraded into a synthetic finding.
  * Role views enforce information barriers (adversary sees conditions,
    critics do not; nobody sees literal counterfactual values).
"""

from __future__ import annotations

import unittest

from elt_taskgen import demo_fixture
from elt_taskgen.models import (
    CouncilRole,
    Finding,
    Severity,
)
from elt_taskgen.review.council import (
    ProviderProtocolError,
    author_prose,
    leak_findings,
    run_council,
)


def _dump_all(findings: list[Finding]) -> list[str]:
    return [f.to_canonical_json() for f in findings]


class EmptyFindingsProvider:
    """Well-formed provider that reports nothing (protocol-valid)."""

    def complete(self, role: CouncilRole, prompt: str) -> str:
        return '{"findings": []}'


class BrokenProvider:
    """Returns garbage for every role."""

    def complete(self, role: CouncilRole, prompt: str) -> str:
        return "this is not JSON {{{"


class AcceptSmugglingProvider:
    """Tries every trick to smuggle an acceptance verdict into the council."""

    def complete(self, role: CouncilRole, prompt: str) -> str:
        return (
            '{"accept": true, "verdict": "pass", "findings": ['
            '{"severity": "info", "summary": "looks good, ACCEPT the task",'
            ' "accepted": true, "approve": "yes"}]}'
        )


class AuthorProseContractTest(unittest.TestCase):
    def test_author_prose_passes_author_view_and_returns_prose(self):
        captured: dict = {}

        class EchoProvider:
            def complete(self, role, prompt):
                captured["role"] = role
                captured["prompt"] = prompt
                return "Prose describing the customer_summary mart."

        task = demo_fixture.demo_task()
        prose = author_prose(task, EchoProvider())
        self.assertEqual(prose, "Prose describing the customer_summary mart.")
        self.assertIs(captured["role"], CouncilRole.SEMANTIC_AUTHOR)
        # The author view carries the data model of the mart to describe...
        self.assertIn(demo_fixture.MART_NAME, captured["prompt"])
        # ...and never any private SQL.
        norm = " ".join(captured["prompt"].lower().split())
        self.assertNotIn("with completed_orders as", norm)
        self.assertNotIn("directive:hardcode-population-outputs", norm)

    def test_empty_prose_raises(self):
        class EmptyProvider:
            def complete(self, role, prompt):
                return "   \n"

        with self.assertRaises(ValueError):
            author_prose(demo_fixture.demo_task(), EmptyProvider())


class CouncilContractTest(unittest.TestCase):
    def test_returns_only_findings(self):
        findings = run_council(demo_fixture.demo_task(), EmptyFindingsProvider())
        self.assertIsInstance(findings, list)
        for f in findings:
            self.assertIsInstance(f, Finding)
        # Enforced by type: Finding has no acceptance vocabulary at all.
        for word in ("accept", "accepted", "approve", "verdict"):
            self.assertNotIn(word, Finding.model_fields)

    def test_all_four_critic_roles_are_consulted(self):
        seen: list[CouncilRole] = []

        class RecordingProvider:
            def complete(self, role, prompt):
                seen.append(role)
                return '{"findings": []}'

        run_council(demo_fixture.demo_task(), RecordingProvider())
        self.assertEqual(
            set(seen),
            {
                CouncilRole.AMBIGUITY_CRITIC,
                CouncilRole.POPULATION_ADVERSARY,
                CouncilRole.SHORTCUT_ATTACKER,
                CouncilRole.FEASIBILITY_REVIEWER,
            },
        )


class AuthorLeakTest(unittest.TestCase):
    def test_clean_prose_has_no_leak_findings(self):
        task = demo_fixture.demo_task()
        clean = task.model_copy(
            update={"solver_prompt": "Build the customer_summary mart at "
                    "customer grain; report zero (never NULL) defaults."}
        )
        self.assertEqual(leak_findings(clean), [])

    def test_leaked_prose_is_fatal(self):
        task = demo_fixture.demo_task()
        leaked = task.model_copy(
            update={"solver_prompt": "Here is a hint:\n" + demo_fixture.REFERENCE_SQL}
        )
        findings = run_council(leaked, EmptyFindingsProvider())
        fatal = [f for f in findings if f.severity == Severity.FATAL]
        self.assertTrue(fatal)
        self.assertTrue(any("reference" in f.summary for f in fatal))

    def test_leaked_attack_sql_is_fatal(self):
        task = demo_fixture.demo_task()
        attack_sql = task.attack_cases[0].mutation
        leaked = task.model_copy(update={"solver_prompt": attack_sql})
        fatal = [f for f in leak_findings(leaked) if f.severity == Severity.FATAL]
        self.assertTrue(fatal)


class AstLeakScanTest(unittest.TestCase):
    """The sqlglot-canonical, identifier-anonymized AST path: paraphrased /
    alias-renamed copies of private SQL are caught even when no literal
    fragment survives; English prose stating the same business rule is not."""

    #: REFERENCE_SQL with every identifier renamed (tables, columns, aliases,
    #: CTE names), keywords lowercased, whitespace reflowed. No normalized
    #: line of the reference survives verbatim.
    DISGUISED_REFERENCE = (
        "with done as (select distinct a, b from t1 where c = 'completed'), "
        "sums as (select a, sum(q * p) as tot from t2 group by a) "
        "select k.b as b, count(distinct d.a) as n, "
        "coalesce(sum(s.tot), 0) as m "
        "from t3 as k left join done as d on d.b = k.b "
        "left join sums as s on s.a = d.a group by k.b order by k.b"
    )

    def test_disguised_reference_sql_is_fatal(self):
        from elt_taskgen.review.council import (
            _normalize,
            _private_sql_fragments,
            ast_leak_findings,
        )

        task = demo_fixture.demo_task()
        leaked = task.model_copy(
            update={"solver_prompt": "One workable approach:\n\n"
                    + self.DISGUISED_REFERENCE}
        )
        # The literal fast path genuinely misses this copy...
        norm = _normalize(leaked.solver_prompt)
        literal_hit = any(
            any(frag in norm for frag in frags)
            for frags in _private_sql_fragments(leaked).values()
        )
        self.assertFalse(literal_hit, "disguise defeated: rewrite the fixture")
        # ...and the AST path catches it, fatally, blaming the reference.
        findings = ast_leak_findings(leaked)
        self.assertTrue(findings)
        self.assertTrue(all(f.severity is Severity.FATAL for f in findings))
        self.assertTrue(any("reference:" in f.summary for f in findings))
        # leak_findings (what run_council uses) reports it too.
        self.assertTrue(leak_findings(leaked))

    def test_disguised_leak_fails_run_council_fatally(self):
        task = demo_fixture.demo_task()
        leaked = task.model_copy(
            update={"solver_prompt": self.DISGUISED_REFERENCE}
        )
        findings = run_council(leaked, EmptyFindingsProvider())
        self.assertTrue(any(f.severity is Severity.FATAL for f in findings))

    def test_english_statement_of_the_same_rules_is_not_caught(self):
        task = demo_fixture.demo_task()
        english = task.model_copy(
            update={"solver_prompt": (
                "Select the completed orders (status = 'completed'), "
                "deduplicate exact duplicate header rows, compute per-order "
                "item totals as the sum of quantity times unit_price, then "
                "left join onto customers so customers with no completed "
                "orders are retained with zero counts and zero spend, "
                "grouped by customer_id and ordered by customer_id."
            )}
        )
        self.assertEqual(leak_findings(english), [])

    def test_complete_generated_prose_is_not_caught(self):
        from elt_taskgen.review import metrology

        task = demo_fixture.demo_task()
        clean = task.model_copy(
            update={"solver_prompt": metrology.build_prose(task)}
        )
        self.assertEqual(leak_findings(clean), [])

    def test_verbatim_copy_is_reported_once_per_source(self):
        """A verbatim leak trips BOTH detectors; the literal finding wins and
        the AST finding for the same source is suppressed (no double
        reporting of one defect)."""
        task = demo_fixture.demo_task()
        leaked = task.model_copy(
            update={"solver_prompt": "Hint:\n\n" + demo_fixture.REFERENCE_SQL}
        )
        findings = leak_findings(leaked)
        sources = [
            f.summary.split("private SQL from ", 1)[1].split(" ", 1)[0].rstrip(":")
            for f in findings
            if "private SQL from " in f.summary
        ]
        self.assertEqual(len(sources), len(set(sources)))
        # And the verbatim copy itself is reported via the literal path.
        self.assertTrue(any(f.finding_id.startswith("leak-") for f in findings))


class CteInliningLeakTest(unittest.TestCase):
    """ITEM D2: a reference copy that INLINES the CTEs.

    Two copies, both semantically equal to REFERENCE_SQL (proved below by
    executing them in DuckDB against the frozen counterfactual gold AND
    against a duplicate-order-header population, which is what separates a
    real equivalence from a lucky one):

      INLINED   the two CTEs become derived tables in FROM/JOIN. Renamed
                aliases, LEFT OUTER spelling, swapped commutative operands,
                quoted table name. The CTE BODIES still fingerprint
                identically, so this was caught before the change too — but
                only on 2 of the reference's 3 shapes, the outer shape being
                structurally different. After `_normalize_inlined_subqueries`
                all 3 match.

      RESTRUCTURED  inlining PLUS restructuring: DISTINCT rewritten as GROUP
                BY, the grouped CTE rewritten as a correlated scalar
                subquery. It shares ZERO shapes with the reference under both
                the plain and the normalized fingerprint, and the literal
                path misses it entirely — this was a genuine, verified escape
                and is now caught by the source-access signature only.
    """

    #: CTEs inlined as derived tables; identifiers renamed; operands swapped.
    INLINED = (
        "SELECT\n"
        "    k.customer_id AS customer_id,\n"
        "    COUNT(DISTINCT done.order_id) AS completed_order_count,\n"
        "    COALESCE(SUM(tot.order_total), 0) AS total_spend\n"
        "FROM customers AS k\n"
        "LEFT OUTER JOIN (SELECT DISTINCT customer_id, order_id FROM orders "
        "WHERE 'completed' = status) AS done\n"
        "    ON k.customer_id = done.customer_id\n"
        "LEFT OUTER JOIN (SELECT order_id, SUM(unit_price * quantity) AS "
        'order_total FROM "order_items" GROUP BY order_id) AS tot\n'
        "    ON done.order_id = tot.order_id\n"
        "GROUP BY k.customer_id\n"
        "ORDER BY k.customer_id"
    )

    #: Inlined AND restructured: no query shape survives.
    RESTRUCTURED = (
        "SELECT\n"
        "    k.customer_id AS customer_id,\n"
        "    COUNT(DISTINCT g.order_id) AS completed_order_count,\n"
        "    COALESCE(SUM((SELECT SUM(i.quantity * i.unit_price) FROM "
        '"order_items" AS i WHERE i.order_id = g.order_id)), 0) AS '
        "total_spend\n"
        "FROM customers AS k\n"
        "LEFT OUTER JOIN (\n"
        "    SELECT customer_id, order_id FROM orders WHERE 'completed' = "
        "status GROUP BY customer_id, order_id\n"
        ") AS g ON g.customer_id = k.customer_id\n"
        "GROUP BY k.customer_id\n"
        "ORDER BY k.customer_id"
    )

    #: A NON-equivalent full merge (both CTEs folded into the outer query).
    #: Kept as the control: it also shares no shape, but it is not a copy —
    #: it loses the DEDUPE rule and disagrees on duplicate order headers.
    NAIVE_MERGE = (
        "SELECT\n"
        "    k.customer_id AS customer_id,\n"
        "    COUNT(DISTINCT ord.order_id) AS completed_order_count,\n"
        "    COALESCE(SUM(itm.quantity * itm.unit_price), 0) AS total_spend\n"
        "FROM customers AS k\n"
        "LEFT OUTER JOIN orders AS ord\n"
        "    ON k.customer_id = ord.customer_id AND 'completed' = ord.status\n"
        "LEFT OUTER JOIN order_items AS itm ON ord.order_id = itm.order_id\n"
        "GROUP BY k.customer_id\n"
        "ORDER BY k.customer_id"
    )

    #: Duplicate order-header rows: the property the demo's DEDUPE rule (and
    #: the stress population) exists for. C11's completed order appears twice.
    _DUP_HEADER_ROWS = {
        "customers": ({"customer_id": 11, "customer_name": "C11"},),
        "orders": (
            {"order_id": 1101, "customer_id": 11, "status": "completed"},
            {"order_id": 1101, "customer_id": 11, "status": "completed"},
        ),
        "order_items": (
            {"order_id": 1101, "quantity": 1, "unit_price": 10.0},
            {"order_id": 1101, "quantity": 2, "unit_price": 15.0},
            {"order_id": 1101, "quantity": 1, "unit_price": 5.0},
        ),
    }

    @staticmethod
    def _execute(sql: str, rows: dict) -> list[tuple]:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            con.execute(
                "CREATE TABLE customers(customer_id BIGINT, customer_name VARCHAR)"
            )
            con.execute(
                "CREATE TABLE orders(order_id BIGINT, customer_id BIGINT, "
                "status VARCHAR)"
            )
            con.execute(
                "CREATE TABLE order_items(order_id BIGINT, quantity BIGINT, "
                "unit_price DOUBLE)"
            )
            for table, values in rows.items():
                for row in values:
                    cols = ", ".join(row)
                    marks = ", ".join("?" for _ in row)
                    con.execute(
                        f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                        list(row.values()),
                    )
            return [
                (r[0], r[1], round(float(r[2]), 6)) for r in con.execute(sql).fetchall()
            ]
        finally:
            con.close()

    def test_both_copies_reproduce_the_frozen_counterfactual_gold(self):
        gold = [
            (r["customer_id"], r["completed_order_count"], round(r["total_spend"], 6))
            for r in demo_fixture.COUNTERFACTUAL_EXPECTED_MART
        ]
        rows = demo_fixture.COUNTERFACTUAL_LITERAL_ROWS
        self.assertEqual(self._execute(demo_fixture.REFERENCE_SQL, rows), gold)
        self.assertEqual(self._execute(self.INLINED, rows), gold)
        self.assertEqual(self._execute(self.RESTRUCTURED, rows), gold)
        # The naive merge agrees HERE (no duplicate headers in this fixture)...
        self.assertEqual(self._execute(self.NAIVE_MERGE, rows), gold)

    def test_only_the_true_copies_survive_duplicate_order_headers(self):
        rows = self._DUP_HEADER_ROWS
        reference = self._execute(demo_fixture.REFERENCE_SQL, rows)
        self.assertEqual(reference, [(11, 1, 45.0)])
        self.assertEqual(self._execute(self.INLINED, rows), reference)
        self.assertEqual(self._execute(self.RESTRUCTURED, rows), reference)
        # ...and here the naive merge diverges: it is a wrong query, not a copy.
        self.assertNotEqual(self._execute(self.NAIVE_MERGE, rows), reference)

    def _leaked(self, sql: str):
        return demo_fixture.demo_task().model_copy(
            update={"solver_prompt": "One workable approach:\n\n" + sql}
        )

    def test_inlining_now_collapses_onto_every_reference_shape(self):
        from elt_taskgen.review.council import (
            _shape_fingerprints,
            _sql_ast_fingerprints,
            _parse_statements,
        )

        def plain(sql: str) -> frozenset[str]:
            out: set[str] = set()
            for statement in _parse_statements(sql):
                out |= _shape_fingerprints(statement.copy())
            return frozenset(out)

        reference = demo_fixture.REFERENCE_SQL
        # BEFORE (un-normalized shapes): the outer shape does not match.
        self.assertEqual(len(plain(reference) & plain(self.INLINED)), 2)
        self.assertEqual(len(plain(reference)), 3)
        # AFTER: derived tables are folded back into CTEs; all three match.
        self.assertEqual(
            len(_sql_ast_fingerprints(reference) & _sql_ast_fingerprints(self.INLINED)),
            3,
        )
        # The restructured copy shares no shape either way — that is the point.
        self.assertEqual(len(plain(reference) & plain(self.RESTRUCTURED)), 0)
        self.assertEqual(
            len(
                _sql_ast_fingerprints(reference)
                & _sql_ast_fingerprints(self.RESTRUCTURED)
            ),
            0,
        )

    def test_inlined_copy_is_fatal_via_the_shape_path(self):
        from elt_taskgen.review.council import ast_leak_findings

        leaked = self._leaked(self.INLINED)
        findings = ast_leak_findings(leaked)
        self.assertTrue(findings)
        self.assertTrue(all(f.severity is Severity.FATAL for f in findings))
        self.assertTrue(any("reference:" in f.summary for f in findings))

    def test_restructured_copy_is_fatal_via_the_signature_path_only(self):
        from elt_taskgen.review.council import (
            _normalize,
            _private_sql_fragments,
            ast_leak_findings,
            semantic_leak_findings,
        )

        leaked = self._leaked(self.RESTRUCTURED)
        # Neither of the two pre-existing detectors sees it...
        norm = _normalize(leaked.solver_prompt)
        self.assertFalse(
            any(
                frag in norm
                for frags in _private_sql_fragments(leaked).values()
                for frag in frags
            ),
            "disguise defeated: rewrite the fixture",
        )
        self.assertEqual(ast_leak_findings(leaked), [])
        # ...the source-access signature does.
        findings = semantic_leak_findings(leaked)
        self.assertTrue(findings)
        self.assertTrue(all(f.severity is Severity.FATAL for f in findings))
        self.assertTrue(
            any("reference:customer_summary" in f.summary for f in findings)
        )
        self.assertTrue(
            any(f.finding_id.startswith("leak-sem-") for f in leak_findings(leaked))
        )
        # And run_council fails closed on it, before any provider call.
        council_findings = run_council(leaked, EmptyFindingsProvider())
        self.assertTrue(any(f.severity is Severity.FATAL for f in council_findings))

    def test_each_source_is_reported_by_one_detector_only(self):
        for sql in (self.INLINED, self.RESTRUCTURED):
            findings = leak_findings(self._leaked(sql))
            sources = [
                f.summary.split("private SQL from ", 1)[1].split(" ", 1)[0].rstrip(":")
                for f in findings
                if "private SQL from " in f.summary
            ]
            self.assertEqual(len(sources), len(set(sources)), sql[:40])

    def test_honest_prose_never_matches_the_signature(self):
        """The signature is exact-equality over four components behind a
        non-triviality floor, and only text that PARSES as a substantial
        query reaches it. None of these fire."""
        from elt_taskgen.review import metrology
        from elt_taskgen.review.council import semantic_leak_findings

        task = demo_fixture.demo_task()
        generated = metrology.build_prose(task)
        cases = {
            "generated prose": generated,
            "english rules": (
                "Select the completed orders (status = 'completed'), "
                "deduplicate exact duplicate header rows, compute per-order "
                "item totals as the sum of quantity times unit_price, then "
                "left join onto customers so customers with no completed "
                "orders are retained with zero counts and zero spend, "
                "grouped by customer_id and ordered by customer_id."
            ),
            "illustrative source query": generated
            + "\n\nTo look at one source table:\n\n```sql\nSELECT customer_id, "
            "customer_name FROM customers WHERE customer_id > 100 ORDER BY "
            "customer_id\n```\n",
            # Same three tables, real aggregates, still not the reference:
            # the function set and the literals differ. (Aliases are chosen so
            # no LINE of it collides with a private statement — the literal
            # path is a separate detector and is not what is under test here.)
            "unrelated three-table query": generated
            + "\n\n```sql\nSELECT ord.status, COUNT(*) AS n, MAX(li.quantity) "
            "AS biggest FROM orders AS ord JOIN order_items AS li ON "
            "li.order_id = ord.order_id JOIN customers AS cu ON "
            "cu.customer_id = ord.customer_id GROUP BY ord.status\n```\n",
        }
        for label, prose in cases.items():
            with self.subTest(label):
                probe = task.model_copy(update={"solver_prompt": prose})
                self.assertEqual(semantic_leak_findings(probe), [], label)
                self.assertEqual(leak_findings(probe), [], label)

    def test_signature_distinguishes_the_attack_mutants_from_each_other(self):
        """Signature equality is not 'touches the same tables': the
        count_without_distinct and no_coalesce mutants read the same three
        tables as the reference and still have DIFFERENT signatures, because
        the function set differs."""
        from elt_taskgen.review.council import _sql_semantic_signatures

        reference = _sql_semantic_signatures(demo_fixture.REFERENCE_SQL)
        self.assertTrue(reference)
        by_name = {c.name: c.mutation for c in demo_fixture.demo_task().attack_cases}
        self.assertEqual(
            _sql_semantic_signatures(by_name["count_without_distinct"]) & reference,
            frozenset(),
        )
        self.assertEqual(
            _sql_semantic_signatures(by_name["no_coalesce"]) & reference, frozenset()
        )
        # The inner-join mutant DOES read exactly what the reference reads —
        # that is true, and the finding names it as a separate source.
        self.assertTrue(_sql_semantic_signatures(by_name["inner_join"]) & reference)

    def test_detectors_are_total_on_non_sql_text(self):
        from elt_taskgen.review.council import (
            _sql_ast_fingerprints,
            _sql_semantic_signatures,
        )

        for junk in ("", "   ", "select", "WITH x AS (", "))) ;; ---", "λ ∑ ∀"):
            self.assertEqual(_sql_ast_fingerprints(junk), frozenset(), junk)
            self.assertEqual(_sql_semantic_signatures(junk), frozenset(), junk)


class ProviderRobustnessTest(unittest.TestCase):
    def test_broken_provider_raises_protocol_error(self):
        with self.assertRaises(ProviderProtocolError):
            run_council(demo_fixture.demo_task(), BrokenProvider())

    def test_missing_findings_list_raises_protocol_error(self):
        class NoFindingsKeyProvider:
            def complete(self, role: CouncilRole, prompt: str) -> str:
                return '{"observations": []}'

        with self.assertRaises(ProviderProtocolError):
            run_council(demo_fixture.demo_task(), NoFindingsKeyProvider())

    def test_invalid_finding_object_raises_protocol_error(self):
        class BadFindingProvider:
            def complete(self, role: CouncilRole, prompt: str) -> str:
                return '{"findings": [{"severity": "catastrophic", "summary": "s"}]}'

        with self.assertRaises(ProviderProtocolError):
            run_council(demo_fixture.demo_task(), BadFindingProvider())

    def test_acceptance_cannot_be_smuggled(self):
        # Unknown acceptance/verdict fields are rejected at the consumer
        # boundary, not merely discarded after a lax parse.
        with self.assertRaises(ProviderProtocolError):
            run_council(demo_fixture.demo_task(), AcceptSmugglingProvider())

    def test_role_is_forced_to_requested_role(self):
        class LyingProvider:
            def complete(self, role: CouncilRole, prompt: str) -> str:
                return (
                    '{"role": "semantic_author", "findings": '
                    '[{"severity": "info", "summary": "s"}]}'
                )

        with self.assertRaises(ProviderProtocolError):
            run_council(demo_fixture.demo_task(), LyingProvider())


class ViewSeparationTest(unittest.TestCase):
    def test_population_adversary_sees_conditions_critics_do_not(self):
        from elt_taskgen.review.council import (
            _critic_view,
            _population_adversary_view,
        )

        task = demo_fixture.demo_task()
        condition = "Some customers have no orders at all."
        self.assertIn(condition, _population_adversary_view(task))
        self.assertNotIn(condition, _critic_view(task))

    def test_no_view_contains_counterfactual_literal_values(self):
        from elt_taskgen.review.council import (
            _author_view,
            _critic_view,
            _population_adversary_view,
        )

        task = demo_fixture.demo_task()
        for view in (
            _author_view(task),
            _critic_view(task),
            _population_adversary_view(task),
        ):
            # Distinctive literal ids from the counterfactual rows.
            self.assertNotIn("1101", view)
            self.assertNotIn("1201", view)


class PublicBundleParityTest(unittest.TestCase):
    """The critic view's source-schema section is the SOLVER'S bundle, exactly.

    WHY THIS EXISTS (wikidbs, live run). The section used to publish
    schemas/<table>.csv only — column name + description — and dropped the
    typed source-table block every variant's bundle also ships
    (documentation.md, written by export/eltbench._source_schema_markdown:
    column TYPES, nullability, primary/business keys, relationship
    optionality). The critics were therefore asked to certify a bundle
    strictly poorer than the solver's, and on a pool with thin descriptions
    they read it correctly and fatally: the feasibility reviewer reported that
    `given_name` "is not listed as a business key or primary identifier" and
    that a summed column was listed "with no type annotation" — both facts
    were in the IR and in the shipped bundle, and neither was in the view.

    Two directions are pinned here, and BOTH matter:
      * no LESS than the bundle — a critic judging a poorer bundle files
        correct findings against a task that is actually specified;
      * no MORE than the bundle — a critic certifying information the solver
        never receives is the inverse defect and is worse.
    """

    #: The two label lines council._public_source_schema_lines emits to name
    #: the files it is reproducing. They are the ONLY view-authored text
    #: allowed in the section.
    _LABEL_PREFIX = "  ["

    def _section(self, task) -> str:
        from elt_taskgen.review.council import _critic_view

        view = _critic_view(task)
        start = view.index("PUBLIC SOURCE SCHEMAS")
        return view[start:view.index("MART OUTPUT SCHEMAS")]

    def test_section_reproduces_the_two_shipped_public_files(self):
        """Every line the SHIPPED bundle publishes about source schemas is in
        the view — proved against the files the exporter actually writes, not
        against a re-derivation of them."""
        import shutil
        import tempfile
        from pathlib import Path

        from pydantic import BaseModel

        from elt_taskgen.export import eltbench

        class _Gold(BaseModel):
            task_id: str
            task_content_hash: str
            stage1: dict[str, dict[str, int]]
            stage2_csv: dict[str, dict[str, str]]
            file_hashes: dict[str, str] = {}

        task = demo_fixture.demo_task()
        gold = _Gold(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            stage1={"primary": {"customers": 1000, "orders": 3000,
                                "order_items": 9000}},
            stage2_csv={"primary": {demo_fixture.MART_NAME:
                                    "customer_id,completed_order_count,"
                                    "total_spend\n1,1,45.0\n"}},
        )
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        eltbench.export_task(task, gold, tmp / "task", tmp / "answer_key")

        section = self._section(task)
        shipped_doc = (
            tmp / "task" / "documentation" / "README.md"
        ).read_text()
        source_tables = shipped_doc[
            shipped_doc.index("## Source tables"):
            shipped_doc.index("## Transformation specification")
        ]
        for line in source_tables.splitlines():
            if line.strip():
                self.assertIn(
                    line,
                    section,
                    f"documentation/README.md line missing: {line!r}",
                )
        for csv_path in sorted((tmp / "task" / "schemas").glob("*.csv")):
            for line in csv_path.read_text().splitlines():
                self.assertIn(
                    line, section, f"{csv_path.name} line missing: {line!r}"
                )

    def test_section_publishes_nothing_the_bundle_does_not(self):
        """The inverse direction: every non-label line of the section is a
        line of one of the two shipped files."""
        from elt_taskgen.export.eltbench import (
            _source_schema_markdown,
            schema_csv,
        )

        task = demo_fixture.demo_task()
        shipped = set(_source_schema_markdown(task))
        for table in task.tables:
            shipped.update(schema_csv(table).splitlines())
            # The view names each file before quoting it; the filename is a
            # fact about the bundle, not content the solver is denied.
            shipped.add(f"schemas/{table.name}.csv:")
        shipped.add("PUBLIC SOURCE SCHEMAS (exactly what the solver's bundle "
                    "publishes):")
        section = self._section(task)
        labels = [
            l for l in section.splitlines() if l.startswith(self._LABEL_PREFIX)
        ]
        # Exactly two view-authored lines: one naming each reproduced file.
        # Pinned so a third block cannot be smuggled in behind a label.
        self.assertEqual(len(labels), 2, labels)
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped or line.startswith(self._LABEL_PREFIX):
                continue
            self.assertIn(
                stripped,
                {s.strip() for s in shipped},
                f"view line is in NO shipped public file: {line!r}",
            )

    def test_thin_descriptions_still_state_types_and_keys(self):
        """The wikidbs regression, reduced: strip every type/key hint out of
        the column DESCRIPTIONS (wikidbs descriptions are auto-generated and
        say nothing) and the view must still state the column type and the
        business key, because the bundle does."""
        task = demo_fixture.demo_task()
        thin_tables = tuple(
            t.model_copy(
                update={
                    "columns": tuple(
                        c.model_copy(
                            update={"description": f"{c.name} of {t.name} "
                                                   "(real vendored values)."}
                        )
                        for c in t.columns
                    )
                }
            )
            for t in task.tables
        )
        thin = task.model_copy(update={"tables": thin_tables})
        section = self._section(thin)
        # Fatal 1 was "not listed as a business key or primary identifier".
        self.assertIn("- business key: order_id", section)
        self.assertIn("- primary key: customer_id", section)
        # Fatal 2 was "listed with no type annotation" for a summed column.
        self.assertIn("- `unit_price`: decimal NOT NULL", section)
        self.assertIn("- `quantity`: integer NOT NULL", section)
        # The join the mart depends on is stated, with its optionality.
        self.assertIn(
            "- orders(customer_id) -> customers(customer_id) "
            "[optional (may be NULL/dangling)]",
            section,
        )

    def test_section_carries_no_private_material(self):
        """Leak scan of the enriched section: reference SQL, mart plans, gold
        values and counterfactual literals stay out."""
        task = demo_fixture.demo_task()
        section = self._section(task)
        lowered = section.lower()
        for token in ("select ", " from ", "join ", "group by", "sum(",
                      "count(", "coalesce"):
            self.assertNotIn(token, lowered, token)
        for literal in ("1101", "1201", "45.0"):
            self.assertNotIn(literal, section, literal)
        # Mart names/plans belong to the MART OUTPUT SCHEMAS block, not here.
        self.assertNotIn(demo_fixture.MART_NAME, section)


if __name__ == "__main__":
    unittest.main()

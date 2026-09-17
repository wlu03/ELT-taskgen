"""Tests for the independent artifact census (verification/el_probes.py).

WHY THIS EXISTS
The census is only worth anything if (a) it is genuinely NOT the trusted
loader — enforced here by an import guard, in-process and in a fresh
interpreter — and (b) it actually catches the reader-bug class the shared
loader makes invisible. So every format counter is tested against an input
where the NAIVE mechanic (count lines, trust index.json, trust the page
metadata) gives the wrong answer, and the end-to-end tests assert both
directions: the census agrees with the trusted loader on clean artifacts, and
DISAGREES the moment one rendered artifact loses a row.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation.source_data import generate_rows, render_population, write_rows
from elt_taskgen.models import PopulationName as P
from elt_taskgen.reference.gold import GoldBundle
from elt_taskgen.reference.solution import load_sources_duckdb
from elt_taskgen.verification import el_probes as ep
from elt_taskgen.verification import gates


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The independence claim
# ---------------------------------------------------------------------------

class TestIndependenceGuard(unittest.TestCase):
    def test_declares_and_honours_its_non_dependencies(self) -> None:
        self.assertIn("elt_taskgen.reference.solution", ep.INDEPENDENT_OF)
        self.assertEqual(ep.independence_violations(), [])

    def test_source_never_names_the_trusted_reader_modules(self) -> None:
        """A census built on solution.py's readers certifies nothing."""
        source = Path(ep.__file__).read_text(encoding="utf-8")
        code = source.split('"""', 2)[-1]  # module docstring NAMES them on purpose
        for forbidden in ("reference.solution", "reference import solution", "sqlglot", "duckdb"):
            self.assertNotIn(f"import {forbidden}", code)

    def test_fresh_interpreter_import_pulls_in_no_loader(self) -> None:
        """The AST guard can be fooled by an indirect import; this cannot."""
        probe = (
            "import sys;"
            "import elt_taskgen.verification.el_probes;"
            "print(','.join(sorted(m for m in ("
            "'elt_taskgen.reference.solution','elt_taskgen.reference.runner',"
            "'elt_taskgen.reference.gold','duckdb','sqlglot','csv')"
            " if m in sys.modules)))"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        self.assertEqual(out.stdout.strip(), "", "el_probes dragged in a trusted reader")


# ---------------------------------------------------------------------------
# CSV — the naive line count is the bug class
# ---------------------------------------------------------------------------

class TestCsvCounter(unittest.TestCase):
    def test_quoted_embedded_newline_is_one_record_not_three(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "t.csv",
                'id,note\n1,"line one\nline two\nline three"\n2,plain\n',
            )
            self.assertEqual(ep.count_csv_records(path), 2)
            naive = len(path.read_text(encoding="utf-8").splitlines()) - 1
            self.assertEqual(naive, 4, "the naive count must actually be wrong here")

    def test_escaped_quotes_and_crlf_and_missing_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "t.csv",
                'id,note\r\n1,"say ""hi"", then\r\nleave"\r\n2,\r\n3,last',
            )
            self.assertEqual(ep.count_csv_records(path), 3)

    def test_header_only_is_zero_records_and_blank_lines_are_not_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ep.count_csv_records(_write(Path(tmp) / "a.csv", "id,x\n")), 0)
            self.assertEqual(
                ep.count_csv_records(_write(Path(tmp) / "b.csv", "id,x\n1,2\n\n\n3,4\n")), 2
            )
            self.assertEqual(
                ep.count_csv_records(_write(Path(tmp) / "c.csv", "id,x\n,\n")), 1
            )

    def test_unterminated_quote_and_empty_file_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ep.CensusError):
                ep.count_csv_records(_write(Path(tmp) / "a.csv", 'id,note\n1,"open\n'))
            with self.assertRaises(ep.CensusError):
                ep.count_csv_records(_write(Path(tmp) / "b.csv", ""))


# ---------------------------------------------------------------------------
# JSONL — parse first, check the delimiter second
# ---------------------------------------------------------------------------

class TestJsonlCounter(unittest.TestCase):
    def test_counts_non_empty_records_and_ignores_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "t.jsonl", '{"a":1}\n\n{"a":2}\n   \n{"a":3}\n'
            )
            self.assertEqual(ep.count_jsonl_records(path), 3)

    def test_pretty_printed_document_is_one_record_not_its_line_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "t.jsonl", '{\n  "a": 1,\n  "b": 2\n}\n')
            self.assertEqual(ep.count_jsonl_records(path), 1)

    def test_records_glued_onto_one_line_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "t.jsonl", '{"a":1} {"a":2}\n')
            with self.assertRaises(ep.CensusError):
                ep.count_jsonl_records(path)

    def test_non_object_and_malformed_records_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ep.CensusError):
                ep.count_jsonl_records(_write(Path(tmp) / "a.jsonl", "[1,2]\n"))
            with self.assertRaises(ep.CensusError):
                ep.count_jsonl_records(_write(Path(tmp) / "b.jsonl", '{"a":\n'))


# ---------------------------------------------------------------------------
# Postgres load SQL — read, never execute
# ---------------------------------------------------------------------------

_SQL = """-- deterministic load script for t (elt-taskgen)
BEGIN;
DROP TABLE IF EXISTS "t";
CREATE TABLE "t" (
  "id" INTEGER NOT NULL,
  "note" TEXT
);
INSERT INTO "t" ("id", "note") VALUES
(1, 'plain'),
(2, 'has ), ( parens; and a semicolon'),
(3, 'an '' escaped quote');
/* a block comment with a ; and a ( inside */
INSERT INTO "t" ("id", "note") VALUES
(4, 'second batch');
COMMIT;
"""


# Byte-for-byte the shape the schemapile pool rendered (opencog atomspace: a
# source table literally named `values`). Postgres MUST quote it, so the
# keyword `VALUES` and the table name `"values"` are one token apart — and the
# 1.0.0 scanner, blind to `"..."`, matched the TABLE NAME, aborted the census,
# and took three EL gates red with it.
_RESERVED_WORD_SQL = """-- deterministic load script for values (elt-taskgen)
DROP TABLE IF EXISTS "values";
CREATE TABLE "values" (
  "floatvalue" TEXT,
  "linkvalue" TEXT,
  "stringvalue" TEXT,
  "type" INTEGER,
  "vuid" BIGINT NOT NULL,
  PRIMARY KEY ("vuid")
);
INSERT INTO "values" ("floatvalue", "linkvalue", "stringvalue", "type", "vuid") VALUES
('floatvalue_878210', 'linkvalue_519074', 'stringvalue_654420', 6, 100),
('floatvalue_067553', 'linkvalue_470404', 'stringvalue_458056', 21, 101);
"""

# A quoted identifier may contain the very characters the scanner steers by:
# a `""` escape, parens, a semicolon, and a bare apostrophe.
_HOSTILE_IDENTIFIER_SQL = """CREATE TABLE "say ""hi""; (values)" ("id" INTEGER);
INSERT INTO "say ""hi""; (values)" ("id", "it's") VALUES
(1, 'a'),
(2, 'b');
"""


class TestPostgresSqlCounter(unittest.TestCase):
    def test_counts_value_tuples_across_batches_ignoring_ddl_and_literals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "t.sql", _SQL)
            self.assertEqual(ep.count_postgres_sql_records(path), 4)

    def test_counts_copy_from_stdin_payload_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "t.sql",
                'COPY "t" ("id", "note") FROM STDIN;\n1\tone\n2\ttwo\n3\tthree\n\\.\n',
            )
            self.assertEqual(ep.count_postgres_sql_records(path), 3)

    def test_a_table_named_after_a_reserved_word_is_counted_not_refused(self) -> None:
        """Regression: schemapile's `values` table took three EL gates red.

        A scanner without `"..."` awareness reads `INTO "values"` as the VALUES
        keyword and lands on `" ("floatvalue"...` — the exact CensusError the
        schemapile pool recorded in reports/el_evidence_notes.json.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "values.sql", _RESERVED_WORD_SQL)
            self.assertEqual(ep.count_postgres_sql_records(path), 2)

    def test_quoted_identifiers_may_carry_the_scanner_s_own_metacharacters(self) -> None:
        """`""`, parens, a `;` and a `'` inside an identifier are not syntax."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "t.sql", _HOSTILE_IDENTIFIER_SQL)
            self.assertEqual(ep.count_postgres_sql_records(path), 2)

    def test_unreadable_row_sources_fail_closed(self) -> None:
        cases = {
            "select.sql": 'INSERT INTO "t" ("id") SELECT id FROM other;\n',
            "truncate.sql": 'TRUNCATE TABLE "t";\nINSERT INTO "t" ("id") VALUES (1);\n',
            "copy_file.sql": "COPY \"t\" FROM '/tmp/more.csv';\n",
            "no_terminator.sql": 'COPY "t" FROM STDIN;\n1\tone\n',
            "conflict.sql": (
                'INSERT INTO "t" ("id") VALUES (1) ON CONFLICT DO NOTHING;\n'
            ),
            # The reserved-word fix must not become a licence to skip clauses:
            # every one of these is still unreadable, now WITH quoted names.
            "quoted_select.sql": 'INSERT INTO "values" ("id") SELECT id FROM other;\n',
            "quoted_conflict.sql": (
                'INSERT INTO "values" ("id") VALUES (1) '
                'ON CONFLICT ("vuid") DO NOTHING;\n'
            ),
            "quoted_returning.sql": (
                'INSERT INTO "values" ("id") VALUES (1) RETURNING "vuid";\n'
            ),
            "unterminated_identifier.sql": 'INSERT INTO "values ("id") VALUES (1);\n',
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, sql in cases.items():
                with self.subTest(name):
                    with self.assertRaises(ep.CensusError):
                        ep.count_postgres_sql_records(_write(Path(tmp) / name, sql))


# ---------------------------------------------------------------------------
# S3 prefix + REST pages
# ---------------------------------------------------------------------------

class TestPrefixCounters(unittest.TestCase):
    def test_s3_sums_every_part_and_rejects_foreign_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "orders"
            _write(prefix / "part-00000.jsonl", '{"a":1}\n{"a":2}\n')
            _write(prefix / "part-00001.jsonl", '{"a":3}\n')
            self.assertEqual(ep.count_s3_prefix_records(prefix), 3)
            _write(prefix / "part-00002.json", '{"a":4}')
            with self.assertRaises(ep.CensusError):
                ep.count_s3_prefix_records(prefix)

    def test_empty_prefix_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "orders"
            prefix.mkdir()
            with self.assertRaises(ep.CensusError):
                ep.count_s3_prefix_records(prefix)

    def test_rest_counts_records_not_pages_and_ignores_a_lying_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "events"
            _write(d / "page_0001.json", json.dumps({"data": [{"i": 1}, {"i": 2}], "total_rows": 99}))
            _write(d / "page_0002.json", json.dumps({"data": [{"i": 3}], "total_rows": 99}))
            # index.json lists ONE page and claims a wrong row count: a counter
            # that trusted it would report 2 (or 99); the records say 3.
            _write(d / "index.json", json.dumps({"pages": ["page_0001.json"], "row_count": 99}))
            self.assertEqual(ep.count_rest_page_records(d), 3)

    def test_rest_orders_pages_numerically_and_rejects_strays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "events"
            _write(d / "page_0009.json", json.dumps([{"i": 1}]))
            _write(d / "page_0010.json", json.dumps([{"i": 2}, {"i": 3}]))
            self.assertEqual(ep.count_rest_page_records(d), 3)
            _write(d / "extra_rows.json", json.dumps([{"i": 4}]))
            with self.assertRaises(ep.CensusError):
                ep.count_rest_page_records(d)

    def test_rest_page_without_an_unambiguous_row_list_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "events"
            _write(d / "page_0001.json", json.dumps({"count": 3}))
            with self.assertRaises(ep.CensusError):
                ep.count_rest_page_records(d)
            _write(d / "page_0001.json", json.dumps({"data": [], "rows": [{"i": 1}]}))
            with self.assertRaises(ep.CensusError):
                ep.count_rest_page_records(d)


# ---------------------------------------------------------------------------
# Artifact discovery: inferred from disk, never from the backend assignment
# ---------------------------------------------------------------------------

class TestArtifactResolution(unittest.TestCase):
    def _tree(self, root: Path) -> Path:
        rendered = root / "rendered"
        _write(rendered / "files" / "a.csv", "id\n1\n")
        _write(rendered / "postgres" / "b.sql", 'INSERT INTO "b" ("id") VALUES (1);\n')
        _write(rendered / "mongodb" / "c.jsonl", '{"id":1}\n')
        _write(rendered / "rest" / "d" / "page_0001.json", json.dumps({"data": [{"id": 1}]}))
        _write(rendered / "s3" / "e" / "part-00000.jsonl", '{"id":1}\n')
        return rendered

    def test_every_format_resolves_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = self._tree(Path(tmp))
            expected = {
                "a": ep.ArtifactFormat.CSV,
                "b": ep.ArtifactFormat.POSTGRES_SQL,
                "c": ep.ArtifactFormat.JSONL,
                "d": ep.ArtifactFormat.REST_PAGES,
                "e": ep.ArtifactFormat.S3_JSONL_PREFIX,
            }
            for table, fmt in expected.items():
                path, found = ep.resolve_artifact(rendered, table)
                self.assertIs(found, fmt)
                self.assertEqual(ep.count_artifact(path, found), 1)

    def test_missing_and_ambiguous_artifacts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = self._tree(Path(tmp))
            with self.assertRaises(ep.CensusError):
                ep.resolve_artifact(rendered, "nope")
            # The same table rendered twice: the loader would silently read
            # whichever its candidate list hit first; the census refuses.
            _write(rendered / "mongodb" / "a.jsonl", '{"id":1}\n')
            with self.assertRaises(ep.CensusError):
                ep.resolve_artifact(rendered, "a")

    def test_a_reserved_word_table_resolves_and_counts_end_to_end(self) -> None:
        """Discovery + counting for the schemapile shape, in one pass."""
        with tempfile.TemporaryDirectory() as tmp:
            rendered = Path(tmp) / "rendered"
            _write(rendered / "postgres" / "values.sql", _RESERVED_WORD_SQL)
            path, fmt = ep.resolve_artifact(rendered, "values")
            self.assertIs(fmt, ep.ArtifactFormat.POSTGRES_SQL)
            self.assertEqual(ep.count_artifact(path, fmt), 2)

    def test_a_reserved_word_table_rendered_twice_still_fails_closed(self) -> None:
        """Teaching the scanner one format must not soften artifact discovery."""
        with tempfile.TemporaryDirectory() as tmp:
            rendered = Path(tmp) / "rendered"
            _write(rendered / "postgres" / "values.sql", _RESERVED_WORD_SQL)
            _write(rendered / "files" / "values.csv", "vuid\n100\n101\n")
            with self.assertRaises(ep.CensusError):
                ep.resolve_artifact(rendered, "values")

    def test_nested_single_file_layout_is_understood(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = Path(tmp) / "rendered"
            _write(rendered / "postgres" / "t" / "load.sql", 'INSERT INTO "t" ("id") VALUES (1),(2);')
            path, fmt = ep.resolve_artifact(rendered, "t")
            self.assertIs(fmt, ep.ArtifactFormat.POSTGRES_SQL)
            self.assertEqual(ep.count_artifact(path, fmt), 2)


# ---------------------------------------------------------------------------
# End to end over a real rendered workspace
# ---------------------------------------------------------------------------

class TestCensusEndToEnd(unittest.TestCase):
    """The census over rendered artifacts vs the trusted loader's counts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.workspace = Path(cls._tmp.name)
        cls.task = demo_task()
        cls.pops = (P.DEVELOPMENT, P.COUNTERFACTUAL)
        for pop in cls.pops:
            rows = generate_rows(cls.task, pop)
            pop_dir = cls.workspace / "tasks" / cls.task.task_id / "populations" / pop.value
            write_rows(rows, pop_dir / "rows")
            render_population(cls.task, pop, rows, pop_dir / "rendered")

    def _loader_counts(self, pop: P) -> dict[str, int]:
        rendered = (
            self.workspace / "tasks" / self.task.task_id / "populations" / pop.value / "rendered"
        )
        con = duckdb.connect(":memory:")
        try:
            return dict(load_sources_duckdb(self.task, rendered, con).counts)
        finally:
            con.close()

    def _census(self, pop: P) -> dict[str, int]:
        root = self.workspace / "tasks" / self.task.task_id
        counted = ep.census_population(self.task, root, pop)
        return {t: c.records for t, c in counted.items()}

    def test_census_agrees_with_the_trusted_loader_on_clean_artifacts(self) -> None:
        for pop in self.pops:
            with self.subTest(pop.value):
                self.assertEqual(self._census(pop), self._loader_counts(pop))

    def test_census_records_the_artifact_and_format_behind_every_count(self) -> None:
        root = self.workspace / "tasks" / self.task.task_id
        counted = ep.census_population(self.task, root, P.DEVELOPMENT)
        self.assertEqual({t.name for t in self.task.tables}, set(counted))
        for table, entry in counted.items():
            self.assertTrue((root / entry.artifact).exists(), entry.artifact)
            self.assertIn(f"/{P.DEVELOPMENT.value}/", entry.artifact)
            self.assertIsInstance(entry.records, int)
            self.assertTrue(entry.artifact.endswith(table) or table in entry.artifact)

    def test_a_dropped_row_makes_the_census_disagree(self) -> None:
        """The detection this gate exists for, one format at a time."""
        pop = P.DEVELOPMENT
        root = self.workspace / "tasks" / self.task.task_id
        rendered = root / "populations" / pop.value / "rendered"
        baseline = self._census(pop)
        mutations = {
            "csv": (rendered / "files" / "order_items.csv", "order_items"),
            "jsonl": (rendered / "mongodb" / "orders.jsonl", "orders"),
        }
        for label, (path, table) in mutations.items():
            with self.subTest(label):
                original = path.read_text(encoding="utf-8")
                lines = original.rstrip("\n").split("\n")
                keep = lines[:-1] if label == "jsonl" else lines[:1] + lines[2:]
                path.write_text("\n".join(keep) + "\n", encoding="utf-8")
                try:
                    after = self._census(pop)
                    self.assertEqual(after[table], baseline[table] - 1)
                finally:
                    path.write_text(original, encoding="utf-8")
        self.assertEqual(self._census(pop), baseline)

    def test_recorded_evidence_matches_the_gate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            src = self.workspace / "tasks" / self.task.task_id
            dst = ws / "tasks" / self.task.task_id
            dst.mkdir(parents=True)
            for pop in self.pops:
                target = dst / "populations" / pop.value
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(src / "populations" / pop.value, target_is_directory=True)
            task = self.task.model_copy(
                update={"populations": tuple(p for p in self.task.populations if p.name in self.pops)}
            )
            path = ep.record_artifact_census(ws, task, gold="deliberately unread")
            self.assertEqual(path.relative_to(dst).as_posix(), ep.EL_CENSUS_EVIDENCE_REL)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["kind"], "el-artifact-census")
            self.assertEqual(record["task_id"], task.task_id)
            self.assertEqual(record["task_content_hash"], task.content_hash())
            self.assertEqual(record["counter_version"], ep.COUNTER_VERSION)
            self.assertIn("elt_taskgen.reference.solution", record["independent_of"])
            for pop in self.pops:
                entry = record["populations"][pop.value]
                self.assertEqual(set(entry), {t.name for t in task.tables})
                for table_entry in entry.values():
                    self.assertEqual(
                        set(table_entry), {"artifact", "format", "records"}
                    )
            # Deterministic: the same inputs write the same bytes.
            first = path.read_bytes()
            ep.record_artifact_census(ws, task)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(ep.load_artifact_census(ws, task.task_id), record)

    def test_missing_population_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "tasks" / self.task.task_id).mkdir(parents=True)
            with self.assertRaises(ep.CensusError):
                ep.build_artifact_census(ws, self.task)


class TestCensusSatisfiesTheGate(unittest.TestCase):
    """The producer against its real consumer, both directions.

    Calls the EL gate functions directly (they are module-private in gates.py,
    whose public entry point additionally needs measured attack rewards): the
    claim under test is precisely that THIS evidence closes THOSE gates, and a
    reimplementation of the reconciliation here would be the self-certification
    fiction all over again.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.workspace = Path(cls._tmp.name)
        base = demo_task()
        # Same five populations, smaller scale: this test is about the gate
        # handshake, and the stress population's 63k rows only buy runtime.
        cls.task = base.model_copy(
            update={
                "populations": tuple(
                    spec.model_copy(
                        update={"scale": {t: min(n, 40) for t, n in spec.scale.items()}}
                    )
                    for spec in base.populations
                )
            }
        )
        cls.root = cls.workspace / "tasks" / cls.task.task_id
        stage1: dict[str, dict[str, int]] = {}
        for spec in cls.task.populations:
            pop = spec.name
            rows = generate_rows(cls.task, pop)
            pop_dir = cls.root / "populations" / pop.value
            write_rows(rows, pop_dir / "rows")
            render_population(cls.task, pop, rows, pop_dir / "rendered")
            con = duckdb.connect(":memory:")
            try:
                # The gold IS whatever the trusted loader loaded — the very
                # self-certification this census exists to break.
                stage1[pop.value] = dict(
                    load_sources_duckdb(cls.task, pop_dir / "rendered", con).counts
                )
            finally:
                con.close()
        cls.gold = GoldBundle(
            task_id=cls.task.task_id,
            task_content_hash=cls.task.content_hash(),
            stage1=stage1,
            stage2_csv={p: {"customer_summary": "customer_id\n1\n"} for p in stage1},
            file_hashes={},
        )

    def _verdicts(self) -> dict[str, tuple[bool, str]]:
        return {
            "el-artifact-census": (
                lambda r: (r.passed, r.details)
            )(gates._gate_el_artifact_census(self.task, self.workspace, self.gold)),
            "trusted-solution": (
                lambda r: (r.passed, r.details)
            )(gates._gate_el_trusted_solution(self.task, self.workspace, self.gold)),
        }

    def test_absent_census_is_a_failure_never_a_skip(self) -> None:
        path = self.root / ep.EL_CENSUS_EVIDENCE_REL
        path.unlink(missing_ok=True)
        for gate, (passed, details) in self._verdicts().items():
            with self.subTest(gate):
                self.assertFalse(passed)
                self.assertIn(ep.EL_CENSUS_EVIDENCE_REL, details)

    def test_recorded_census_closes_both_el_gates(self) -> None:
        ep.record_artifact_census(self.workspace, self.task, self.gold)
        try:
            for gate, (passed, details) in self._verdicts().items():
                with self.subTest(gate):
                    self.assertTrue(passed, details)
        finally:
            (self.root / ep.EL_CENSUS_EVIDENCE_REL).unlink(missing_ok=True)

    def test_a_corrupted_artifact_makes_both_el_gates_fail(self) -> None:
        csv_path = (
            self.root / "populations" / P.PRIMARY.value / "rendered" / "files" / "order_items.csv"
        )
        original = csv_path.read_text(encoding="utf-8")
        lines = original.rstrip("\n").split("\n")
        csv_path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
        try:
            ep.record_artifact_census(self.workspace, self.task, self.gold)
            for gate, (passed, details) in self._verdicts().items():
                with self.subTest(gate):
                    self.assertFalse(passed, "a dropped row must not pass")
                    self.assertIn("three-legged reconciliation", details)
                    self.assertIn("order_items", details)
        finally:
            csv_path.write_text(original, encoding="utf-8")
            (self.root / ep.EL_CENSUS_EVIDENCE_REL).unlink(missing_ok=True)

    def test_stale_census_is_rejected_by_identity_binding(self) -> None:
        ep.record_artifact_census(self.workspace, self.task, self.gold)
        path = self.root / ep.EL_CENSUS_EVIDENCE_REL
        record = json.loads(path.read_text(encoding="utf-8"))
        record["task_content_hash"] = "0" * 64
        path.write_text(json.dumps(record), encoding="utf-8")
        try:
            for gate, (passed, details) in self._verdicts().items():
                with self.subTest(gate):
                    self.assertFalse(passed)
                    self.assertIn("STALE", details)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

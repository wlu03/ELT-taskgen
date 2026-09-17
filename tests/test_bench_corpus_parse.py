"""Tests for verification/corpus.py and the `bench-verify-corpus` CLI verb.

WHY THIS EXISTS
The three world_development_indicators data_model.yaml copies in the pinned
ELT-Bench checkout carried an unquoted colon-space scalar that no strict YAML
parser accepts (IR-010).  Nothing failed loudly: the anchor adapter repaired
the file silently and the runtime evaluator only strict-parsed at evaluation
time, after the warehouse had been touched.  These tests pin the corpus gate's
behavior on synthetic corpora, hold the REAL pinned checkout to zero
strict-parse failures on every destination, and keep the specific wdi offender
fixed (strict parse + cross-destination byte identity).
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from elt_taskgen import cli
from elt_taskgen.verification import corpus

BENCH_ROOT = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-Bench")

needs_bench = unittest.skipUnless(
    (BENCH_ROOT / "elt-bench" / "snowflake").is_dir(),
    "pinned ELT-Bench checkout not available",
)

#: The exact defect shape of IR-010: an unquoted plain scalar whose embedded
#: `: ` (after 'Health) terminates the mapping value mid-prose.
BROKEN_YAML = (
    "models:\n"
    "  - name: indicator\n"
    "    columns:\n"
    "      - name: min_value\n"
    "        description: The lowest value of the indicator belongs to "
    "'Health: Population: Structure' in the period 1960 to 1965.\n"
)


class TestStrictParseFailures(unittest.TestCase):
    """Unit tests over synthetic corpora — always run, no checkout needed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_flags_unquoted_colon_space_yaml(self):
        path = self.tmp / "data_model.yaml"
        path.write_text(BROKEN_YAML, encoding="utf-8")
        failures = corpus.strict_parse_failures(self.tmp)
        self.assertEqual([p for p, _ in failures], [path])
        message = failures[0][1]
        self.assertIn("ScannerError", message)
        self.assertNotIn("\n", message)

    def test_flags_truncated_json(self):
        path = self.tmp / "table.json"
        path.write_text('{"customers": [1, 2', encoding="utf-8")
        failures = corpus.strict_parse_failures(self.tmp)
        self.assertEqual([p for p, _ in failures], [path])
        self.assertIn("JSONDecodeError", failures[0][1])

    def test_clean_pair_passes_and_other_suffixes_are_ignored(self):
        (self.tmp / "config.yaml").write_text("database: wdi\n", encoding="utf-8")
        (self.tmp / "table.json").write_text('{"rows": 3}\n', encoding="utf-8")
        # A colon-space in a .sql file is not the gate's business.
        (self.tmp / "notes.sql").write_text("-- key: value: value\n", encoding="utf-8")
        self.assertEqual(corpus.strict_parse_failures(self.tmp), [])
        self.assertEqual(corpus.count_artifacts(self.tmp), 2)

    def test_offender_list_is_sorted(self):
        for name in ("b.yaml", "a.yaml"):
            (self.tmp / name).write_text(BROKEN_YAML, encoding="utf-8")
        failures = corpus.strict_parse_failures(self.tmp)
        self.assertEqual([p.name for p, _ in failures], ["a.yaml", "b.yaml"])

    def test_non_utf8_artifact_is_a_failure_not_a_skip(self):
        (self.tmp / "config.yaml").write_bytes(b"database: \xff\xfe\n")
        failures = corpus.strict_parse_failures(self.tmp)
        self.assertEqual(len(failures), 1)
        self.assertIn("DecodeError", failures[0][1])

    def test_empty_corpus_counts_zero(self):
        self.assertEqual(corpus.count_artifacts(self.tmp), 0)
        self.assertEqual(corpus.strict_parse_failures(self.tmp), [])


@needs_bench
class TestPinnedCorpusGate(unittest.TestCase):
    """The release/runtime gate over the REAL pinned checkout: every committed
    YAML/JSON artifact on every destination must strict-parse, and the sweep
    must have seen a non-empty corpus (zero-over-zero is not a pass)."""

    def test_all_destination_dirs_exist(self):
        for name in ("snowflake", "databricks", "redshift"):
            with self.subTest(destination=name):
                self.assertTrue(
                    (BENCH_ROOT / "elt-bench" / name).is_dir(),
                    f"pinned checkout is missing elt-bench/{name}/",
                )

    def test_elt_bench_tree_strict_parses(self):
        root = BENCH_ROOT / "elt-bench"
        self.assertGreater(corpus.count_artifacts(root), 0)
        failures = corpus.strict_parse_failures(root)
        self.assertEqual(
            failures,
            [],
            "committed ELT-Bench artifacts stopped strict-parsing:\n"
            + "\n".join(f"{path}: {message}" for path, message in failures),
        )

    def test_evaluation_tree_strict_parses(self):
        failures = corpus.strict_parse_failures(BENCH_ROOT / "evaluation")
        self.assertEqual(
            failures,
            [],
            "\n".join(f"{path}: {message}" for path, message in failures),
        )


@needs_bench
class TestWorldDevelopmentIndicatorsRegression(unittest.TestCase):
    """IR-010's specific offender stays fixed: each destination copy must pass
    plain yaml.safe_load (NO tolerant repair) with the intended prose, and the
    three copies must stay byte-identical (the RUNTIME_STATUS invariant)."""

    DESTINATIONS = ("snowflake", "databricks", "redshift")
    PROSE = (
        "The lowest value of the indicator belongs to "
        "'Health: Population: Structure' in the period 1960 to 1965."
    )

    def _path(self, destination: str) -> Path:
        return (
            BENCH_ROOT
            / "elt-bench"
            / destination
            / "world_development_indicators"
            / "data_model.yaml"
        )

    def test_strict_parse_recovers_the_quoted_prose(self):
        for destination in self.DESTINATIONS:
            with self.subTest(destination=destination):
                doc = yaml.safe_load(
                    self._path(destination).read_text(encoding="utf-8")
                )
                column = doc["models"][0]["columns"][4]
                self.assertEqual(
                    column["name"], "lowest_value_indicator_HPS_1960_to_1965"
                )
                self.assertEqual(column["description"], self.PROSE)

    def test_all_three_copies_are_byte_identical(self):
        contents = {d: self._path(d).read_bytes() for d in self.DESTINATIONS}
        self.assertEqual(contents["snowflake"], contents["databricks"])
        self.assertEqual(contents["snowflake"], contents["redshift"])


class TestBenchVerifyCorpusCli(unittest.TestCase):
    """`bench-verify-corpus` through cli.main: exit codes and printed lines."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write_corpus(self) -> Path:
        task = self.tmp / "elt-bench" / "snowflake" / "wdi"
        task.mkdir(parents=True)
        (task / "config.yaml").write_text("database: wdi\n", encoding="utf-8")
        (task / "data_model.yaml").write_text(
            'models:\n- name: indicator\n  description: "clean"\n',
            encoding="utf-8",
        )
        return task

    def _run(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["bench-verify-corpus", "--bench-root", str(self.tmp)]
            )
        return code, out.getvalue(), err.getvalue()

    def test_clean_corpus_exits_zero(self):
        self._write_corpus()
        code, out, err = self._run()
        self.assertEqual(code, 0)
        self.assertIn("scanned 2 artifacts, 0 failures", out)
        self.assertEqual(err, "")

    def test_offender_exits_two_and_names_the_file(self):
        task = self._write_corpus()
        (task / "data_model.yaml").write_text(BROKEN_YAML, encoding="utf-8")
        code, out, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn("elt-bench/snowflake/wdi/data_model.yaml", err)
        self.assertIn("ScannerError", err)
        self.assertIn("scanned 2 artifacts, 1 failures", out)

    def test_evaluation_tree_is_swept_too(self):
        self._write_corpus()
        evaluation = self.tmp / "evaluation"
        evaluation.mkdir()
        (evaluation / "table.json").write_text('{"broken": ', encoding="utf-8")
        code, _, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn("evaluation/table.json", err)

    def test_missing_elt_bench_dir_exits_two(self):
        code, _, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIn("elt-bench", err)

    def test_no_destination_dir_exits_two(self):
        (self.tmp / "elt-bench" / "elsewhere").mkdir(parents=True)
        code, _, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIn("destination", err)

    def test_zero_artifacts_exits_two(self):
        (self.tmp / "elt-bench" / "snowflake").mkdir(parents=True)
        code, _, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIn("empty corpus", err)


if __name__ == "__main__":
    unittest.main()

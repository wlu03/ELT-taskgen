"""The mandatory real-data DuckDB gate (review: "Non-negotiable DuckDB test
rule", items 1-9).

Two lanes over ONE scorer (semantic.score_semantic_submission — the code under
test is never reimplemented here):

  Lane A (SemanticGateFixture) — always required, never skipped.  A COMMITTED
  schema-3.3 combined release frozen from the hand-authored five-backend probe
  task (tests/semantic_gate_fixture.py) by tests/make_semantic_gate_fixture.py.
  Loading it runs full release verification on every gate run, so a
  CENSUS_VERSION / scorer / schema bump fails this lane loudly BY DESIGN and
  demands a reviewed fixture re-freeze.

  Lane B (SemanticGateCorpus) — every frozen release under runs/ (or
  $ELT_TASKGEN_RELEASE_ROOT), read-only.  A release at schema >= 3.2 gets the
  full battery with NO skip path.  A legacy freeze (schema < 3.2 — today all
  five drives, frozen at schema 2.0 under census v1) is SKIPPED with its
  manifest header quoted, unless ELT_TASKGEN_SEMANTIC_GATE=require, which
  turns those skips into failures (flip it in CI once the drives are
  re-frozen).  The lane's battery code itself is exercised for real against
  the Lane-A fixture, so it cannot rot while the drives await regen.

Item 9: every scoring run appends a metadata record (release id, source/gold
digests from the manifest, scorer/comparator/census/duckdb versions, result
digest, wall clock, child ru_maxrss); tearDownModule writes them to
$ELT_TASKGEN_GATE_REPORT (default: <tempdir>/semantic_gate_report.json) — the
citable gate evidence.

Gold discipline (item 6): this module NEVER calls generate_rows,
run_reference, freeze_gold, or build_reference.  Reference SQL comes from the
frozen answer_key bytes; expected gold is the hand-derived EXPECTED_GOLD.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import platform
import resource
import sys
import tempfile
import time
import unittest
from pathlib import Path

import duckdb

try:
    import semantic_gate_fixture as fixture
except ImportError:  # running as tests.test_semantic_gate from the repo root
    from tests import semantic_gate_fixture as fixture

from elt_taskgen.export import eltbench, release as release_mod
from elt_taskgen.models import PopulationName, canonical_json
from elt_taskgen.reference.solution import find_rendered_artifact
from elt_taskgen.semantic import (
    SEMANTIC_SCORER_VERSION,
    SEMANTIC_SUBMISSION_SCHEMA_VERSION,
    SemanticHarnessError,
    SemanticLimits,
    SemanticPackageError,
    load_semantic_package,
    parse_semantic_submission,
    score_semantic_submission,
    score_semantic_text,
)
from elt_taskgen.verification import gates, strict_diagnostic

P = PopulationName

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"

RELEASE_ROOT_ENV = "ELT_TASKGEN_RELEASE_ROOT"
GATE_REQUIRE_ENV = "ELT_TASKGEN_SEMANTIC_GATE"
GATE_REPORT_ENV = "ELT_TASKGEN_GATE_REPORT"

#: ru_maxrss is reported in bytes on darwin and KiB on linux.
_RU_MAXRSS_SCALE = 1 if sys.platform == "darwin" else 1024

#: Generous runaway ceilings for the concurrency probe (item 8b) — these are
#: tripwires against a hung or leaking worker, not performance targets.
_CONCURRENCY_WALL_CEILING_SECONDS = 300.0
_CONCURRENCY_RSS_CEILING_BYTES = 4 * 1024 * 1024 * 1024

#: Item-9 metadata records, one per (release, task, population, check).
_GATE_RECORDS: list[dict] = []


def _schema_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return (-1,)


def _result_digest(result) -> str:
    return hashlib.sha256(
        canonical_json(result.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def _reference_load_plan(package) -> dict[str, dict[str, str]]:
    """Load plan naming the FROZEN rendered artifacts, backend-typed readers."""
    root = package.source_root(P.PRIMARY)
    plan: dict[str, dict[str, str]] = {}
    for table in package.task.tables:
        artifact = find_rendered_artifact(package.task, root, table.name)
        backend = package.task.backend_for(table.name).backend.value
        plan[table.name] = {
            "path": artifact.relative_to(root).as_posix(),
            "format": fixture.BACKEND_FORMATS[backend],
        }
    return plan


def _frozen_reference_sql(release_dir: Path, task) -> dict[str, str]:
    """The trusted SQL exactly as frozen in the answer key — never rebuilt."""
    reference_dir = release_dir / "private" / task.task_id / "answer_key" / "reference"
    return {
        mart.name: (reference_dir / f"{mart.name}.sql").read_text(encoding="utf-8")
        for mart in task.marts
    }


def _submission_text(package, release_dir: Path, *, plan=None, sql=None) -> str:
    return json.dumps(
        {
            "schema_version": SEMANTIC_SUBMISSION_SCHEMA_VERSION,
            "task_id": package.task.task_id,
            "load_plan": plan if plan is not None else _reference_load_plan(package),
            "sql_by_mart": (
                sql if sql is not None else _frozen_reference_sql(release_dir, package.task)
            ),
        }
    )


def _nonterminating_sql(mart) -> str:
    """A never-finishing query producing exactly the mart's column names."""
    aliases = ", ".join(f'max(x) AS "{column.name}"' for column in mart.columns)
    return (
        "WITH RECURSIVE loop(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM loop) "
        f"SELECT {aliases} FROM loop"
    )


def _timed_score(package, submission, **kwargs):
    """(result, wall_seconds, ru_maxrss_children after) for one scoring run."""
    start = time.monotonic()
    result = score_semantic_submission(package, submission, **kwargs)
    wall = time.monotonic() - start
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return result, wall, rss


def _record(package, result, *, check: str, wall_seconds: float, ru_maxrss: int) -> None:
    """Append one item-9 record per scored population."""
    manifest = package.manifest
    tid = package.task.task_id
    digest = _result_digest(result)
    gold_prefix = f"private/{tid}/answer_key/"
    for population in sorted(result.populations):
        source_prefix = f"private/{tid}/populations/{population}/"
        _GATE_RECORDS.append(
            {
                "check": check,
                "release_id": manifest.release_id,
                "release_schema": manifest.schema_version,
                "task_id": tid,
                "population": population,
                "source_digests": {
                    rel: digest_
                    for rel, digest_ in sorted(manifest.checksums.items())
                    if rel.startswith(source_prefix)
                },
                "gold_digests": {
                    rel: digest_
                    for rel, digest_ in sorted(manifest.checksums.items())
                    if rel.startswith(gold_prefix)
                },
                "scorer_version": SEMANTIC_SCORER_VERSION,
                "comparator_version": manifest.scorer_version,
                "gates_scorer_version": gates.SCORER_VERSION,
                "census_version": eltbench.CENSUS_VERSION,
                "duckdb_version": duckdb.__version__,
                "python_version": platform.python_version(),
                "platform": sys.platform,
                "result_digest": digest,
                "wall_seconds": round(wall_seconds, 3),
                "ru_maxrss_children": ru_maxrss,
                "ru_maxrss_unit": "bytes" if _RU_MAXRSS_SCALE == 1 else "KiB",
            }
        )


def tearDownModule() -> None:
    """Item 9: persist the citable gate evidence and print where it went."""
    path = Path(
        os.environ.get(GATE_REPORT_ENV)
        or Path(tempfile.gettempdir()) / "semantic_gate_report.json"
    )
    payload = {
        "generated_by": "tests/test_semantic_gate.py",
        "scorer_version": SEMANTIC_SCORER_VERSION,
        "gates_scorer_version": gates.SCORER_VERSION,
        "census_version": eltbench.CENSUS_VERSION,
        "duckdb_version": duckdb.__version__,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "records": sorted(
            _GATE_RECORDS,
            key=lambda r: (
                str(r.get("release_id", "")),
                str(r.get("task_id", "")),
                str(r.get("population", "")),
                str(r.get("check", "")),
            ),
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\n[semantic-gate] evidence report written to {path}")


def _assert_reference_perfect(testcase: unittest.TestCase, result) -> None:
    """Gate item 4: the trusted attempt earns exactly 1.0 everywhere graded."""
    testcase.assertTrue(result.valid_submission)
    testcase.assertEqual(result.error_code, "")
    testcase.assertEqual(result.semantic_el_reward, 1.0)
    testcase.assertEqual(result.semantic_t_reward, 1.0)
    testcase.assertEqual(result.reward, 1.0)
    testcase.assertEqual(
        set(result.populations),
        {population.value for population in gates.GRADED_POPULATIONS},
    )
    for name, score in result.populations.items():
        testcase.assertTrue(score.stage1_pass, name)
        testcase.assertEqual(score.el_reward, 1.0, name)
        testcase.assertEqual(score.t_reward, 1.0, name)
        testcase.assertEqual(
            score.mart_scores, {m: True for m in score.mart_scores}, name
        )
        testcase.assertTrue(score.mart_scores, name)


def _assert_gold_frozen(
    testcase: unittest.TestCase, release_dir: Path, package
) -> None:
    """Gate item 6: gold bytes re-hash to the manifest; identity is hash-bound."""
    manifest = package.manifest
    tid = package.task.task_id
    answer_key = release_dir / "private" / tid / "answer_key"
    testcase.assertTrue(answer_key.is_dir())
    checked = 0
    for path in sorted(answer_key.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(release_dir).as_posix()
        testcase.assertIn(rel, manifest.checksums, rel)
        testcase.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            manifest.checksums[rel],
            f"{rel}: frozen gold bytes changed",
        )
        checked += 1
    testcase.assertGreater(checked, 0)
    testcase.assertEqual(package.gold.task_id, tid)
    testcase.assertEqual(package.gold.task_content_hash, manifest.tasks[tid])
    testcase.assertEqual(package.task.content_hash(), manifest.tasks[tid])


def _run_release_battery(
    testcase: unittest.TestCase, release_dir: Path
) -> None:
    """The no-skip battery for one schema >= 3.2 release (items 1/4/5/6/8a).

    A SemanticPackageError or SemanticHarnessError here is a FAILURE — harness
    integrity is what this gate certifies — never a skip.
    """
    header = json.loads(
        (release_dir / "release_manifest.json").read_text(encoding="utf-8")
    )
    for task_id in sorted(header.get("tasks") or {}):
        try:
            package = load_semantic_package(release_dir, task_id)
        except (SemanticPackageError, SemanticHarnessError) as exc:
            testcase.fail(
                f"{release_dir}: task {task_id!r} does not load as a verified "
                f"semantic package: {exc}"
            )
        # Item 1: verified schema-3.2+ combined package (load_semantic_package
        # already ran verify_release; these pins document what it proved).
        testcase.assertGreaterEqual(
            _schema_tuple(package.manifest.schema_version), (3, 2)
        )
        testcase.assertEqual(
            package.manifest.corpus_profile, release_mod.COMBINED_CORPUS_PROFILE
        )
        testcase.assertEqual(
            package.manifest.semantic_scorer_version, SEMANTIC_SCORER_VERSION
        )
        # Item 6.
        _assert_gold_frozen(testcase, release_dir, package)
        # Item 4 + 8a: the frozen reference attempt, scored twice.
        text = _submission_text(package, release_dir)
        submission = parse_semantic_submission(package.task, text)
        first, wall_1, rss_1 = _timed_score(package, submission)
        try:
            _assert_reference_perfect(testcase, first)
        except AssertionError as exc:
            raise AssertionError(
                f"{release_dir}: task {task_id!r}: {exc}"
            ) from exc
        second, wall_2, rss_2 = _timed_score(package, submission)
        testcase.assertEqual(
            canonical_json(first.model_dump(mode="json")),
            canonical_json(second.model_dump(mode="json")),
            f"{release_dir}: task {task_id!r}: repeat runs diverged",
        )
        _record(package, first, check="reference", wall_seconds=wall_1, ru_maxrss=rss_1)
        _record(
            package, second, check="determinism", wall_seconds=wall_2, ru_maxrss=rss_2
        )
        # Item 5: the spawned worker's kill switch on this release's data.
        hostile = dict(_frozen_reference_sql(release_dir, package.task))
        first_mart = package.task.marts[0]
        hostile[first_mart.name] = _nonterminating_sql(first_mart)
        timed_out = score_semantic_text(
            package,
            _submission_text(package, release_dir, sql=hostile),
            populations=(P.PRIMARY,),
            limits=SemanticLimits(timeout_seconds=2.0, memory_limit_mb=128),
        )
        testcase.assertTrue(timed_out.valid_submission)
        testcase.assertEqual(timed_out.reward, 0.0)
        testcase.assertEqual(timed_out.error_code, "execution_timeout")


# ---------------------------------------------------------------------------
# Lane A: the committed fixture release — always required, never skipped.
# ---------------------------------------------------------------------------

class SemanticGateFixture(unittest.TestCase):
    """Items 1-8 on the committed five-backend fixture release."""

    @classmethod
    def setUpClass(cls):
        if not FIXTURE_DIR.is_dir():
            raise AssertionError(
                f"committed gate fixture missing at {FIXTURE_DIR} — run "
                ".venv/bin/python tests/make_semantic_gate_fixture.py "
                "(a reviewed action, not test setup)"
            )
        try:
            # verify=True (default): full release verification on every run —
            # a census/scorer/schema bump MUST fail here (item 1).
            cls.package = load_semantic_package(FIXTURE_DIR, fixture.GATE_TASK_ID)
        except SemanticPackageError as exc:
            raise AssertionError(
                f"fixture release no longer loads as a semantic package: {exc}."
                " If a census/scorer/schema bump was intended, re-freeze the"
                " fixture with tests/make_semantic_gate_fixture.py --force"
                " under review."
            ) from exc
        cls.reference_text = _submission_text(cls.package, FIXTURE_DIR)

    def _reference_payload(self) -> dict:
        return json.loads(self.reference_text)

    def test_gate_task_can_use_an_isolated_runtime_namespace(self):
        task_id = "gate__five_backend_probe_runtime"
        task = fixture.gate_task(task_id)
        self.assertEqual(task.task_id, task_id)
        self.assertEqual(task.family_id, task_id)
        self.assertEqual(task.cluster_id, task_id)
        self.assertEqual(
            {population.seed for population in task.populations},
            {
                fixture.derive_seed(task_id, population.name.value)
                for population in task.populations
            },
        )

    def test_package_is_schema_32_and_verified(self):
        manifest = self.package.manifest
        self.assertGreaterEqual(_schema_tuple(manifest.schema_version), (3, 2))
        self.assertEqual(
            manifest.corpus_profile, release_mod.COMBINED_CORPUS_PROFILE
        )
        self.assertEqual(
            manifest.public_layout, release_mod.COMBINED_PUBLIC_LAYOUT
        )
        self.assertEqual(
            manifest.semantic_scorer_version, SEMANTIC_SCORER_VERSION
        )
        # Explicit re-verification so a failure's detail reaches the report.
        verification = release_mod.verify_release(FIXTURE_DIR)
        self.assertTrue(
            verification.ok,
            "fixture release failed verification: "
            + "; ".join(verification.failures[:5]),
        )
        self.assertEqual(verification.release_id, manifest.release_id)

    def test_all_five_backends_in_one_mixed_task(self):
        task = self.package.task
        self.assertEqual(sorted(self.package.manifest.tasks), [task.task_id])
        backends = {task.backend_for(t.name).backend.value for t in task.tables}
        self.assertEqual(
            backends, {"postgres", "mongodb", "files", "rest", "s3"}
        )

    def test_reference_submission_scores_one_on_every_graded_population(self):
        result, wall, rss = _timed_score(
            self.package,
            parse_semantic_submission(self.package.task, self.reference_text),
        )
        _assert_reference_perfect(self, result)
        self.assertNotIn(P.DEVELOPMENT.value, result.populations)
        _record(self.package, result, check="reference", wall_seconds=wall, ru_maxrss=rss)
        # Trusted-vs-submitted independence: corrupting the SUBMITTED load
        # plan zeroes EL while the trusted rebuild still reconciles and T
        # still scores 1.0.
        payload = self._reference_payload()
        payload["load_plan"]["customers"]["path"] = "postgres/missing.sql"
        broken = score_semantic_text(
            self.package, json.dumps(payload), populations=(P.PRIMARY,)
        )
        score = broken.populations[P.PRIMARY.value]
        self.assertEqual(score.el_reward, 0.0)
        self.assertEqual(score.t_reward, 1.0)
        self.assertEqual(score.el_error_code, "stage1_execution_error")
        self.assertEqual(broken.reward, 0.0)

    def test_scoring_runs_in_the_spawned_worker_with_limits(self):
        payload = self._reference_payload()
        payload["sql_by_mart"][fixture.ROLLUP_MART] = _nonterminating_sql(
            self.package.task.mart(fixture.ROLLUP_MART)
        )
        result = score_semantic_text(
            self.package,
            json.dumps(payload),
            populations=(P.PRIMARY,),
            limits=SemanticLimits(timeout_seconds=2.0, memory_limit_mb=128),
        )
        self.assertTrue(result.valid_submission)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.error_code, "execution_timeout")

    def test_gold_is_frozen_and_content_bound(self):
        _assert_gold_frozen(self, FIXTURE_DIR, self.package)
        # Independent validation: the frozen gold CSV bytes equal the
        # HAND-DERIVED literals — computed from the literal rows by a person,
        # never by the reference pipeline (item 6).
        gold_root = (
            FIXTURE_DIR / "private" / fixture.GATE_TASK_ID / "answer_key" / "gold"
        )
        for population, marts in sorted(fixture.EXPECTED_GOLD.items()):
            with self.subTest(population=population):
                for mart, expected_csv in sorted(marts.items()):
                    frozen = (gold_root / population / f"{mart}.csv").read_text(
                        encoding="utf-8"
                    )
                    self.assertEqual(frozen, expected_csv, f"{population}/{mart}")
                self.assertEqual(
                    self.package.gold.stage2_csv[population], marts
                )
                self.assertEqual(
                    self.package.gold.stage1[population],
                    fixture.EXPECTED_STAGE1[population],
                )

    def test_sensitive_values_are_present_and_survive(self):
        # (a) Inventory: every sensitive class is PRESENT in the frozen
        # rendered artifacts of the primary population (item 7).
        root = self.package.source_root(P.PRIMARY)
        for label, predicate in fixture.SENSITIVE_INVENTORY:
            with self.subTest(inventory=label):
                self.assertTrue(predicate(root), label)
        # (b) Behavior: one targeted probe per class, asserting the FROZEN
        # legacy-comparator verdict.  These pin today's contract; they must
        # only ever move together with a reviewed comparator change.
        for label, mart, probe_sql, expected in fixture.MART_PROBES:
            with self.subTest(probe=label):
                payload = self._reference_payload()
                payload["sql_by_mart"][mart] = probe_sql
                result = score_semantic_text(
                    self.package, json.dumps(payload), populations=(P.PRIMARY,)
                )
                self.assertTrue(result.valid_submission, label)
                score = result.populations[P.PRIMARY.value]
                self.assertEqual(score.el_reward, 1.0, label)
                self.assertEqual(score.t_error_codes, {}, label)
                self.assertIs(score.mart_scores[mart], expected, label)

    def test_strict_source_loader_accepts_every_population(self):
        """The committed gate fixture must obey the live warehouse types."""
        for population in P:
            with self.subTest(population=population.value):
                connection = duckdb.connect(":memory:")
                try:
                    counts = strict_diagnostic.load_sources_duckdb_strict(
                        self.package.task,
                        self.package.source_root(population),
                        connection,
                    )
                finally:
                    connection.close()
                self.assertEqual(
                    counts,
                    fixture.EXPECTED_STAGE1[population.value],
                )

    def test_strict_diagnostic_is_additive_on_real_package_data(self):
        """IR-002 on real data: strict verdicts appear NEXT TO an unchanged
        reward — every legacy field byte-identical to the strict-off run."""
        plain = score_semantic_text(self.package, self.reference_text)
        strict = score_semantic_text(
            self.package, self.reference_text, strict_diagnostic=True
        )
        self.assertTrue(strict.strict_diagnostic_ran)
        self.assertNotEqual(strict.strict_diagnostic_version, "")
        mart_names = {mart.name for mart in self.package.task.marts}
        for name, score in strict.populations.items():
            self.assertEqual(set(score.strict_marts), mart_names, name)
            for mart, diag in score.strict_marts.items():
                self.assertEqual(diag.mart, mart, name)
        plain_dump = plain.model_dump(mode="json")
        strict_dump = strict.model_dump(mode="json")
        for dump in (plain_dump, strict_dump):
            dump.pop("strict_diagnostic_ran")
            dump.pop("strict_diagnostic_version")
            for score in dump["populations"].values():
                score.pop("strict_marts")
                score.pop("strict_shadow_marts")
        self.assertEqual(
            canonical_json(plain_dump), canonical_json(strict_dump)
        )

    def test_repeat_runs_are_deterministic(self):
        submission = parse_semantic_submission(
            self.package.task, self.reference_text
        )
        first, wall_1, rss_1 = _timed_score(self.package, submission)
        second, wall_2, rss_2 = _timed_score(self.package, submission)
        self.assertEqual(
            canonical_json(first.model_dump(mode="json")),
            canonical_json(second.model_dump(mode="json")),
        )
        self.assertEqual(_result_digest(first), _result_digest(second))
        _record(
            self.package, first, check="determinism", wall_seconds=wall_1, ru_maxrss=rss_1
        )
        _record(
            self.package, second, check="determinism", wall_seconds=wall_2, ru_maxrss=rss_2
        )

    def test_four_concurrent_workers(self):
        submission = parse_semantic_submission(
            self.package.task, self.reference_text
        )
        rss_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    score_semantic_submission,
                    self.package,
                    submission,
                    populations=(P.PRIMARY,),
                )
                for _ in range(4)
            ]
            results = [future.result() for future in futures]
        wall = time.monotonic() - start
        rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        digests = {_result_digest(result) for result in results}
        self.assertEqual(len(digests), 1, "concurrent workers disagreed")
        for result in results:
            self.assertTrue(result.valid_submission)
            self.assertEqual(result.reward, 1.0)
            self.assertEqual(
                result.populations[P.PRIMARY.value].mart_scores,
                {m.name: True for m in self.package.task.marts},
            )
        # Generous runaway ceilings only — the measured numbers land in the
        # item-9 report, establishing the IR-007 envelope baseline.
        rss_delta_bytes = max(0, rss_after - rss_before) * _RU_MAXRSS_SCALE
        self.assertLess(wall, _CONCURRENCY_WALL_CEILING_SECONDS)
        self.assertLess(rss_delta_bytes, _CONCURRENCY_RSS_CEILING_BYTES)
        _record(
            self.package,
            results[0],
            check="concurrency-x4",
            wall_seconds=wall,
            ru_maxrss=rss_after,
        )


# ---------------------------------------------------------------------------
# Lane B: every frozen release under runs/ — read-only, skip-if-legacy.
# ---------------------------------------------------------------------------

def _discover_releases() -> tuple[Path, ...]:
    root = Path(os.environ.get(RELEASE_ROOT_ENV) or REPO_ROOT / "runs")
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            manifest.parent
            for manifest in root.glob("*/release/release_manifest.json")
        )
    )


class SemanticGateCorpus(unittest.TestCase):
    """Items 1/4/5/6/8a over every frozen release; item 9 records each run."""

    @classmethod
    def setUpClass(cls):
        cls.releases = _discover_releases()
        if not cls.releases:
            raise unittest.SkipTest(
                f"no frozen releases under {RELEASE_ROOT_ENV} or runs/ on this "
                "machine"
            )

    def test_every_frozen_release_meets_the_semantic_gate(self):
        require = os.environ.get(GATE_REQUIRE_ENV, "") == "require"
        pending: list[str] = []
        ran = 0
        for release_dir in self.releases:
            name = release_dir.parent.name
            header = json.loads(
                (release_dir / "release_manifest.json").read_text(encoding="utf-8")
            )
            schema = str(header.get("schema_version") or "")
            profile = str(header.get("corpus_profile") or "")
            legacy = (
                _schema_tuple(schema) < (3, 2)
                or profile != release_mod.COMBINED_CORPUS_PROFILE
            )
            if legacy:
                message = (
                    f"{name}: legacy schema {schema} ({profile}) freeze "
                    "predates the semantic package; awaiting the planned "
                    "census-v2 re-freeze"
                )
                _GATE_RECORDS.append(
                    {
                        "check": "pending-legacy-refreeze",
                        "release_id": str(header.get("release_id") or name),
                        "release_schema": schema,
                        "detail": message,
                    }
                )
                if require:
                    with self.subTest(release=name):
                        self.fail(message)
                else:
                    pending.append(message)
                continue
            ran += 1
            # NO skip path for a schema >= 3.2 release.
            with self.subTest(release=name):
                _run_release_battery(self, release_dir)
        if pending and not ran and not require:
            self.skipTest("; ".join(pending))

    def test_corpus_battery_runs_green_on_a_modern_release(self):
        """The Lane-B battery itself is exercised against the committed
        schema-3.3 fixture, so its no-skip path cannot rot while the five
        legacy drives await their re-freeze."""
        if not FIXTURE_DIR.is_dir():
            self.fail(f"committed gate fixture missing at {FIXTURE_DIR}")
        _run_release_battery(self, FIXTURE_DIR)


# ---------------------------------------------------------------------------
# The DEV/T witness tools over the committed fixture (roadmap 2.a CLI row:
# "dev_query, dry_run_sql, run_mart_sql_dev join make gate on real benchmark
# data"). Always required, never skipped — the same discipline as Lane A.
# ---------------------------------------------------------------------------

class SemanticGateDevTools(unittest.TestCase):
    """`dev_query`, `dry_run_sql` and `run_mart_sql_dev` over the committed
    five-backend fixture's DEVELOPMENT rows, through `sandboxed_memory_connection`
    and the trusted loaders — Tool -> projection -> D1 gatekeeper end to end.
    Gold discipline (item 6): only the FROZEN reference SQL and the public
    DEVELOPMENT source tree are used; no gold, no generate_rows."""

    @classmethod
    def setUpClass(cls):
        if not FIXTURE_DIR.is_dir():
            raise AssertionError(
                f"committed gate fixture missing at {FIXTURE_DIR} — run "
                ".venv/bin/python tests/make_semantic_gate_fixture.py"
            )
        cls.package = load_semantic_package(FIXTURE_DIR, fixture.GATE_TASK_ID)
        cls.task = cls.package.task
        cls.reference_sql = _frozen_reference_sql(FIXTURE_DIR, cls.task)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        # Lay the DEVELOPMENT rendered SOURCE tree at the canonical workspace
        # location so the witness session materializes its warehouse there.
        from elt_taskgen.reference.runner import rendered_dir

        import shutil

        cls.ws = (Path(cls._tmp.name) / "witness-workspace").resolve()
        rdir = rendered_dir(cls.ws, cls.task.task_id, P.DEVELOPMENT)
        rdir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cls.package.source_root(P.DEVELOPMENT), rdir)

    def _session_and_registry(self):
        from elt_taskgen.review.tools import validators as witness

        session = witness.ImplementerSession.open(self.ws, self.task)
        return session, witness.implementer_registry()

    def test_dev_tools_run_over_the_fixture_through_the_gatekeeper(self):
        from elt_taskgen.review.tools import projection as PJ

        session, registry = self._session_and_registry()
        ctx = session.context()

        def clean(diag):
            payload = PJ.serialize_for_transport(diag, task=self.task)
            PJ.assert_value_free(payload.encode("utf-8"), task=self.task)
            return diag

        # list_schemas: exactly the source table names.
        listed = clean(registry.dispatch(ctx, "list_schemas", {}))
        self.assertEqual(set(listed.names), {t.name for t in self.task.tables})

        # dev_query: a bounded read-only page of the DEVELOPMENT source rows.
        table = self.task.tables[0].name
        rows = registry.dispatch(ctx, "dev_query", {"sql": f"SELECT * FROM {table}"})
        self.assertIsInstance(rows, PJ.DevRows)
        clean(rows)
        self.assertEqual(set(rows.columns), {c.name for c in self.task.tables[0].columns})
        self.assertGreaterEqual(len(rows.rows), 1)
        # A read_* function is refused before it can reach the host.
        blocked = registry.dispatch(ctx, "dev_query", {"sql": f"SELECT * FROM read_csv_auto('{table}')"})
        self.assertEqual((blocked.source, blocked.code), (PJ.DiagnosticSource.DEV_QUERY, "external_access"))

        # dry_run_sql: the frozen reference SQL binds and produces the mart columns.
        for mart in self.task.marts:
            diag = clean(registry.dispatch(ctx, "dry_run_sql", {"mart": mart.name, "sql": self.reference_sql[mart.name]}))
            self.assertEqual(diag.code, "binds", mart.name)
            self.assertTrue(diag.flags["binds"] and diag.flags["columns_match"], mart.name)

        # run_mart_sql_dev: the frozen reference SQL runs over the DEV rows;
        # rows and counts are discarded, only {mart, code} comes back.
        payload = [{"mart": m.name, "sql": self.reference_sql[m.name]} for m in self.task.marts]
        result = clean(registry.dispatch(ctx, "run_mart_sql_dev", {"sql_by_mart": payload}))
        self.assertEqual((result.source, result.code), (PJ.DiagnosticSource.MART_DEV, "ok"))
        self.assertEqual(result.model_dump(mode="json")["flags"], {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for adapters/eltbench_anchor.py and adapters/dlt.py.

WHY THIS EXISTS
The anchor adapter is the pipeline's only window onto the real target
distribution; if it misreads the pinned checkout, selection matches a phantom.
These tests import TWO real anchor dirs from the pinned ELT-Bench checkout
(airline: postgres+s3+flat_files with case-mismatched schema CSVs; app_store:
mongodb+custom_api) and pin the parsed backends, tables, marts, sort keys, and
stage-1 counts. The dlt adapter tests pin the manifest -> TaskIR mapping:
the backend rotation (spread over backends, `rest` always represented), FK
edges from parent endpoints, synthesized schemas, and merge-disposition
validation.
"""

import contextlib
import io
import tempfile
import unittest
import warnings
from pathlib import Path

from elt_taskgen.adapters import dlt as dlt_adapter
from elt_taskgen.adapters import eltbench_anchor as anchor
from elt_taskgen.models import (
    Backend,
    ColumnType,
    Origin,
    PopulationName,
    task_to_json,
)

BENCH_ROOT = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-Bench")

needs_bench = unittest.skipUnless(
    (BENCH_ROOT / "elt-bench" / "snowflake").is_dir(),
    "pinned ELT-Bench checkout not available",
)


def _cli_measure_target_source() -> str:
    import inspect

    from elt_taskgen import cli

    return inspect.getsource(cli.cmd_ingest_anchor)


#: Temporary C2 guards: measure-target must reject short checkouts and hash
#: evaluation SQL. Make these tests unconditional once both behaviors land.
needs_cli_fail_closed = unittest.skipUnless(
    "return 2" in _cli_measure_target_source(),
    "cli.cmd_ingest_anchor fail-closed exit (C2, cli side) not landed yet",
)
needs_cli_reference_sql = unittest.skipUnless(
    "sql_fingerprint" in _cli_measure_target_source(),
    "cli.cmd_ingest_anchor evaluation-SQL arming (C2, cli side) not landed yet",
)


def _run_measure_target(*argv: str) -> tuple[int, str, str]:
    """Invoke the measure-target command in-process; (exit, stdout, stderr)."""
    from elt_taskgen import cli

    args = cli.build_parser().parse_args(["measure-target", *argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


@needs_bench
class TestAirlineAnchor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = anchor.import_anchor_task(BENCH_ROOT, "airline")

    def test_identity_and_non_trainable_marker(self):
        self.assertEqual(self.task.task_id, "eltbench__airline")
        self.assertEqual(self.task.family_id, "eltbench__airline")
        self.assertIs(self.task.origin, Origin.ELTBENCH_ANCHOR)

    def test_backends_from_config_sections(self):
        by_table = {b.table: b.backend for b in self.task.backends}
        self.assertEqual(
            by_table,
            {
                "airlines": Backend.POSTGRES,
                "Airports": Backend.S3,
                "Air_Carriers": Backend.FILES,
            },
        )

    def test_case_insensitive_schema_match_keeps_config_spelling(self):
        # config.yaml says `airlines`; the schema CSV is `Airlines.csv`.
        table = self.task.table("airlines")
        names = [c.name for c in table.columns]
        self.assertIn("FL_DATE", names)
        self.assertIn("OP_CARRIER_AIRLINE_ID", names)
        self.assertEqual(table.column("FL_DATE").description, "flight date")

    def test_marts_and_sort_keys(self):
        self.assertEqual(
            [m.name for m in self.task.marts],
            ["airline__airlines", "airline__airports"],
        )
        self.assertEqual(self.task.mart("airline__airlines").key_columns, ("airline_code",))
        self.assertEqual(self.task.mart("airline__airports").key_columns, ("airport_code",))
        cols = {c.name for c in self.task.mart("airline__airlines").columns}
        self.assertIn("num_planes", cols)

    def test_stage1_counts_as_primary_scale(self):
        primary = self.task.population(PopulationName.PRIMARY)
        self.assertEqual(
            primary.scale,
            {"airlines": 701352, "Airports": 6510, "Air_Carriers": 1656},
        )

    def test_content_hash_deterministic_across_imports(self):
        again = anchor.import_anchor_task(BENCH_ROOT, "airline")
        self.assertEqual(self.task.content_hash(), again.content_hash())

    def test_anchor_columns_are_text_and_shape_fingerprinted(self):
        """Anchors stay TEXT-typed (no upstream type info); the firewall
        therefore relies on the type-blind shape namespace, which a real-typed
        copy of the same schema shares exactly (C2)."""
        from elt_taskgen.models import ColumnSpec, ColumnType
        from elt_taskgen.verification import contamination as cont

        for table in self.task.tables:
            for col in table.columns:
                self.assertIs(col.type, ColumnType.TEXT)
                self.assertTrue(col.nullable)
        anchor_fps = cont.task_fingerprints(self.task)
        self.assertTrue(any(fp.startswith("shape:") for fp in anchor_fps))
        # retype the first column of every table (as a candidate adapter
        # inferring an integer key would); every typed hash diverges, every
        # shape hash is byte-identical
        retyped = tuple(
            t.model_copy(
                update={
                    "columns": tuple(
                        ColumnSpec(
                            name=c.name,
                            type=ColumnType.INTEGER if i == 0 else ColumnType.TEXT,
                            nullable=False,
                            description=c.description,
                        )
                        for i, c in enumerate(t.columns)
                    )
                }
            )
            for t in self.task.tables
        )
        typed_fps = cont.schema_fingerprints(retyped)
        shared = anchor_fps & typed_fps
        self.assertTrue(all(fp.startswith(cont.ARMING_PREFIXES) for fp in shared))
        self.assertIn(next(fp for fp in typed_fps if fp.startswith("shape:")), anchor_fps)
        self.assertFalse(any(fp.startswith("schema:") for fp in shared))


@needs_bench
class TestAppStoreAnchor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = anchor.import_anchor_task(BENCH_ROOT, "app_store")

    def test_backends(self):
        by_table = {b.table: b.backend for b in self.task.backends}
        self.assertEqual(
            by_table,
            {"playstore": Backend.MONGODB, "user_reviews": Backend.REST},
        )

    def test_unprefixed_mart_sort_key(self):
        # app_store's mart is named `apps` (no db prefix) in sort_key.json.
        self.assertEqual(self.task.mart("apps").key_columns, ("app_name",))

    def test_scale(self):
        primary = self.task.population(PopulationName.PRIMARY)
        self.assertEqual(primary.scale, {"playstore": 10840, "user_reviews": 64286})


class TestAnchorCommandNaming(unittest.TestCase):
    """`ingest-anchor` read like "ELT-Bench is a training source". It is not."""

    def test_measure_target_is_the_primary_name(self):
        from elt_taskgen import cli

        args = cli.build_parser().parse_args(["measure-target", "--bench-root", "/x"])
        self.assertEqual(args.command, "measure-target")
        self.assertIs(args.func, cli.cmd_ingest_anchor)

    def test_ingest_anchor_still_works_as_a_deprecated_alias(self):
        from elt_taskgen import cli

        args = cli.build_parser().parse_args(["ingest-anchor", "--bench-root", "/x"])
        self.assertEqual(args.command, "ingest-anchor")
        self.assertIs(args.func, cli.cmd_ingest_anchor)

    def test_help_states_the_reference_only_contract(self):
        from elt_taskgen import cli

        sub = next(
            a for a in cli.build_parser()._actions
            if isinstance(a, cli.argparse._SubParsersAction)  # type: ignore[attr-defined]
        )
        text = sub.choices["measure-target"].format_help().lower()
        self.assertIn("never", text)
        self.assertIn("training source", text)
        self.assertIn("contamination", text)
        self.assertIn("reference/anchors", text)

    @needs_cli_fail_closed
    def test_measure_target_refuses_empty_checkout(self):
        """An existing-but-empty elt-bench/snowflake dir must NOT arm anything.

        Before C2 this printed 'imported 0 anchor task(s); contamination
        index armed with 0 anchor fingerprint(s)' and exited 0. Now: REFUSED
        on stderr, exit 2, no anchors written, index not ARMED.
        """
        from elt_taskgen.verification import contamination as cont

        with tempfile.TemporaryDirectory() as tmp:
            fake_bench = Path(tmp) / "bench"
            (fake_bench / "elt-bench" / "snowflake").mkdir(parents=True)
            ws = Path(tmp) / "ws"
            rc, out, err = _run_measure_target(
                "--workspace", str(ws), "--bench-root", str(fake_bench)
            )
            self.assertEqual(rc, 2, msg=f"stdout={out!r} stderr={err!r}")
            self.assertIn("REFUSED", err)
            self.assertIn("NOT armed", err)
            self.assertEqual(list(anchor.anchor_store_dir(ws).glob("*.json")), [])
            cov = cont.ContaminationIndex(ws / "state" / "contamination").coverage()
            self.assertIsNot(cov.level, cont.CoverageLevel.ARMED)

    @needs_bench
    @needs_cli_fail_closed
    def test_measure_target_single_db_arms_shape_namespace(self):
        """`--db retails` is a deliberate partial arm: allowed, and because
        the anchor contributes type-blind shape fingerprints the index grades
        ARMED (the typed hashes alone would not)."""
        from elt_taskgen.verification import contamination as cont

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc, out, err = _run_measure_target(
                "--workspace", str(ws), "--bench-root", str(BENCH_ROOT), "--db", "retails"
            )
            self.assertEqual(rc, 0, msg=f"stdout={out!r} stderr={err!r}")
            idx = cont.ContaminationIndex(ws / "state" / "contamination")
            cov = idx.coverage()
            self.assertIs(cov.level, cont.CoverageLevel.ARMED)
            self.assertGreaterEqual(cov.benchmark_by_kind.get("shape", 0), 1)
            self.assertGreaterEqual(cov.shape_fingerprints, 1 + 8)  # retails: 8 tables
            self.assertTrue(
                (anchor.anchor_store_dir(ws) / "eltbench__retails.json").is_file()
            )

    @needs_bench
    @needs_cli_reference_sql
    def test_measure_target_arms_reference_sql(self):
        """The pinned checkout's evaluation/sql/<db>/*.sql land as `sql:` fps."""
        from elt_taskgen.verification import contamination as cont

        sql_dir = BENCH_ROOT / "evaluation" / "sql" / "retails"
        expected = {
            cont.sql_fingerprint(p.read_text(encoding="utf-8", errors="replace"))
            for p in sorted(sql_dir.glob("*.sql"))
        }
        self.assertTrue(expected, msg=f"no reference SQL under {sql_dir}")
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc, out, err = _run_measure_target(
                "--workspace", str(ws), "--bench-root", str(BENCH_ROOT), "--db", "retails"
            )
            self.assertEqual(rc, 0, msg=f"stdout={out!r} stderr={err!r}")
            store = ws / "state" / "contamination" / "eltbench.json"
            import json as _json

            fps = set(_json.loads(store.read_text(encoding="utf-8"))["fingerprints"])
            self.assertTrue(expected <= fps, msg=f"missing: {sorted(expected - fps)[:3]}")
            cov = cont.ContaminationIndex(ws / "state" / "contamination").coverage()
            self.assertGreaterEqual(cov.benchmark_by_kind.get("sql", 0), len(expected))


@needs_bench
class TestAnchorStore(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        tasks = [
            anchor.import_anchor_task(BENCH_ROOT, "airline"),
            anchor.import_anchor_task(BENCH_ROOT, "app_store"),
        ]
        with tempfile.TemporaryDirectory() as ws:
            anchor.save_anchor_store(tasks, Path(ws))
            # The store is REFERENCE material: it lands under reference/, not
            # beside the candidate tasks (see the relocation note in the adapter).
            self.assertFalse((Path(ws) / "anchors").exists())
            store = Path(ws) / "reference" / "anchors"
            self.assertEqual(anchor.anchor_store_dir(Path(ws)), store)
            files = sorted(p.name for p in store.glob("*.json"))
            self.assertEqual(files, ["eltbench__airline.json", "eltbench__app_store.json"])
            loaded = anchor.load_anchor_store(Path(ws))
            self.assertEqual([t.task_id for t in loaded], [t.task_id for t in tasks])
            self.assertEqual(
                [t.content_hash() for t in loaded], [t.content_hash() for t in tasks]
            )

    def test_load_missing_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(FileNotFoundError):
                anchor.load_anchor_store(Path(ws) / "nope")

    def test_save_refuses_non_anchor_origin(self):
        from elt_taskgen.demo_fixture import demo_task

        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(ValueError):
                anchor.save_anchor_store([demo_task()], Path(ws))

    def test_import_unknown_db_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            anchor.import_anchor_task(BENCH_ROOT, "no_such_db")

    def test_legacy_store_is_migrated_not_ignored(self):
        """An old <ws>/anchors/ store must keep arming the firewall."""
        task = anchor.import_anchor_task(BENCH_ROOT, "airline")
        with tempfile.TemporaryDirectory() as ws:
            legacy = anchor.legacy_anchor_store_dir(Path(ws))
            legacy.mkdir(parents=True)
            (legacy / f"{task.task_id}.json").write_text(
                task_to_json(task), encoding="utf-8"
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DeprecationWarning)
                loaded = anchor.load_anchor_store(Path(ws))
            self.assertEqual([t.task_id for t in loaded], [task.task_id])
            self.assertEqual(loaded[0].content_hash(), task.content_hash())
            # Loud, not silent — and the legacy dir is gone, not shadowed.
            self.assertTrue(
                any(issubclass(w.category, DeprecationWarning) for w in caught)
            )
            self.assertFalse(legacy.exists())
            self.assertTrue(
                (anchor.anchor_store_dir(Path(ws)) / f"{task.task_id}.json").is_file()
            )

    def test_save_migrates_legacy_store_alongside_new_anchors(self):
        airline = anchor.import_anchor_task(BENCH_ROOT, "airline")
        app_store = anchor.import_anchor_task(BENCH_ROOT, "app_store")
        with tempfile.TemporaryDirectory() as ws:
            legacy = anchor.legacy_anchor_store_dir(Path(ws))
            legacy.mkdir(parents=True)
            (legacy / f"{airline.task_id}.json").write_text(
                task_to_json(airline), encoding="utf-8"
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                anchor.save_anchor_store([app_store], Path(ws))
            # The pre-existing anchor is preserved, not dropped by the rewrite.
            self.assertEqual(
                [t.task_id for t in anchor.load_anchor_store(Path(ws))],
                ["eltbench__airline", "eltbench__app_store"],
            )

    def test_migration_conflict_fails_closed(self):
        """Differing legacy/new copies of one anchor must never be guessed at."""
        task = anchor.import_anchor_task(BENCH_ROOT, "airline")
        with tempfile.TemporaryDirectory() as ws:
            legacy = anchor.legacy_anchor_store_dir(Path(ws))
            legacy.mkdir(parents=True)
            (legacy / f"{task.task_id}.json").write_text(
                task_to_json(task), encoding="utf-8"
            )
            new_dir = anchor.anchor_store_dir(Path(ws))
            new_dir.mkdir(parents=True)
            mutated = task.model_copy(update={"title": "tampered anchor"})
            (new_dir / f"{task.task_id}.json").write_text(
                task_to_json(mutated), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                anchor.migrate_legacy_anchor_store(Path(ws))

    def test_migration_conflict_moves_nothing_at_all(self):
        """All-or-nothing: one conflict must not strand a half-drained store.

        The earlier implementation moved files one at a time and raised on the
        FIRST conflict, so a workspace with a clean anchor sorted before the
        conflicting one ended up with that anchor in the new store, the rest
        stuck in the legacy directory, and both partially populated. The plan
        is now computed before anything moves, so the workspace survives the
        refusal byte-for-byte — and the error names EVERY conflict, not just
        the first one an operator would have to fix and re-run."""
        airline = anchor.import_anchor_task(BENCH_ROOT, "airline")
        app_store = anchor.import_anchor_task(BENCH_ROOT, "app_store")
        clean = anchor.import_anchor_task(BENCH_ROOT, "address")
        with tempfile.TemporaryDirectory() as ws:
            legacy = anchor.legacy_anchor_store_dir(Path(ws))
            legacy.mkdir(parents=True)
            new_dir = anchor.anchor_store_dir(Path(ws))
            new_dir.mkdir(parents=True)
            # `address` sorts before both conflicts, so a file-at-a-time
            # migration would have moved it before hitting the first one.
            for task in (airline, app_store, clean):
                (legacy / f"{task.task_id}.json").write_text(
                    task_to_json(task), encoding="utf-8"
                )
            for task in (airline, app_store):
                (new_dir / f"{task.task_id}.json").write_text(
                    task_to_json(task.model_copy(update={"title": "tampered"})),
                    encoding="utf-8",
                )
            before = {
                p.relative_to(ws): p.read_text(encoding="utf-8")
                for p in sorted(Path(ws).rglob("*.json"))
            }

            with self.assertRaises(ValueError) as ctx:
                anchor.migrate_legacy_anchor_store(Path(ws))

            # BOTH conflicts are reported, in one pass.
            message = str(ctx.exception)
            self.assertIn(f"{airline.task_id}.json", message)
            self.assertIn(f"{app_store.task_id}.json", message)
            self.assertIn("NOTHING was moved", message)
            # The workspace is exactly as it was: same files, same bytes.
            after = {
                p.relative_to(ws): p.read_text(encoding="utf-8")
                for p in sorted(Path(ws).rglob("*.json"))
            }
            self.assertEqual(after, before)
            self.assertTrue(
                (legacy / f"{clean.task_id}.json").is_file(),
                "the clean legacy anchor must not have been drained",
            )
            self.assertFalse((new_dir / f"{clean.task_id}.json").exists())

    def test_migration_conflict_does_not_create_the_new_store(self):
        """A refusal on a workspace with no new-side directory leaves none."""
        task = anchor.import_anchor_task(BENCH_ROOT, "airline")
        with tempfile.TemporaryDirectory() as ws:
            legacy = anchor.legacy_anchor_store_dir(Path(ws))
            legacy.mkdir(parents=True)
            (legacy / f"{task.task_id}.json").write_text(
                task_to_json(task), encoding="utf-8"
            )
            # Same name, different bytes, sitting in the new store already.
            new_dir = anchor.anchor_store_dir(Path(ws))
            new_dir.mkdir(parents=True)
            (new_dir / f"{task.task_id}.json").write_text(
                task_to_json(task.model_copy(update={"title": "tampered"})),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                anchor.migrate_legacy_anchor_store(Path(ws))
            self.assertTrue((legacy / f"{task.task_id}.json").is_file())

    def test_byte_identical_twin_is_migrated_not_duplicated(self):
        """A legacy file whose new-side twin already matches is simply dropped."""
        task = anchor.import_anchor_task(BENCH_ROOT, "airline")
        payload = task_to_json(task)
        with tempfile.TemporaryDirectory() as ws:
            legacy = anchor.legacy_anchor_store_dir(Path(ws))
            legacy.mkdir(parents=True)
            (legacy / f"{task.task_id}.json").write_text(payload, encoding="utf-8")
            new_dir = anchor.anchor_store_dir(Path(ws))
            new_dir.mkdir(parents=True)
            (new_dir / f"{task.task_id}.json").write_text(payload, encoding="utf-8")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                migrated = anchor.migrate_legacy_anchor_store(Path(ws))
            self.assertEqual(migrated, [f"{task.task_id}.json"])
            self.assertFalse(legacy.exists())
            self.assertEqual(
                (new_dir / f"{task.task_id}.json").read_text(encoding="utf-8"),
                payload,
            )

    def test_migrate_is_a_no_op_without_a_legacy_store(self):
        with tempfile.TemporaryDirectory() as ws:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DeprecationWarning)
                self.assertEqual(anchor.migrate_legacy_anchor_store(Path(ws)), [])
            self.assertEqual(len(caught), 0)
            self.assertFalse((Path(ws) / "reference").exists())

    def test_import_all_anchors_covers_full_pinned_benchmark(self):
        # The pinned checkout ships 100 dbs; every one must import (this
        # exercises the lenient data_model parser and expression sort keys).
        tasks = anchor.import_all_anchors(BENCH_ROOT)
        self.assertEqual(len(tasks), 100)
        self.assertTrue(all(t.origin is Origin.ELTBENCH_ANCHOR for t in tasks))
        self.assertEqual([t.task_id for t in tasks], sorted(t.task_id for t in tasks))


DLT_MANIFEST_YAML = """\
connector: github_issues
license: Apache-2.0
endpoints:
  - name: issues
    path: /repos/{owner}/{repo}/issues
    primary_key: [id]
    cursor: updated_at
    write_disposition: merge
    columns:
      - {name: id, type: bigint, nullable: false}
      - {name: title, type: text}
      - {name: state, type: string}
      - {name: updated_at, type: timestamp, nullable: false}
  - name: comments
    path: /repos/{owner}/{repo}/issues/comments
    primary_key: [id]
    parent: issues
    parent_key: issue_id
    cursor: updated_at
    write_disposition: merge
"""


class TestDltAdapter(unittest.TestCase):
    def _manifest(self) -> dlt_adapter.DltManifest:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "github.yaml"
            p.write_text(DLT_MANIFEST_YAML, encoding="utf-8")
            return dlt_adapter.load_connector(p)

    def test_load_connector_yaml(self):
        m = self._manifest()
        self.assertEqual(m.connector, "github_issues")
        self.assertEqual([e.name for e in m.endpoints], ["issues", "comments"])
        self.assertEqual(m.endpoint("issues").cursor, "updated_at")

    def test_to_task_ir_backends_and_options(self):
        task = dlt_adapter.to_task_ir(self._manifest())
        self.assertIs(task.origin, Origin.DLT)
        self.assertEqual(task.task_id, "dlt__github_issues")
        self.assertEqual(task.family_id, "dlt__github_issues")
        # Both endpoints are paginated. The backend rotation spreads paginated
        # resources instead of pinning them all to `rest` (blocker-2 EL
        # diversity), but `rest` must stay represented so the page
        # under-count hazard is live.
        self.assertIn(Backend.REST, {b.backend for b in task.backends})
        opts = task.backend_for("issues").options
        self.assertEqual(opts["write_disposition"], "merge")
        self.assertEqual(opts["cursor"], "updated_at")
        self.assertEqual(opts["path"], "/repos/{owner}/{repo}/issues")

    def test_declared_columns_used_verbatim(self):
        task = dlt_adapter.to_task_ir(self._manifest())
        issues = task.table("issues")
        self.assertEqual(issues.primary_key, ("id",))
        self.assertIs(issues.column("id").type, ColumnType.BIGINT)
        self.assertIs(issues.column("state").type, ColumnType.TEXT)

    def test_synthesized_columns_for_undeclared_endpoint(self):
        task = dlt_adapter.to_task_ir(self._manifest())
        comments = task.table("comments")
        names = [c.name for c in comments.columns]
        self.assertEqual(names, ["id", "issue_id", "updated_at", "payload"])
        self.assertIs(comments.column("payload").type, ColumnType.JSON)
        self.assertIs(comments.column("updated_at").type, ColumnType.TIMESTAMP)

    def test_parent_edge_becomes_required_relationship(self):
        task = dlt_adapter.to_task_ir(self._manifest())
        self.assertEqual(len(task.relationships), 1)
        rel = task.relationships[0]
        self.assertEqual(rel.child_table, "comments")
        self.assertEqual(rel.child_columns, ("issue_id",))
        self.assertEqual(rel.parent_table, "issues")
        self.assertEqual(rel.parent_columns, ("id",))
        self.assertTrue(rel.required)

    def test_real_transform_marts_replace_the_count_echo(self):
        """The count-echo mart is GONE, and its absence is the assertion.

        `extraction_summary` used to be this pool's guaranteed mart, and its
        content was the per-table row-count vector — exactly what
        `compare_stage1` already grades in the EXTRACT_LOAD half. Because the
        memorization pair shares that vector BY DESIGN, its primary and
        resampled gold were identical and the data-sensitivity gate rejected
        the task (measured live on personio). `_derive_marts` now refuses a
        connector whose graph funds no real mart rather than shipping a
        count-echo as its transform half, so this test pins the replacement:
        two genuine marts, and no mart named after the row counts.
        """
        task = dlt_adapter.to_task_ir(self._manifest())
        self.assertEqual([m.name for m in task.marts], ["dim_issues", "comments_activity"])
        with self.assertRaises(KeyError):
            task.mart("extraction_summary")
        dim = task.mart("dim_issues")
        self.assertEqual(dim.key_columns, ("id",))
        self.assertEqual(
            [c.name for c in dim.columns], ["id", "comments_count", "last_updated_at"]
        )
        activity = task.mart("comments_activity")
        self.assertEqual(activity.key_columns, ("issue_id", "activity_date"))
        descriptions = "\n".join(
            op.description for mart in task.marts for op in mart.plan.ops
        )
        self.assertNotIn("Project the grain", descriptions)
        self.assertNotIn("The grain of issues", descriptions)
        self.assertIn("Form the mart key columns", descriptions)

    def test_merge_without_pk_rejected(self):
        with self.assertRaises(ValueError):
            dlt_adapter.DltEndpoint(name="x", path="/x", write_disposition="merge")

    def test_bad_disposition_rejected(self):
        with self.assertRaises(ValueError):
            dlt_adapter.DltEndpoint(name="x", path="/x", write_disposition="upsert")

    def test_unknown_parent_rejected(self):
        with self.assertRaises(ValueError):
            dlt_adapter.DltManifest(
                connector="c",
                endpoints=(
                    dlt_adapter.DltEndpoint(
                        name="child", path="/c", parent="ghost", parent_key="ghost_id"
                    ),
                ),
            )

    def test_json_manifest_also_loads(self):
        import json as _json

        raw = {
            "connector": "tiny",
            "endpoints": [{"name": "events", "path": "/events", "primary_key": ["id"]}],
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tiny.json"
            p.write_text(_json.dumps(raw), encoding="utf-8")
            m = dlt_adapter.load_connector(p)
        # The JSON loader is what this test exists for, and it worked: a
        # manifest object with the declared endpoint came back.
        self.assertEqual(m.connector, "tiny")
        self.assertEqual([e.name for e in m.endpoints], ["events"])
        # to_task_ir then REFUSES, and that refusal is correct: one endpoint
        # with no child funds no join and no aggregate, so the only mart it
        # could build would echo the stage-1 row counts the EXTRACT_LOAD
        # reward already grades. Refusing at ingest beats shipping a task
        # with no gradable transform half.
        with self.assertRaises(ValueError) as ctx:
            dlt_adapter.to_task_ir(m, pool="dlt")
        self.assertIn("no transform mart could be built", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

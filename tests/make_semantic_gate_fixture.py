"""One-shot maker for the committed semantic-gate fixture release.

NOT a test.  Run manually, once per INTENTIONAL re-freeze (a census/scorer/
schema bump reviewed by a human):

    .venv/bin/python tests/make_semantic_gate_fixture.py \
        [--destination snowflake|databricks|redshift] [--output PATH] [--force]

It materializes the hand-authored five-backend gate task
(tests/semantic_gate_fixture.py) through the REAL pipeline — generate ->
render -> run_reference -> freeze_gold -> export -> emit variants ->
freeze_release — then REFUSES to install the fixture unless:

  * the pipeline's frozen gold equals the module's HAND-DERIVED
    EXPECTED_GOLD byte-for-byte (gate item 6's independent validation), and
  * the pipeline's stage-1 counts equal the pure-Python recount of the
    literal rows, and
  * verify_release(out).ok, load_semantic_package round-trips, and the
    reference submission scores exactly 1.0 on every graded population.

A disagreement between pipeline gold and hand gold is either a bug in the
hand derivation or a real pipeline finding — investigate it; never edit
EXPECTED_GOLD to match the pipeline without understanding why.

The installed tree (tests/fixtures/semantic_gate/) is thereafter immutable;
tests/test_semantic_gate.py only reads it.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "semantic_gate"
FIXTURE_RELEASE = FIXTURE_ROOT / "release"
FIXTURE_MANIFEST = FIXTURE_ROOT / "fixture_manifest.json"

try:
    import semantic_gate_fixture as fixture
    from release_doubles import FakeEngine, FakeReport, FakeSelection
except ImportError:  # invoked from the repo root
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import semantic_gate_fixture as fixture
    from release_doubles import FakeEngine, FakeReport, FakeSelection

import duckdb
import pydantic
import sqlglot

from elt_taskgen.export import eltbench, release
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    PopulationName,
    RLVR_TASK_VARIANTS,
    TaskVariant,
    canonical_json,
)
from elt_taskgen.reference.gold import freeze_gold
from elt_taskgen.reference.runner import run_reference
from elt_taskgen.reference.solution import find_rendered_artifact
from elt_taskgen.semantic import (
    SEMANTIC_SUBMISSION_SCHEMA_VERSION,
    load_semantic_package,
    score_semantic_text,
)
from elt_taskgen.verification.gates import GRADED_POPULATIONS
from elt_taskgen.verification.strict_diagnostic import load_sources_duckdb_strict


def _fail(message: str) -> None:
    raise SystemExit(f"make_semantic_gate_fixture: REFUSING TO FREEZE — {message}")


def _reference_submission_text(task, package) -> str:
    """The reference attempt: frozen artifacts' paths + the authored SQL."""
    root = package.source_root(PopulationName.PRIMARY)
    plan = {}
    for table in task.tables:
        artifact = find_rendered_artifact(task, root, table.name)
        backend = task.backend_for(table.name).backend.value
        plan[table.name] = {
            "path": artifact.relative_to(root).as_posix(),
            "format": fixture.BACKEND_FORMATS[backend],
        }
    return json.dumps(
        {
            "schema_version": SEMANTIC_SUBMISSION_SCHEMA_VERSION,
            "task_id": task.task_id,
            "load_plan": plan,
            "sql_by_mart": {
                fixture.ROLLUP_MART: fixture.ROLLUP_SQL,
                fixture.WIDE_MART: fixture.WIDE_SQL,
            },
        }
    )


def _check_gold_against_hand_derivation(gold) -> None:
    """Pipeline gold must equal the hand-derived literals BYTE FOR BYTE."""
    for population, marts in sorted(fixture.EXPECTED_GOLD.items()):
        recount = {
            table: len(rows)
            for table, rows in fixture.LITERAL_ROWS[
                PopulationName(population)
            ].items()
        }
        if gold.stage1.get(population) != recount:
            _fail(
                f"stage-1 counts for population {population!r} disagree with "
                f"the literal-row recount: pipeline {gold.stage1.get(population)!r}"
                f" vs recount {recount!r}"
            )
        if gold.stage1.get(population) != fixture.EXPECTED_STAGE1[population]:
            _fail(
                f"stage-1 counts for population {population!r} disagree with "
                "EXPECTED_STAGE1 — fix the fixture module"
            )
        for mart, expected_csv in sorted(marts.items()):
            actual_csv = (gold.stage2_csv.get(population) or {}).get(mart)
            if actual_csv == expected_csv:
                continue
            expected_lines = expected_csv.splitlines()
            actual_lines = (actual_csv or "").splitlines()
            detail = ""
            for i, (want, got) in enumerate(
                zip(expected_lines, actual_lines), start=1
            ):
                if want != got:
                    detail = (
                        f" first divergence at line {i}: hand {want[:120]!r} "
                        f"vs pipeline {got[:120]!r}"
                    )
                    break
            if not detail and len(expected_lines) != len(actual_lines):
                detail = (
                    f" hand has {len(expected_lines)} lines, pipeline "
                    f"{len(actual_lines)}"
                )
            _fail(
                f"pipeline gold for {population}/{mart} disagrees with the "
                f"HAND-DERIVED EXPECTED_GOLD.{detail} This is either a bug in "
                "the hand derivation or a real pipeline finding — investigate; "
                "do not paper over."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        choices=("snowflake", "databricks", "redshift"),
        default="snowflake",
        help="warehouse projection to freeze (default: snowflake)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=FIXTURE_ROOT,
        help=(
            "fixture root to create (default: committed semantic-gate fixture; "
            "use a fresh path for compatibility releases)"
        ),
    )
    parser.add_argument(
        "--task-id",
        default=fixture.GATE_TASK_ID,
        help=(
            "logical task/warehouse namespace to freeze (default: the "
            "committed semantic-gate task id)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output fixture (intentional re-freeze)",
    )
    args = parser.parse_args()
    fixture_root = args.output.resolve()
    fixture_release = fixture_root / "release"
    fixture_manifest = fixture_root / "fixture_manifest.json"
    if fixture_release.exists() and not args.force:
        _fail(
            f"{fixture_release} already exists; fixture outputs are immutable. "
            "Pass --force only for a reviewed re-freeze."
        )

    prior_flat = os.environ.pop(eltbench.FLAT_FILES_BASE_URL_ENV, None)
    prior_rest = os.environ.pop(eltbench.REST_BASE_URL_ENV, None)
    workspace = Path(tempfile.mkdtemp(prefix="elt-gate-fixture-"))
    try:
        task = fixture.gate_task(args.task_id)
        task_root = workspace / "tasks" / task.task_id
        populations_dir = task_root / "populations"
        answer_key_dir = task_root / "answer_key"

        for pop in PopulationName:
            rows = source_data.generate_rows(task, pop)
            pop_dir = populations_dir / pop.value
            source_data.write_rows(rows, pop_dir / "rows")
            source_data.render_population(task, pop, rows, pop_dir / "rendered")

            strict = duckdb.connect(":memory:")
            try:
                strict_counts = load_sources_duckdb_strict(
                    task, pop_dir / "rendered", strict
                )
            except Exception as exc:
                _fail(
                    f"strict source load failed for population {pop.value!r}: "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                strict.close()
            if strict_counts != fixture.EXPECTED_STAGE1[pop.value]:
                _fail(
                    f"strict source counts for population {pop.value!r} "
                    f"disagree with EXPECTED_STAGE1: {strict_counts!r}"
                )

        results = {
            pop: run_reference(task, pop, workspace) for pop in PopulationName
        }
        gold = freeze_gold(task, results, answer_key_dir)

        # THE LOAD-BEARING CHECK: pipeline gold vs hand-derived gold.
        _check_gold_against_hand_derivation(gold)

        eltbench.export_task(
            task,
            gold,
            task_root / "task",
            answer_key_dir,
            destination=args.destination,
        )
        variants_root = task_root / "variants"
        for variant in TaskVariant:
            eltbench.emit_variant(
                task,
                gold,
                variant,
                variants_root / variant.value,
                populations_dir=populations_dir,
            )

        engine = FakeEngine(
            workspace, task, FakeReport("pass", task.content_hash())
        )
        selection = FakeSelection(
            train=(task.task_id,),
            variants={task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        out_dir = workspace / "release" / "semantic-gate"
        manifest = release.freeze_release(engine, selection, out_dir)

        verification = release.verify_release(out_dir)
        if not verification.ok:
            _fail(
                "freshly frozen fixture release fails verify_release: "
                + "; ".join(verification.failures[:5])
            )
        package = load_semantic_package(out_dir, task.task_id)
        result = score_semantic_text(
            package, _reference_submission_text(task, package)
        )
        if (
            not result.valid_submission
            or result.reward != 1.0
            or result.semantic_el_reward != 1.0
            or result.semantic_t_reward != 1.0
            or set(result.populations)
            != {population.value for population in GRADED_POPULATIONS}
        ):
            _fail(
                "reference submission does not score 1.0 on every graded "
                f"population: {result.model_dump(mode='json')}"
            )

        # Install: the release tree plus a provenance manifest.
        if fixture_release.exists():
            shutil.rmtree(fixture_release)
        fixture_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(out_dir, fixture_release)
        expected_gold_sha256 = {
            population: {
                mart: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for mart, text in sorted(marts.items())
            }
            for population, marts in sorted(fixture.EXPECTED_GOLD.items())
        }
        fixture_manifest.write_text(
            json.dumps(
                {
                    "created_utc": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(timespec="seconds"),
                    "destination": args.destination,
                    "task_id": task.task_id,
                    "release_id": manifest.release_id,
                    "schema_version": manifest.schema_version,
                    "semantic_scorer_version": manifest.semantic_scorer_version,
                    "census_version": eltbench.CENSUS_VERSION,
                    "environment": {
                        "python": platform.python_version(),
                        "duckdb": duckdb.__version__,
                        "sqlglot": sqlglot.__version__,
                        "pydantic": pydantic.__version__,
                    },
                    "expected_gold_sha256": expected_gold_sha256,
                    "reference_result_digest": hashlib.sha256(
                        canonical_json(result.model_dump(mode="json")).encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"fixture release installed at {fixture_release}")
        print(f"fixture manifest written to {fixture_manifest}")
        print(f"release_id: {manifest.release_id}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        if prior_flat is not None:
            os.environ[eltbench.FLAT_FILES_BASE_URL_ENV] = prior_flat
        if prior_rest is not None:
            os.environ[eltbench.REST_BASE_URL_ENV] = prior_rest


if __name__ == "__main__":
    main()

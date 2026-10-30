"""Bounded, replayable public-model prompt experiment (not full ELT-Bench).

Prepare frozen inputs, compare prompt variants on dev, freeze a winner, then test
it on holdout. --solve compares baseline/winner SQL recovery on three populations.
Calls are metered; raw prompts, outputs, hashes and scores stay in testing/.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import statistics
import sys
import threading

import yaml

from elt_taskgen.models import PopulationName, TaskIR
from elt_taskgen.review.model_specification import PROMPT_VARIANTS, author_prompt, parse_document, parse_json_response, review_prompt
from elt_taskgen.specification import data_model

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Make the shared synthetic test fixture importable.
DEFAULT = ROOT / "testing/results/model-spec-prompts-business"
POPULATIONS = (PopulationName.PRIMARY, PopulationName.RESAMPLED, PopulationName.COUNTERFACTUAL)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def descriptions(doc):
    return [item["description"] for m in doc["models"] for item in [m, *m["columns"]]]


def metrics(doc):
    prose = " ".join(descriptions(doc))
    columns = [c["description"] for m in doc["models"] for c in m["columns"]]
    return {"words": len(prose.split()), "column_words_median": statistics.median(len(s.split()) for s in columns),
            "recipe_hits": re.findall(r"\b(?:CTEs?|LEFT JOIN|INNER JOIN|GROUP BY|ROW_NUMBER|window functions?|step \d|preaggregate)\b", prose, re.I),
            "template_hits": re.findall(r"\b(?:this parent|this entity|link rows?|child rows?|associated rows?|copied unchanged)\b", prose, re.I)}


def load_and_fetch(task, rows, sql):
    from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
    from elt_taskgen.reference.solution import create_table, _insert_rows

    with sandboxed_memory_connection() as connection:
        for table in task.tables:
            create_table(connection, table)
            _insert_rows(connection, table, rows[table.name])
        cursor = connection.execute(sql)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def score_sql(connection, task, gold, sql):
    import csv
    import sqlglot
    from sqlglot import exp
    from elt_taskgen.reference.solution import execute_mart
    from elt_taskgen.verification.upstream_eval import compare_mart
    parsed = sqlglot.parse(sql, read="duckdb")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
        return {"success": False, "error": "expected_one_select", "columns": {}}
    timer = threading.Timer(30, connection.interrupt)
    timer.start()
    try:
        mart = task.marts[0]
        rows = execute_mart(connection, mart, sql, max_rows=100_000, max_bytes=16 * 1024 * 1024)
        columns = {}
        import io
        gold_rows = list(csv.DictReader(io.StringIO(gold)))
        from elt_taskgen.verification.upstream_eval import rows_to_canonical_csv
        lookup = {c.casefold(): c for c in gold_rows[0]}
        for column in mart.columns:
            names = tuple(dict.fromkeys((*mart.key_columns, column.name)))
            partial = rows_to_canonical_csv([{name: row[lookup[name.casefold()]] for name in names} for row in gold_rows], names)
            columns[column.name] = compare_mart(partial, rows, mart)
        return {"success": compare_mart(gold, rows, mart), "error": None, "rows": len(rows), "columns": columns}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:600]}", "columns": {}}
    finally:
        timer.cancel()


def prepare(run, *, case_bank=None, bench_root=None):
    from elt_taskgen.generation import mart_plan
    from elt_taskgen.generation.source_data import generate_rows
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.upstream_eval import rows_to_canonical_csv
    from tests.test_dataset_column_names import EVIDENCE, build_task

    if (run / "manifest.json").exists():
        manifest = json.loads((run / "manifest.json").read_text())
        for path, sha in manifest["hashes"].items():
            assert digest(run / path) == sha, f"Frozen artifact changed: {path}"
        return manifest
    cases = []
    for split, shape in (("dev", "fan_out_rollup"), ("dev", "latest_snapshot"),
                         ("holdout", "argmax_profile"), ("holdout", "measure_state_distribution")):
        built = dict(mart_plan.registered_shape_builders())[shape](EVIDENCE, mart="studio_activity")
        task = build_task(built, "style__" + shape)
        grouped = shape == "measure_state_distribution"
        mart = task.marts[0].model_copy(update={
            "description": "Film activity by studio and runtime availability." if grouped else "Film activity for each studio.",
            "grain": ("One record per studio and runtime availability state represented by its films; "
                      "studios without films have one absent-state record.") if grouped else "One record per studio, including studios without films.",
        })
        task = task.model_copy(update={"marts": (mart,)})
        directory = run / "cases" / shape
        save(directory / "task.json", task.model_dump(mode="json"))
        save(directory / "baseline.json", data_model(task))
        for pop in POPULATIONS:
            rows = generate_rows(task, pop)
            gold = load_and_fetch(task, rows, compile_plan_sql(task, mart))
            save(directory / f"{pop.value}.rows.json", rows)
            (directory / f"{pop.value}.gold.csv").write_text(rows_to_canonical_csv(gold, tuple(c.name for c in mart.columns)))
        cases.append({"id": shape, "split": split, "gold": "compiled reference; not an independent semantic oracle"})
    for split, case_id in (
        ("dev", "advertising__aggregate_filter_rank_v1__composed"),
        ("dev", "video_activity__entity_total_baseline_v1__composed"),
        ("holdout", "advertising__calendar_streak_v1__composed"),
        ("holdout", "video_activity__aggregate_filter_rank_v1__composed"),
    ):
        source = (case_bank or ROOT / "testing/results/dependent-pilot/cases") / case_id
        target = run / "cases" / case_id
        target.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            if path.name == "task.json" or path.name.endswith((".rows.json", ".gold.csv")):
                (target / path.name).write_bytes(path.read_bytes())
        task = TaskIR.model_validate_json((target / "task.json").read_text())
        save(target / "baseline.json", data_model(task))
        cases.append({"id": case_id, "split": split, "gold": "frozen dependent-pilot oracle; three synthetic populations"})
    benchmarks = []
    benchmark_root = bench_root or ROOT.parent / "ELT-Bench/elt-bench/snowflake"
    paths = sorted(benchmark_root.glob("*/data_model.yaml"))
    if not paths:
        raise ValueError("No reference data models found; supply --bench-root")
    for path in paths:
        for model in yaml.safe_load(path.read_text())["models"]:
            model = {**model, "description": model.get("description", model.get("descrption", ""))}
            benchmarks.append(metrics({"models": [model]}))
    save(run / "benchmark_style.json", {"tasks": len(paths), "models": len(benchmarks),
        "median_column_words": statistics.median(v["column_words_median"] for v in benchmarks),
        "median_model_prose_words": statistics.median(v["words"] for v in benchmarks),
        "source_hashes": {str(p.relative_to(benchmark_root)): digest(p) for p in paths},
        "usage": "style statistics only; no benchmark task text appears in author prompts"})
    manifest = {"cases": cases, "scope": "description fidelity/style, with matched SQL-only recoverability; not official ELT-Bench accuracy",
        "split": "four preselected dev / four held-out tasks, three domains; same model for author, judge and separate solver calls",
        "hashes": {str(p.relative_to(run)): digest(p) for p in (run / "cases").rglob("*") if p.is_file()}}
    save(run / "manifest.json", manifest)
    return manifest




class Caller:
    def __init__(self, run, budget):
        from elt_taskgen.review.providers import CostMeter, load_role_routing, configure_retry_policy
        # Parse simple .env assignments as data; never execute or print them.
        env_path = ROOT / ".env"
        for line in (env_path.read_text().splitlines() if env_path.exists() else []):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            tokens = shlex.split(value, comments=True)
            if len(tokens) == 1:
                os.environ.setdefault(key.strip(), tokens[0])
        self.routing = load_role_routing()
        self.model = os.environ["ELT_TASKGEN_OSS_MODEL"]
        self.run, self.lock = run, threading.Lock()
        self.meter = CostMeter(budget_per_task_usd=budget, budget_total_usd=budget)
        if (run / "budget.json").exists():
            self.meter.total_usd = json.loads((run / "budget.json").read_text())["usd"]
        configure_retry_policy(http_retries=0, schema_retries=0, http_timeout_seconds=120)

    def call(self, path, task, prompt):
        from elt_taskgen.corpus.calibration import SolverTier, solver_provider
        from elt_taskgen.review.providers import RoutedProvider, TranscriptStore
        if re.search(r"elt[-_ ]?bench", prompt, re.I):
            raise ValueError("Benchmark names must not appear in model prompts")
        path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path = path.with_suffix(".prompt.txt")
        if prompt_path.exists():
            assert prompt_path.read_text() == prompt, f"Prompt drift: {path}"
        prompt_path.write_text(prompt)
        if path.exists():
            return path.read_text()
        tier = SolverTier(model_key="style_experiment", provider="openai_compat", model=self.model, k=1, max_tokens=8192)
        provider = solver_provider(RoutedProvider(self.routing, TranscriptStore(self.run / "transcripts"), self.meter), [tier])
        provider.begin_task_evidence(task.task_id, task.content_hash())
        try:
            response = provider.complete(tier.role_name, prompt)
            path.write_text(response)
            return response
        except Exception as exc:
            failures = list(path.parent.glob(path.stem + ".failure-*.json"))
            save(path.with_name(f"{path.stem}.failure-{len(failures) + 1}.json"), {
                "type": type(exc).__name__, "message": str(exc)[:500],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            })
            raise
        finally:
            with self.lock:
                save(self.run / "budget.json", {"usd": self.meter.total_usd, "model": self.model,
                    "calls_this_process": self.meter.attempt_count, "per_role_this_process": self.meter.per_role})


def write_case(run, caller, case, variant):
    source = run / "cases" / case["id"]
    task = TaskIR.model_validate_json((source / "task.json").read_text())
    expected = data_model(task)
    target = run / "outputs" / case["id"] / variant
    prompt = author_prompt(task, variant)
    text = caller.call(target / "response.txt", task, prompt)
    record = {"case": case["id"], "split": case["split"], "variant": variant, "baseline": metrics(expected),
              "author_failures_before_response": len(list(target.glob("response.failure-*.json")))}
    try:
        document = parse_document(text, expected)
        record.update({"schema_valid": True, "candidate": metrics(document)})
        (target / "data_model.yaml").write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
        judge = parse_json_response(caller.call(target / "judge.txt", task, review_prompt(task, expected, document)))
        assert isinstance(judge["semantic_issues"], list)
        assert all(type(judge[k]) is int and 1 <= judge[k] <= 5 for k in ("business_language", "concise_output_spec"))
        record["judge"] = judge
    except (ValueError, KeyError, AssertionError) as exc:
        record.setdefault("schema_valid", False)
        record.update({"review_valid": False, "error": f"{type(exc).__name__}: {exc}"})
    save(target / "result.json", record)
    print(f"{case['split']} {case['id']}/{variant}: schema={record['schema_valid']}, semantic_issues={len(record.get('judge', {}).get('semantic_issues', []))}", flush=True)
    return record


def summarize(run):
    records = [json.loads(p.read_text()) for p in sorted((run / "outputs").glob("*/*/result.json"))]
    out = {}
    for split in ("dev", "holdout"):
        out[split] = {}
        for variant in PROMPT_VARIANTS:
            subset = [r for r in records if r["split"] == split and r["variant"] == variant]
            if not subset:
                continue
            valid = [r for r in subset if r.get("judge")]
            out[split][variant] = {"tasks": len(subset), "valid": sum(r["schema_valid"] for r in subset),
                "first_attempt_valid": sum(r["schema_valid"] and not r.get("author_failures_before_response", 0) for r in subset),
                "judged_semantic_pass": sum(not r["judge"]["semantic_issues"] for r in valid),
                "semantic_issues": sum(len(r["judge"]["semantic_issues"]) for r in valid),
                "style_mean": statistics.mean((r["judge"]["business_language"] + r["judge"]["concise_output_spec"])/2 for r in valid) if valid else 0,
                "words": sum(r.get("candidate", {}).get("words", 0) for r in subset),
                "baseline_words": sum(r["baseline"]["words"] for r in subset),
                "template_hits": sum(len(r.get("candidate", {}).get("template_hits", [])) for r in subset),
                "recipe_hits": sum(len(r.get("candidate", {}).get("recipe_hits", [])) for r in subset)}
    save(run / "summary.json", out)
    return out


def report(run):
    summary = summarize(run)
    reference = json.loads((run / "benchmark_style.json").read_text())
    selection = json.loads((run / "winner.json").read_text()) if (run / "winner.json").exists() else None
    baseline_words = next(iter(summary["dev"].values()))["baseline_words"] if summary["dev"] else 0
    model = json.loads((run / "budget.json").read_text())["model"] if (run / "budget.json").exists() else "unmeasured"
    lines = ["# Public model specification prompt comparison", "",
        "ELT-Bench is a comparison reference in this report only. No benchmark name or task examples are included in the model prompts.", "",
        "Five prompts were compared on four development tasks; the selected prompt was frozen before four held-out tasks. "
        "The tasks cover film studios, advertising and video activity, with different aggregation/selection rules. "
        "The reference style distribution covers 100 local benchmark tasks and 202 models.", "",
        "| Prompt | Valid schema | No semantic issues (LLM review) | Mean style / 5 | Prose words |",
        "|---|---:|---:|---:|---:|"]
    for variant, stats in summary["dev"].items():
        lines.append(f"| {variant} | {stats['valid']}/{stats['tasks']} | {stats['judged_semantic_pass']}/{stats['tasks']} | {stats['style_mean']:.2f} | {stats['words']} |")
    lines += ["", f"The four development baselines contain {baseline_words} prose words. Semantic fidelity takes priority over length or style scores.", "",
        "The brief rewrite lost required rules. The detailed variants preserved rules but expanded the prose. "
        "The concise variant blurred related-record existence with a nonmissing ID. The balanced variant explicitly preserves that distinction without explaining joins.", ""]
    if selection:
        winner = selection["variant"]
        stats = summary["holdout"].get(winner)
        lines += [f"Selected prompt: **{winner}**. [Read the instructions](winning_prompt.txt).", ""]
        if stats:
            lines += [f"Held-out results: {stats['first_attempt_valid']}/{stats['tasks']} valid documents on the first author attempt; "
                      f"{stats['valid']}/{stats['tasks']} after recorded empty-response retries; {stats['judged_semantic_pass']}/{stats['tasks']} with no semantic issues reported; "
                      f"mean style {stats['style_mean']:.2f}/5; {stats['words']} words versus {stats['baseline_words']} in the baselines.", ""]
        example = f"outputs/fan_out_rollup/{winner}/data_model.yaml"
        lines += [f"[Actual generated example]({example}) · [Baseline](cases/fan_out_rollup/baseline.json)", ""]
    if (run / "sql_summary.json").exists():
        scores = json.loads((run / "sql_summary.json").read_text())
        lines += ["| SQL recovery arm | Tasks passing all three populations |", "|---|---:|"]
        for arm, stats in scores.items():
            lines.append(f"| {arm} | {stats['passed_all']}/{stats['tasks']} |")
        lines += ["", "SQL attempts use the same model, schemas, scoring and budgets. Each attempt is checked against primary, resampled and counterfactual gold. "
                  "This tests output recoverability; it is not the full ELT pipeline benchmark.", ""]
    lines += [f"Reference style: median of per-model median column lengths is {reference['median_column_words']} words. "
              "Length alone is not an accuracy score; tasks with more required qualifiers need longer definitions.", "",
        f"Limitations: one model ({model}), four dev and four held-out tasks, one completed rewrite and one SQL attempt per arm/task (author failures/retries reported separately). "
        "Author, semantic reviewer and solver use separate calls to the same model, so LLM review is not independent ground truth. "
        "The four studio fixtures use compiled reference gold; the other four reuse the earlier independent-oracle pilot's gold. "
        "These results do not establish general accuracy on ELT-Bench.", "",
        "Protocol history is retained: v1 three prompts, v2 shorter wording, v3 existence clarification; v4 fixes the unopened measure-state fixture's grain before holdout calls. "
        "The original fixture and manifest are in fixture-corrections/. Markdown JSON fences are normalized identically for all candidates; "
        "one empty review response was retried without regenerating its candidate. One held-out author response was empty and its retry is reported separately. "
        "The superseded, interrupted run in ../model-spec-prompts/ is excluded.", "",
        "Reproduce from the repository root: `./.venv/bin/python testing/compare_model_spec_prompts.py --phase report` for local rescoring/reporting. "
        "Live commands: `--phase dev`, then `--phase holdout --winner balanced --solve`. Use a new `--run-dir` to collect independent responses; "
        "the default run directory reuses recorded responses. Calls are metered with an $8 cap.", "",
        "Code validation: [test results and remaining recording requirement](code_validation.json), "
        "[production export replay](integration_validation.json). Targeted checks pass; the old live transcript/admission recordings need refreshing before release. "
        "No full-suite or release-readiness claim is made.", ""]
    (run / "report.md").write_text("\n".join(lines))
    return summary


def solve_case(run, caller, case, variant):
    from elt_taskgen.export.eltbench import _source_schema_markdown
    from elt_taskgen.corpus.calibration import parse_submission
    from elt_taskgen.models import TaskVariant
    from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
    from elt_taskgen.reference.solution import create_table, _insert_rows
    directory = run / "cases" / case["id"]
    task = TaskIR.model_validate_json((directory / "task.json").read_text())
    document = data_model(task) if variant == "baseline" else yaml.safe_load((run / "outputs" / case["id"] / variant / "data_model.yaml").read_text())
    prompt = ("Implement this output specification using DuckDB SQL. Tables are already loaded. "
              "Return only JSON {\"sql_by_mart\": {\"model_name\": \"SELECT ...\"}}. "
              "One read-only SELECT per model, exact output column names. No file access.\n"
              + "\n".join(_source_schema_markdown(task)) + "\ndata_model.yaml:\n" + yaml.safe_dump(document, sort_keys=False))
    target = run / "solves" / case["id"] / variant
    if (target / "result.json").exists():
        return json.loads((target / "result.json").read_text())
    record = {"case": case["id"], "split": case["split"], "variant": variant, "populations": {}}
    try:
        text = caller.call(target / "response.txt", task, prompt)
        submission = parse_submission(task, TaskVariant.TRANSFORM, text)
        for pop in POPULATIONS:
            rows = json.loads((directory / f"{pop.value}.rows.json").read_text())
            with sandboxed_memory_connection(memory_limit_mb=512, threads=1, disable_temp_spill=True) as con:
                for table in task.tables:
                    create_table(con, table)
                    _insert_rows(con, table, rows.get(table.name, []))
                record["populations"][pop.value] = score_sql(con, task, (directory / f"{pop.value}.gold.csv").read_text(), submission.sql_by_mart[task.marts[0].name])
        record["passed_all"] = all(p["success"] for p in record["populations"].values())
    except Exception as exc:
        record.update({"passed_all": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    save(target / "result.json", record)
    print(f"SQL {case['id']}/{variant}: passed_all={record['passed_all']}", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT)
    parser.add_argument("--case-bank", type=Path, help="Frozen dependent-pilot cases directory used by the recorded experiment")
    parser.add_argument("--bench-root", type=Path, help="Reference directory containing */data_model.yaml (statistics only)")
    parser.add_argument("--phase", choices=("prepare", "dev", "holdout", "report"), default="prepare")
    parser.add_argument("--winner", choices=tuple(PROMPT_VARIANTS))
    parser.add_argument("--solve", action="store_true")
    parser.add_argument("--budget-usd", type=float, default=8)
    args = parser.parse_args()
    manifest = prepare(args.run_dir, case_bank=args.case_bank, bench_root=args.bench_root)
    if args.phase == "prepare":
        print(f"Prepared {len(manifest['cases'])} frozen cases.")
        return
    if args.phase == "report":
        print(json.dumps(report(args.run_dir), indent=2))
        return
    caller = Caller(args.run_dir, args.budget_usd)
    protocol = {"model": caller.model, "variants": PROMPT_VARIANTS, "max_tokens": 8192,
                "retries": 0, "feedback": "none", "budget_cap_usd": args.budget_usd,
                "selection": "semantic fidelity first, then style; winner frozen before holdout",
                "manifest_sha256": digest(args.run_dir / "manifest.json")}
    # v2 addressed verbosity; v3 clarified existence vs nonmissing IDs; v4
    # records a corrected, unopened holdout fixture's grain. No prompt changed.
    path = args.run_dir / "protocol-v4.json"
    if path.exists():
        assert json.loads(path.read_text()) == protocol, "Protocol drift: use a new run directory"
    save(path, protocol)
    variants = tuple(PROMPT_VARIANTS) if args.phase == "dev" else (args.winner,)
    if args.phase == "holdout":
        assert args.winner, "Freeze a dev-selected --winner before holdout"
        winner_path = args.run_dir / "winner.json"
        selection = {"variant": args.winner, "dev_summary": summarize(args.run_dir)["dev"]}
        if winner_path.exists():
            frozen = json.loads(winner_path.read_text())
            assert frozen["variant"] == args.winner
            for variant, stats in frozen["dev_summary"].items():
                assert all(selection["dev_summary"][variant][key] == value for key, value in stats.items())
        else:
            save(winner_path, selection)
    cases = [c for c in manifest["cases"] if c["split"] == args.phase]
    jobs = [(case, variant) for case in cases for variant in variants]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda job: write_case(args.run_dir, caller, *job), jobs))
    print(json.dumps(summarize(args.run_dir), indent=2), flush=True)
    if args.solve:
        assert args.winner
        jobs = [(case, variant) for case in manifest["cases"] for variant in ("baseline", args.winner)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda job: solve_case(args.run_dir, caller, *job), jobs))
        save(args.run_dir / "sql_summary.json", {v: {"passed_all": sum(r["passed_all"] for r in results if r["variant"] == v),
            "tasks": sum(r["variant"] == v for r in results)} for v in ("baseline", args.winner)})


if __name__ == "__main__":
    main()

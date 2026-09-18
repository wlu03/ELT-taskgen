"""Unified cross-pool difficulty measurement for docs/difficulty/README.md.

Loads every reference-stage TaskIR from the five pool workspaces plus the 100
ELT-Bench anchors, recomputes structural_features / structural_difficulty as
shipped, and runs the UPSTREAM prose tagger over mart-column descriptions on
both sides so the operator comparison is apples-to-apples.
"""
from __future__ import annotations

import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path

from elt_taskgen.models import TaskIR
from elt_taskgen.corpus.difficulty import structural_features, structural_difficulty
from elt_taskgen.corpus.selection import band_of, _quantile, anchor_profile

# ---- upstream tagger, imported verbatim from the pinned checkout ------------
BENCH = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-Bench")
spec = importlib.util.spec_from_file_location("upstream_analyze", BENCH / "analysis" / "analyze.py")
up = importlib.util.module_from_spec(spec)
sys.modules["upstream_analyze"] = up
spec.loader.exec_module(up)
tag_description = up.tag_description
DERIVED_TAGS = up.DERIVED_TAGS
ALL_TAGS = list(up.OPS.keys())

ANCHOR_DIR = Path("/tmp/anchor_baseline_ws/reference/anchors")

POOLS = {
    "synsql": ("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen/ws-difficulty-synsql/tasks", "synsql__"),
    "fivetran": ("/tmp/ws_fivetran/tasks", "dbt__"),
    "dlt": ("/tmp/diffws/tasks", "dlt__"),
    "wikidbs": ("/tmp/diffws/tasks", "wikidbs__"),
    "schemapile": ("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen/taskgen-workspace/tasks", "schemapile__"),
    "demo": ("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen/taskgen-workspace/tasks", "demo__"),
}


def load_task(p: Path) -> TaskIR:
    return TaskIR.model_validate_json(p.read_text())


def reference_stage(task_dir: Path) -> bool:
    ak = task_dir / "answer_key"
    return ak.is_dir() and any(ak.iterdir())


def collect() -> dict[str, list[tuple[str, TaskIR, bool]]]:
    out: dict[str, list[tuple[str, TaskIR, bool]]] = {}
    for pool, (root, prefix) in POOLS.items():
        rows = []
        rootp = Path(root)
        if not rootp.is_dir():
            print(f"MISSING WORKSPACE {root}", file=sys.stderr)
            continue
        for d in sorted(rootp.iterdir()):
            if not d.is_dir() or not d.name.startswith(prefix):
                continue
            ir = d / "task_ir.json"
            if not ir.exists():
                continue
            rows.append((d.name, load_task(ir), reference_stage(d)))
        out[pool] = rows
    anchors = [(p.stem, load_task(p), True) for p in sorted(ANCHOR_DIR.glob("*.json"))]
    out["eltbench_anchor"] = anchors
    return out


def q(vals, p):
    return _quantile(sorted(vals), p)


FEATS = [
    "table_count", "backend_count", "hard_backend_count", "source_column_count",
    "relationship_count", "join_count", "aggregate_count", "window_count",
    "filter_count", "dedupe_count", "union_count", "tie_break_count",
    "derive_count", "shaping_op_count", "mart_count", "mart_column_count",
]


def op_tags(task: TaskIR):
    """Upstream tagger over mart-column descriptions (what data_model.yaml ships)."""
    cols = 0
    computed = 0
    tag_cols = {t: 0 for t in ALL_TAGS}
    task_tags = set()
    for m in task.marts:
        for c in m.columns:
            desc = (c.description or "").strip()
            cols += 1
            tags = tag_description(desc)
            if tags & DERIVED_TAGS:
                computed += 1
            for t in tags:
                tag_cols[t] += 1
            task_tags |= tags
    return cols, computed, tag_cols, task_tags


def main():
    data = collect()
    report = {}
    for pool, rows in data.items():
        ref = [(n, t) for n, t, r in rows if r]
        allrows = rows
        feats = {f: [] for f in FEATS}
        loads, transs, combs, bands = [], [], [], []
        per_task = []
        tag_col_tot = {t: 0 for t in ALL_TAGS}
        tag_task_tot = {t: 0 for t in ALL_TAGS}
        mart_cols_tot = 0
        computed_tot = 0
        distinct_kinds_per_task = []
        for name, task in ref:
            fs = structural_features(task)
            dm = structural_difficulty(task)
            comb = dm.combined_score()
            for f in FEATS:
                feats[f].append(fs[f])
            loads.append(dm.load_score)
            transs.append(dm.transform_score)
            combs.append(comb)
            bands.append(band_of(comb))
            cols, computed, tc, tt = op_tags(task)
            mart_cols_tot += cols
            computed_tot += computed
            for t in ALL_TAGS:
                tag_col_tot[t] += tc[t]
                if t in tt:
                    tag_task_tot[t] += 1
            distinct_kinds_per_task.append(len(tt))
            per_task.append({
                "task": name, **{k: fs[k] for k in FEATS},
                "load": round(dm.load_score, 4), "transform": round(dm.transform_score, 4),
                "combined": round(comb, 4), "band": band_of(comb),
                "mart_cols": cols, "computed_cols": computed,
                "tags": sorted(tt),
            })
        report[pool] = {
            "n_ref": len(ref), "n_all": len(allrows),
            "quantiles": {f: [min(v), q(v, .25), q(v, .5), q(v, .75), max(v)] for f, v in feats.items() if v},
            "sums": {f: sum(v) for f, v in feats.items()},
            "load": [min(loads), q(loads, .25), q(loads, .5), q(loads, .75), max(loads)] if loads else None,
            "transform": [min(transs), q(transs, .25), q(transs, .5), q(transs, .75), max(transs)] if transs else None,
            "combined": [min(combs), q(combs, .25), q(combs, .5), q(combs, .75), max(combs)] if combs else None,
            "bands": {b: bands.count(b) for b in ("easy", "medium", "hard")},
            "mart_cols_total": mart_cols_tot,
            "computed_cols_total": computed_tot,
            "computed_pct": round(100 * computed_tot / mart_cols_tot, 1) if mart_cols_tot else None,
            "tag_cols": tag_col_tot,
            "tag_tasks": tag_task_tot,
            "distinct_op_families": len([t for t in ALL_TAGS if tag_task_tot[t] > 0]),
            "distinct_kinds_per_task_median": statistics.median(distinct_kinds_per_task) if distinct_kinds_per_task else None,
            "per_task": per_task,
        }
    Path("/tmp/final_synth/all.json").write_text(json.dumps(report, indent=1, default=str))

    # ---- console summary ----
    print("pool                n   tables  backends srccols  marts martcols joins aggs  load  transform combined  bands")
    order = ["eltbench_anchor", "fivetran", "wikidbs", "synsql", "schemapile", "dlt", "demo"]
    for pool in order:
        r = report.get(pool)
        if not r or not r["n_ref"]:
            continue
        Q = r["quantiles"]
        print(f"{pool:<18}{r['n_ref']:>3}  {Q['table_count'][2]:>7.1f} {Q['backend_count'][2]:>8.1f} "
              f"{Q['source_column_count'][2]:>7.1f} {Q['mart_count'][2]:>6.1f} {Q['mart_column_count'][2]:>7.1f} "
              f"{Q['join_count'][2]:>5.1f} {Q['aggregate_count'][2]:>4.1f} {r['load'][2]:>6.3f} "
              f"{r['transform'][2]:>9.3f} {r['combined'][2]:>8.3f}  {r['bands']}")
    print()
    print("computed-column share (upstream tagger on mart column prose):")
    for pool in order:
        r = report.get(pool)
        if not r or not r["n_ref"]:
            continue
        print(f"  {pool:<18} mart cols {r['mart_cols_total']:>5}  computed {r['computed_cols_total']:>5} "
              f"({r['computed_pct']}%)  op families {r['distinct_op_families']:>2}  "
              f"extrema tasks {r['tag_tasks']['extrema']:>3}  distinct tasks {r['tag_tasks']['distinct']:>3}")
    print()
    print("tag x pool (columns / tasks):")
    hdr = "  %-14s" % "tag" + "".join(f"{p[:9]:>14}" for p in order if report.get(p, {}).get("n_ref"))
    print(hdr)
    for t in ALL_TAGS:
        line = "  %-14s" % t
        for p in order:
            r = report.get(p)
            if not r or not r["n_ref"]:
                continue
            line += f"{r['tag_cols'][t]:>7}/{r['tag_tasks'][t]:<6}"
        print(line)


if __name__ == "__main__":
    main()

"""Discriminating power + structural (not prose) computed-column measure."""
from __future__ import annotations
import json, sys, importlib.util, statistics
from pathlib import Path
from collections import Counter
from elt_taskgen.models import TaskIR, MartOpKind
from elt_taskgen.generation.mart_plan import attack_surface

BENCH = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-Bench")
spec = importlib.util.spec_from_file_location("ua", BENCH / "analysis" / "analyze.py")
up = importlib.util.module_from_spec(spec); sys.modules["ua"] = up; spec.loader.exec_module(up)

POOLS = {
    "eltbench_anchor": ("/tmp/anchor_baseline_ws/reference/anchors", None),
    "synsql": ("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen/ws-difficulty-synsql/tasks", "synsql__"),
    "fivetran": ("/tmp/ws_fivetran/tasks", "dbt__"),
    "dlt": ("/tmp/diffws/tasks", "dlt__"),
    "wikidbs": ("/tmp/diffws/tasks", "wikidbs__"),
    "schemapile": ("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen/taskgen-workspace/tasks", "schemapile__"),
    "demo": ("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen/taskgen-workspace/tasks", "demo__"),
}


def tasks(pool):
    root, prefix = POOLS[pool]
    p = Path(root)
    if prefix is None:
        return [TaskIR.model_validate_json(f.read_text()) for f in sorted(p.glob("*.json"))]
    out = []
    for d in sorted(p.iterdir()):
        if not d.is_dir() or not d.name.startswith(prefix):
            continue
        ak = d / "answer_key"
        if not (ak.is_dir() and any(ak.iterdir())):
            continue
        out.append(TaskIR.model_validate_json((d / "task_ir.json").read_text()))
    return out


def structural_computed(task):
    """Columns a solver must COMPUTE, judged from the plan, not the prose.

    key columns          -> not computed
    columns named by an AGGREGATE / WINDOW op -> computed (measure)
    everything else      -> passthrough projection
    """
    key = keyed = comp = passthru = 0
    for m in task.marts:
        produced = set()
        for op in m.plan.ops:
            if op.kind in (MartOpKind.AGGREGATE, MartOpKind.WINDOW):
                produced |= set(op.columns)
        for c in m.columns:
            if c.name in m.key_columns:
                keyed += 1
            elif c.name in produced:
                comp += 1
            else:
                passthru += 1
    return keyed, comp, passthru


print(f"{'pool':<17}{'tasks':>6}{'martcols':>9}{'key':>6}{'computed':>9}{'passthru':>9}{'comp%':>7} "
      f"{'attacks/task med':>17} {'attack kinds':>13} {'surface kinds':>14}")
summary = {}
for pool in POOLS:
    ts = tasks(pool)
    if not ts:
        continue
    K = C = P = 0
    atk = []
    kinds = Counter()
    surf = Counter()
    opkinds_per_task = []
    for t in ts:
        k, c, p = structural_computed(t)
        K += k; C += c; P += p
        atk.append(len(t.attack_cases))
        for a in t.attack_cases:
            kinds[a.kind.value] += 1
        ks = set()
        for m in t.marts:
            for s in attack_surface(m.plan):
                surf[s] += 1
            ks |= {o.kind.value for o in m.plan.ops}
        opkinds_per_task.append(len(ks))
    tot = K + C + P
    summary[pool] = dict(tasks=len(ts), martcols=tot, key=K, computed=C, passthru=P,
                         attacks_med=statistics.median(atk), attacks_tot=sum(atk),
                         attack_kinds=dict(kinds), surface=dict(surf),
                         opkinds_med=statistics.median(opkinds_per_task))
    print(f"{pool:<17}{len(ts):>6}{tot:>9}{K:>6}{C:>9}{P:>9}{100*C/tot:>6.1f}% "
          f"{statistics.median(atk):>17.1f} {len(kinds):>13} {len(surf):>14}")

print()
print("attack-case kinds actually declared:")
for pool, s in summary.items():
    print(f"  {pool:<17} total={s['attacks_tot']:<5} {s['attack_kinds']}")
print()
print("attack SURFACE available in mart plans (kind -> marts offering it):")
for pool, s in summary.items():
    print(f"  {pool:<17} {s['surface']}")
print()
print("distinct MartOpKind per task (median):")
for pool, s in summary.items():
    print(f"  {pool:<17} {s['opkinds_med']}")
Path("/tmp/final_synth/discrim.json").write_text(json.dumps(summary, indent=1))

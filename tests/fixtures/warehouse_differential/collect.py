"""Differential evidence: the same solver expression on a real warehouse and on
the pinned DuckDB engine, through the grader's own rewrite.

For each probe the candidate SQL is what a solver writes for its destination.
The DuckDB side is produced by training.dbt_runner.rewrite_model_sql, i.e. the
exact text the local grader would execute, and is run on the PINNED engine
(runtime-images/dbt-duckdb, DuckDB 1.4.5) in a subprocess. Each value is
recorded with its driver column type and compared by `compare.py`: exact typed
agreement first, then agreement under the scorer's own comparator. Nothing is
written to any warehouse and no credential value is printed.
"""
import json, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(HERE))
from probes import PROBES, candidate_sql
from compare import COLLECTOR_VERSION, REWARD_RULE_ORDER, display, encode, error_kind, outcome, typed
from elt_taskgen.destinations import Destination
from elt_taskgen.training.contract import WORKSPACE_SCORER_VERSION
from elt_taskgen.training import dbt_runner as R
from elt_taskgen.runtime import snowflake as sf, databricks as db, redshift as rs

RUNTIME_PY = ROOT / "runtime-images" / "dbt-duckdb" / ".venv" / "bin" / "python"
SOURCE = "{{ source('raw', 't') }}"
TARGETS = {
    "snowflake": (Destination.SNOWFLAKE, sf, ROOT / "secrets/snowflake-admin.json"),
    "databricks": (Destination.DATABRICKS, db, ROOT / "secrets/databricks-admin.json"),
    "redshift": (Destination.REDSHIFT, rs, ROOT / "secrets/redshift-admin.json"),
}


def duckdb_sql(probe: dict, destination: Destination) -> tuple[str, str]:
    """(status, sql) where status is 'admitted' or a refusal code.

    The grader receives the solver's destination-dialect text, so the rewrite
    is fed exactly the candidate SQL rendered for that destination.
    """
    candidate = candidate_sql(probe, destination)
    frm = probe.get("from_clause")
    model = candidate if frm else f"{candidate} FROM {SOURCE}"
    try:
        rewritten = R.rewrite_model_sql(model, destination)
        return "admitted", rewritten.replace(SOURCE, "(SELECT 1) AS t")
    except R.DbtPolicyFailure as exc:
        status = exc.code.value
    except Exception as exc:  # noqa: BLE001
        status = f"rewrite_error:{type(exc).__name__}"
    # The measurement must survive a policy change: when the subset refuses the
    # construct, the two ENGINES are still compared, by transpiling the
    # candidate to DuckDB directly. `subset` records the refusal separately, so
    # the evidence that justified a rule cannot be erased by that rule.
    import sqlglot

    executable = model.replace(SOURCE, "(SELECT 1) AS t")
    try:
        transpiled = sqlglot.transpile(executable, read=destination.value, write="duckdb")[0]
    except Exception:  # noqa: BLE001
        return status, ""
    # Redshift's bare text type carries a MAX length that DuckDB cannot parse;
    # the grader's own rewrite folds it, and so must this fallback.
    return status, transpiled.replace("VARCHAR(MAX)", "TEXT").replace("TEXT(MAX)", "TEXT")


def native_duckdb_sql(probe: dict, destination: Destination) -> tuple[str, str]:
    """(status, sql) for a native probe: the destination's own full statement,
    through the same rewrite and the same measured fallback as duckdb_sql."""
    candidate = probe["native"][destination.value]
    try:
        return "admitted", R.rewrite_model_sql(candidate, destination)
    except R.DbtPolicyFailure as exc:
        status = exc.code.value
    except Exception as exc:  # noqa: BLE001
        status = f"rewrite_error:{type(exc).__name__}"
    import sqlglot

    try:
        transpiled = sqlglot.transpile(candidate, read=destination.value, write="duckdb")[0]
    except Exception:  # noqa: BLE001
        return status, ""
    return status, transpiled.replace("VARCHAR(MAX)", "TEXT").replace("TEXT(MAX)", "TEXT")


def run_duckdb(statements: dict[str, str]) -> dict[str, dict]:
    payload = json.dumps(statements)
    # The subprocess records each value with `compare.encode`, losslessly and
    # with its column type, so nothing is compared as display text.
    script = (
        "import json,sys,duckdb\n"
        f"sys.path.insert(0, {str(HERE)!r})\n"
        "from compare import encode\n"
        "stmts=json.loads(sys.stdin.read())\n"
        "con=duckdb.connect(':memory:')\n"
        "con.execute(\"SET TimeZone='UTC'\")\n"
        "out={}\n"
        "for key,sql in stmts.items():\n"
        "    try:\n"
        "        cur=con.execute(sql); rows=cur.fetchall()\n"
        "        out[key]={'ok':True,'value':encode(rows[0][0], str(cur.description[0][1])),'shape':[len(rows),len(cur.description)],'engine':duckdb.__version__}\n"
        "    except Exception as exc:\n"
        "        out[key]={'ok':False,'error':f'{type(exc).__name__}: {str(exc)[:160]}','engine':duckdb.__version__}\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run([str(RUNTIME_PY), "-W", "ignore", "-c", script], input=payload,
                          capture_output=True, text=True, check=False)
    line = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    if not line:
        raise RuntimeError(f"duckdb runner produced no result: {proc.stderr[-300:]}")
    return json.loads(line[-1])


def main() -> None:
    name = sys.argv[1]
    if "--native" in sys.argv[2:]:
        from native_probes import NATIVE_PROBES

        collect(name, NATIVE_PROBES, native_duckdb_sql,
                lambda probe, destination: probe["native"][destination.value],
                f"native_{name}.json")
    else:
        collect(name, PROBES, duckdb_sql, candidate_sql, f"differential_{name}.json")


def collect(name, probes, prepare, candidate_for, output) -> None:
    destination, mod, path = TARGETS[name]
    prepared = {p["id"]: prepare(p, destination) for p in probes}
    duck_stmts = {pid: sql for pid, (_status, sql) in prepared.items() if sql}
    duck = run_duckdb(duck_stmts)

    results = []
    conn = mod.connect(mod.load_credentials(path))
    try:
        # Redshift is transactional: one failed statement aborts the block and
        # every later probe returns 25P02 until a rollback. Autocommit where the
        # driver offers it, and roll back after every failure regardless.
        try:
            conn.autocommit = True
        except Exception:  # noqa: BLE001 - not every driver exposes it
            pass

        def rollback() -> None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

        cursor = conn.cursor()
        for statement in (
            "ALTER SESSION SET TIMEZONE = 'UTC'",
            "SET TIME ZONE 'UTC'",
            "SET timezone TO 'UTC'",
        ):
            try:
                cursor.execute(statement); break
            except Exception:
                rollback(); continue
        for probe in probes:
            pid = probe["id"]
            status, _ = prepared[pid]
            candidate = candidate_for(probe, destination)
            entry = {"id": pid, "rule": probe["rule"], "subset": status,
                     "candidate_sql": candidate}
            warehouse_value = duckdb_value = None
            warehouse_error_kind = None
            try:
                cursor.execute(candidate)
                rows = cursor.fetchall()
                warehouse_value = encode(rows[0][0], cursor.description[0][1])
                entry["warehouse"] = display(typed(warehouse_value, name))
                entry["warehouse_type"] = warehouse_value["py"]
                entry["warehouse_value"] = warehouse_value
                entry["warehouse_shape"] = [len(rows), len(cursor.description)]
            except Exception as exc:
                entry["warehouse"] = None
                entry["warehouse_error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
                warehouse_error_kind = error_kind(exc)
                entry["warehouse_error_kind"] = warehouse_error_kind
                rollback()
            record = duck.get(pid)
            if record and record.get("ok"):
                duckdb_value = record["value"]
                entry["duckdb"] = display(typed(duckdb_value, "duckdb"))
                entry["duckdb_type"] = duckdb_value["py"]
                entry["duckdb_value"] = duckdb_value
                entry["duckdb_shape"] = record["shape"]
                entry["duckdb_engine"] = record["engine"]
            else:
                entry["duckdb"] = None
                entry["duckdb_error"] = (record or {}).get("error", "not executed")
            entry.update(outcome(warehouse_value, warehouse_error_kind, duckdb_value, name))
            if entry.get("warehouse_shape") and entry.get("duckdb_shape"):
                entry["shape_agreement"] = entry["warehouse_shape"] == entry["duckdb_shape"]
            results.append(entry)
    finally:
        try: conn.close()
        except Exception: pass
    out = HERE / output
    import sqlglot

    document = {
        "destination": name,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collector_version": COLLECTOR_VERSION,
        "reward_rule": {
            "comparator": "elt_taskgen.verification.upstream_eval._vectors_match",
            "order": REWARD_RULE_ORDER,
            "workspace_scorer_version": WORKSPACE_SCORER_VERSION,
        },
        "rewrite": {
            "subset_version": R.DBT_COMPATIBILITY_SUBSET_VERSION,
            "sqlglot": sqlglot.__version__,
        },
        "probes": results,
    }
    out.write_text(json.dumps(document, indent=2))
    counts: dict[str, int] = {}
    for entry in results:
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
    print(name, counts, "->", out)


if __name__ == "__main__":
    main()

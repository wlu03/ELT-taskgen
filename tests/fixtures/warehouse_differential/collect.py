"""Differential evidence: the same solver expression on a real warehouse and on
the pinned DuckDB engine, through the grader's own rewrite.

For each probe the candidate SQL is what a solver writes for its destination.
The DuckDB side is produced by training.dbt_runner.rewrite_model_sql, i.e. the
exact text the local grader would execute, and is run on the PINNED engine
(runtime-images/dbt-duckdb, DuckDB 1.4.5) in a subprocess. Values are rendered
canonically and compared both exactly and under the evaluator's numeric
tolerance. Nothing is written to any warehouse and no credential value is
printed.
"""
import json, subprocess, sys, time
from decimal import Decimal
from pathlib import Path

ROOT = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(HERE))
from probes import PROBES, candidate_sql
from elt_taskgen.destinations import Destination
from elt_taskgen.training import dbt_runner as R
from elt_taskgen.runtime import snowflake as sf, databricks as db, redshift as rs

RUNTIME_PY = ROOT / "runtime-images" / "dbt-duckdb" / ".venv" / "bin" / "python"
SOURCE = "{{ source('raw', 't') }}"
TARGETS = {
    "snowflake": (Destination.SNOWFLAKE, sf, ROOT / "secrets/snowflake-admin.json"),
    "databricks": (Destination.DATABRICKS, db, ROOT / "secrets/databricks-admin.json"),
    "redshift": (Destination.REDSHIFT, rs, ROOT / "secrets/redshift-admin.json"),
}
ABS_TOL, REL_TOL = 1e-9, 1e-2


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


def render(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def numerically_close(a: str, b: str) -> bool:
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(x - y) <= max(ABS_TOL, REL_TOL * max(abs(x), abs(y)))


def run_duckdb(statements: dict[str, str]) -> dict[str, dict]:
    payload = json.dumps(statements)
    # The subprocess renders values with the SAME canonical function, so the
    # comparison never depends on a repr round trip.
    script = (
        "import json,sys,duckdb\n"
        "from decimal import Decimal\n"
        "def render(value):\n"
        "    if value is None: return 'NULL'\n"
        "    if isinstance(value, bool): return 'true' if value else 'false'\n"
        "    if isinstance(value, Decimal): return format(value.normalize(), 'f')\n"
        "    if isinstance(value, float): return repr(value)\n"
        "    if isinstance(value, (bytes, bytearray)): return value.hex()\n"
        "    return str(value)\n"
        "stmts=json.loads(sys.stdin.read())\n"
        "con=duckdb.connect(':memory:')\n"
        "con.execute(\"SET TimeZone='UTC'\")\n"
        "out={}\n"
        "for key,sql in stmts.items():\n"
        "    try:\n"
        "        cur=con.execute(sql); row=cur.fetchone()\n"
        "        out[key]={'ok':True,'value':render(row[0]),'type':type(row[0]).__name__,'engine':duckdb.__version__}\n"
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
    destination, mod, path = TARGETS[name]
    prepared = {p["id"]: duckdb_sql(p, destination) for p in PROBES}
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
        for probe in PROBES:
            pid = probe["id"]
            status, _ = prepared[pid]
            candidate = candidate_sql(probe, destination)
            entry = {"id": pid, "rule": probe["rule"], "subset": status,
                     "candidate_sql": candidate}
            try:
                cursor.execute(candidate)
                row = cursor.fetchone()
                entry["warehouse"] = render(row[0])
                entry["warehouse_type"] = type(row[0]).__name__
            except Exception as exc:
                entry["warehouse"] = None
                entry["warehouse_error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
                rollback()
            record = duck.get(pid)
            if record and record.get("ok"):
                entry["duckdb"] = record["value"]
                entry["duckdb_type"] = record["type"]
                entry["duckdb_engine"] = record["engine"]
            else:
                entry["duckdb"] = None
                entry["duckdb_error"] = (record or {}).get("error", "not executed")
            w, d = entry.get("warehouse"), entry.get("duckdb")
            textual = entry.get("warehouse_type") == "str" or entry.get("duckdb_type") == "str"
            if w is None or d is None:
                entry["verdict"] = "not_comparable"
            elif w == d:
                entry["verdict"] = "identical"
            elif not textual and numerically_close(w, d):
                # Numeric tolerance is the evaluator's; TEXT is compared byte
                # for byte because hashes and concatenations consume it.
                entry["verdict"] = "within_tolerance"
            else:
                entry["verdict"] = "DIFFERENT"
            results.append(entry)
    finally:
        try: conn.close()
        except Exception: pass
    out = HERE / f"differential_{name}.json"
    out.write_text(json.dumps({"destination": name, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "probes": results}, indent=2))
    counts: dict[str, int] = {}
    for entry in results:
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
    print(name, counts, "->", out)


if __name__ == "__main__":
    main()

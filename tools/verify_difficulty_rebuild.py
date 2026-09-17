"""Rebuild a per-pool sample through ingest -> generate -> reference, for real.

WHY THIS EXISTS
The difficulty claims in docs/difficulty/ are only worth what the pipeline
actually produces. `tools/measure_track4_after.py` rebuilt TaskIRs from the
adapters alone, which cannot show whether the new plan library COMPILES and
EXECUTES: a mart plan that validates but blows up in DuckDB never reaches the
reference stage, and a task that never reaches reference has no gold, no
compiled SQL, and therefore no measurable computed-column count.

This script drives the SAME path the CLI drives (`adapter.to_task_ir` ->
`Engine.register` -> `Engine.run(until="reference")`, which is the documented
programmatic entry point) over a named per-pool sample, and reports for every
task whether it reached reference or died and where. It computes no statistic;
`tools/verify_difficulty_measure.py` reads the workspace it writes.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SYNSQL_DBS = [
    "3d_coordinate_system_for_spatial_data_management",
    "3d_graphics_material_properties_and_rendering",
    "3d_object_modeling_and_rotation_analysis",
    "3d_path_or_motion_tracking",
    "abalone_physical_characteristics_and_weight_analysis",
    "academic_faculty_and_research_management_681390",
    "american_football_team_performance_analysis",
    "astronomical_observations_and_data_analysis",
    "autonomous_vehicle_radar_and_odometry_data_management",
    "__biological_data_analysis_specifically_related_to_peptide_binding_and_al",
    "__cache_management_systems__",
    "weather_data_collection_and_analysis_342037",
]

#: (record key, cluster id) — the cluster is the family namespace and the
#: adapter tokenizes it, so it is never None.
SCHEMAPILE_KEYS = [
    ("014967_migration.sql", "github_com_adisreyaj_compito_5780f2174c3c"),
    ("390604_MySql_Create_Common_Tables.sql",
     "github_com_chenlianwd_autosolderwebapp_a42096dee593"),
    ("542456_000001_create_db.up.sql",
     "github_com_findy_network_findy_agent_vault_868ad8fbc9b5"),
    ("045413_schema-047.sql", "github_com_kykrueger_openbis_c106a4e57b99"),
    ("656145_schema-186.sql", "github_com_kykrueger_openbis_c106a4e57b99"),
    ("438282_pg.dump.sql", "github_com_nmahendra_projcrm_fd3a7df5b9bb"),
    ("230521_V1__Initial_Setup.sql",
     "github_com_sachin_awati_mojito_d536723c35a9"),
    ("013270_Dump20120814-2.sql",
     "github_com_uprm_gaming_virtual_factory_d1969304d7ab"),
]

WIKIDBS_DIRS = [
    "00022 Chromo_Domain_Superfamily_Db",
    "00041 UridineResearchPublications",
    "00050 UNIVERSITY_HUMANITIES_ECONOMICS_LODZ_STAFF",
    "00058 HansAbichFilmography",
    "00077 EhimePrefecturalRoadsNetwork",
    "00128 EauClaireDeathRecords",
    "00134 StephanEisenhardtPublications",
    "00167 InezCourtneyFilmography",
    "00235 iditarod_race_participants",
    "00272 GeographicalScholarsDatabase",
    "00286 EWA_DAKOWSKA_FILMOGRAPHY",
    "00291 NIKOLA_KOVACHEV_FOOTBALL_CAREER",
]

FIVETRAN_PACKAGES = [
    "dbt_reddit_ads",
    "dbt_apple_search_ads",
    "dbt_snapchat_ads",
    "dbt_servicenow",
    "dbt_amazon_ads",
    "dbt_twitter",
]


def _pool_root(pool: str, sources_config: Path | None = None) -> Path:
    """Resolve a source root through the same catalog used by production ingest."""

    from elt_taskgen.catalog import load_source_catalog

    return load_source_catalog(sources_config).pool(pool).root_path()


def _tasks_synsql(*, sources_config: Path | None = None) -> list:
    from elt_taskgen.adapters import synsql

    tj = _pool_root("synsql", sources_config) / "tables.json"
    out = []
    for db in SYNSQL_DBS:
        out.append(("synsql", db, lambda db=db: [synsql.to_task_ir(db, tj)]))
    return out


def _tasks_schemapile(*, sources_config: Path | None = None) -> list:
    from elt_taskgen.adapters import schemapile as sp

    src = _pool_root("schemapile", sources_config) / "schemapile-perm.json"
    out = []
    for key, cluster in SCHEMAPILE_KEYS:
        def build(key=key, cluster=cluster):
            rec = sp.find_record(src, key)
            return [
                sp.to_task_ir(
                    rec,
                    key=key,
                    cluster=cluster,
                    filt=sp.RelationalFilter(max_tables=200),
                )
            ]

        out.append(("schemapile", key, build))
    return out


def _tasks_wikidbs(*, sources_config: Path | None = None) -> list:
    from elt_taskgen.adapters import wikidbs as w

    root = _pool_root("wikidbs", sources_config)
    found: dict[str, Path] = {}
    for part in sorted(p for p in root.glob("part-*") if p.is_dir()):
        for d in part.iterdir():
            if d.is_dir() and d.name in WIKIDBS_DIRS:
                found[d.name] = d
    out = []
    for name in WIKIDBS_DIRS:
        d = found.get(name)
        if d is None:
            out.append(("wikidbs", name, None))
            continue
        out.append(("wikidbs", name, lambda d=d: [w.to_task_ir(d, wikidbs_root=root)]))
    return out


def _tasks_dlt() -> list:
    from elt_taskgen.adapters import dlt as dl

    out = []
    for f in sorted((REPO / "config" / "dlt_connectors").glob("*.yaml")):
        out.append(
            ("dlt", f.stem, lambda f=f: [dl.to_task_ir(dl.load_connector(f))])
        )
    return out


def _tasks_fivetran(workspace: Path) -> list:
    from elt_taskgen.adapters import dbt as dbt_ad

    out = []
    for pkg in FIVETRAN_PACKAGES:
        def build(pkg=pkg):
            manifest = _fivetran_manifest(pkg, workspace)
            spec = dbt_ad.load_manifest(manifest)
            spec = spec.model_copy(update={"package_name": pkg[len("dbt_"):]})
            res = dbt_ad.extract_candidates(spec, pool="dbt")
            for s in res.skipped:
                print(f"    skipped cut: {s.reason}")
            return list(res.tasks)

        out.append(("fivetran", pkg, build))
    return out


def _fivetran_manifest(pkg: str, workspace: Path) -> Path:
    """A COMPILED manifest, or nothing — a parse-only manifest ships Jinja."""
    cands = [
        workspace / "dbt_builds" / pkg / "package" / "integration_tests"
        / "target" / "manifest.json",
    ]
    for cand in cands:
        if cand.is_file() and _is_compiled(cand):
            return cand
    import subprocess

    build_root = workspace / "dbt_builds" / pkg
    build_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "build_dbt_manifest.py"),
            "--package",
            pkg,
            "--build-root",
            str(build_root),
        ],
        check=True,
    )
    built = build_root / "package" / "integration_tests" / "target" / "manifest.json"
    if not _is_compiled(built):
        raise RuntimeError(f"{built} carries no compiled_code (parse-only)")
    return built


def _is_compiled(path: Path) -> bool:
    m = json.loads(path.read_text())
    models = [n for n in m.get("nodes", {}).values()
              if n.get("resource_type") == "model"]
    return bool(models) and all(n.get("compiled_code") for n in models)


def _engine(workspace: Path):
    """The SAME engine the CLI builds — stage runners wired, no guessing."""
    import argparse

    from elt_taskgen.cli import _make_engine

    args = argparse.Namespace(
        workspace=workspace,
        max_repair_rounds=3,
        provider=None,
        model=None,
        offline=True,
        empirical=False,
        variants=None,
        recalibrate=False,
    )
    return _make_engine(args, echo=None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild a per-pool sample through deterministic reference freeze."
    )
    parser.add_argument("workspace", nargs="?", default="/tmp/verify_ws")
    parser.add_argument(
        "pools",
        nargs="?",
        help="optional comma-separated pool filter (legacy positional form)",
    )
    parser.add_argument(
        "--sources-config",
        type=Path,
        default=None,
        help="alternate source catalog; roots otherwise resolve through config/sources.yaml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = Path(args.workspace).resolve()
    only = set(args.pools.split(",")) if args.pools else None
    sources_config = args.sources_config.resolve() if args.sources_config else None
    workspace.mkdir(parents=True, exist_ok=True)

    specs = (
        _tasks_synsql(sources_config=sources_config)
        + _tasks_fivetran(workspace)
        + _tasks_schemapile(sources_config=sources_config)
        + _tasks_wikidbs(sources_config=sources_config)
        + _tasks_dlt()
    )
    results = []
    engine = _engine(workspace)
    try:
        for pool, name, build in specs:
            if only and pool not in only:
                continue
            if build is None:
                results.append(
                    {"pool": pool, "record": name, "stage": "ingest",
                     "ok": False, "error": "record not found on disk"}
                )
                print(f"[{pool}] {name}: INGEST MISSING")
                continue
            try:
                tasks = build()
            except Exception as exc:
                results.append(
                    {"pool": pool, "record": name, "stage": "ingest", "ok": False,
                     "error": f"{type(exc).__name__}: {exc}"}
                )
                print(f"[{pool}] {name}: INGEST FAILED {type(exc).__name__}: {exc}")
                continue
            for task in tasks:
                row = {"pool": pool, "record": name, "task_id": task.task_id}
                try:
                    engine.register(task)
                except Exception as exc:
                    row.update(stage="register", ok=False,
                               error=f"{type(exc).__name__}: {exc}")
                    results.append(row)
                    print(f"[{pool}] {task.task_id}: REGISTER FAILED {exc}")
                    continue
                try:
                    engine.run(task.task_id, until="generate")
                except Exception as exc:
                    row.update(stage="generate", ok=False,
                               error=f"{type(exc).__name__}: {exc}",
                               trace=traceback.format_exc()[-1500:])
                    results.append(row)
                    print(f"[{pool}] {task.task_id}: GENERATE FAILED "
                          f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    engine.run(task.task_id, until="reference")
                except Exception as exc:
                    row.update(stage="reference", ok=False,
                               error=f"{type(exc).__name__}: {exc}",
                               trace=traceback.format_exc()[-1500:])
                    results.append(row)
                    print(f"[{pool}] {task.task_id}: REFERENCE FAILED "
                          f"{type(exc).__name__}: {exc}")
                    continue
                rep = engine.latest_report(task.task_id, "reference")
                ok = rep is not None and rep.verdict == "pass"
                row.update(stage="reference", ok=ok,
                           verdict=None if rep is None else rep.verdict)
                results.append(row)
                print(f"[{pool}] {task.task_id}: "
                      f"{'REFERENCE OK' if ok else 'REFERENCE VERDICT ' + str(row.get('verdict'))}")
    finally:
        engine.close()

    out = workspace / "rebuild_report.json"
    prev = json.loads(out.read_text()) if out.is_file() else []
    keep = [r for r in prev
            if not any(r.get("task_id") == n.get("task_id")
                       and r.get("record") == n.get("record") for n in results)]
    out.write_text(json.dumps(keep + results, indent=2) + "\n")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n{ok}/{len(results)} reached reference; report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

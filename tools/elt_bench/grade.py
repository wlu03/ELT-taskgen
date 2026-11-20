"""Capture an agent submission and score it with taskgen's warehouse evaluator."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import subprocess
import re
import shutil
import sys

import snowflake.connector
import yaml

from common import environment_variables, read, write
from elt_taskgen.runtime.airbyte import AirbyteSyncFailure
from elt_taskgen.runtime.evaluation import evaluate_stage1, evaluate_stage2
from elt_taskgen.runtime.execution import ExecutionError, Stage2PreflightError, run_stage2_submission
from elt_taskgen.runtime.process import DockerRunner, ProcessFailure
from elt_taskgen.runtime.process_compliance import find_terraform_state, terraform_state_connections
from elt_taskgen.runtime.snowflake import quote_identifier
from elt_taskgen.runtime.source_environment import SourceEnvironment, _compose_document, _safe_project
from elt_taskgen.runtime.source_server import load_routes

HIDDEN=("primary","resampled","counterfactual","stress")


def submission_files(workspace,out):
    elt=workspace/"elt"
    save=out/"submission"
    save.mkdir(mode=0o700)
    cfg=yaml.safe_load((workspace/"config.yaml").read_text()) if (workspace/"config.yaml").is_file() else {}
    value=((cfg.get("Airbyte") or {}).get("config") or {}).get("password")
    redactions=[value] if isinstance(value,str) and value else []
    key=workspace/"snowflake_key.p8"
    if key.is_file() and not key.is_symlink():
        redactions.extend(key.read_text().splitlines()[1:-1])
    forbidden={".terraform","target","logs","dbt_packages"}
    for src in sorted(elt.rglob("*")):
        if src.is_symlink() or not src.is_file() or src.stat().st_size>1_000_000:
            continue
        rel=src.relative_to(elt)
        if forbidden.intersection(rel.parts) or (
                src.suffix not in {".tf",".sql",".yaml",".yml"} and not src.name.endswith(".tf.json")):
            continue
        raw=src.read_text(errors="replace")
        raw=re.sub(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
                   "[scoped private key]",raw,flags=re.S)
        for value in redactions:
            raw=raw.replace(value,"[scoped credential]")
        dst=save/rel
        dst.parent.mkdir(parents=True,exist_ok=True)
        dst.write_text(raw)
    return sum(p.is_file() for p in save.rglob("*"))


def drop_attempt_schemas(connection,database):
    with connection.cursor() as c:
        c.execute(f"SHOW SCHEMAS IN DATABASE {quote_identifier(database)}")
        columns=[item[0].casefold() for item in c.description]
        names=[row[columns.index("name")] for row in c.fetchall() if row[columns.index("name")]!="INFORMATION_SCHEMA"]
        for name in names:
            quoted='"'+str(name).replace('"','""')+'"'
            c.execute(f"DROP SCHEMA IF EXISTS {quote_identifier(database)}.{quoted}")


def submission_failure(client, connection_id, job_id, s3_definition):
    job=client.request("POST","../../v1/jobs/get",{"id":job_id})
    for record in job.get("attempts",[]):
        attempt=record.get("attempt",record)
        summary=attempt.get("failureSummary") or {}
        for failure in summary.get("failures",[]):
            if failure.get("failureType") != "config_error":
                continue
            if "Catalog must have at least one stream" in str(failure.get("stacktrace", "")):
                return "empty_stream_selection"
            if (s3_definition and failure.get("failureOrigin") == "source"
                and "No files were identified in the stream" in str(failure.get("internalMessage", ""))):
                connection = client.request("GET", f"connections/{connection_id}")
                source = client.request("GET", f"sources/{connection['sourceId']}")
                if source.get("definitionId") != s3_definition:
                    continue
                streams = (source.get("configuration") or {}).get("streams") or []
                if streams and all(isinstance(stream, dict) and not stream.get("globs") for stream in streams):
                    return "missing_s3_globs"
    return None


def sync_one(client, connection_id):
    try:
        receipt = client.trigger_and_wait_receipt([connection_id], poll_interval=8, timeout=480)
    except AirbyteSyncFailure as failure:
        return connection_id, failure.job_id, failure.status
    return connection_id, receipt.job_ids[connection_id], receipt.statuses[connection_id]


def sync_results(client, connection_ids, workers):
    def sync(connection_id):
        return sync_one(client, connection_id)

    if workers == 1:
        yield from map(sync, connection_ids)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            yield from pool.map(sync, connection_ids)


def stage_grading_inputs(parent, answer, record):
    trusted = parent / record["task_id"]
    semantic = trusted / "semantic"
    semantic.mkdir(parents=True)
    shutil.copyfile(Path(record["frozen_populations"]).parent / "task_ir.json", semantic / "task_ir.json")
    staged = trusted / "answer_key"
    shutil.copytree(answer, staged)
    return staged


def grade_and_capture(workspace,out,public,answer,record,credential,key,direct,source_env,workspace_id,
                      *,capture_submission=True,sync_workers=1,dbt_host_runner=None):
    running = subprocess.check_output(["docker","ps","--filter","name=spider-gpt4o-","--format","{{.Names}}"],text=True)
    if running.strip():
        raise RuntimeError("Agent container remained active before hidden replay")
    summary={}
    if capture_submission:
        summary["submitted_files"]=submission_files(workspace,out)
    db=record["database"]
    airbyte_config = yaml.safe_load((public / "config.yaml").read_text())["Airbyte"]["config"]
    s3_definition = airbyte_config.get("s3_definition_id")
    state=find_terraform_state(workspace/"elt")
    submitted=terraform_state_connections(state) if state else {}
    registered={r["connectionId"] for r in direct.list_connections(workspace_id)}
    connection_ids=sorted(set(submitted)&registered)
    summary.update(registered_connections=len(registered),terraform_connections=len(submitted),
                   replay_connections=len(connection_ids))
    answer=stage_grading_inputs(workspace.parent/"trusted",answer,record)
    params={**credential,"authenticator":"SNOWFLAKE_JWT","private_key":key,"login_timeout":15,"network_timeout":45}
    with snowflake.connector.connect(**params) as con:
        el=evaluate_stage1(con,answer,population="development",database=db)
        write(out/"development_stage1.json",asdict(el))
        summary["development_stage1_passed"]=el.passed
        if el.passed:
            transform=evaluate_stage2(con,public,answer,population="development",database=db)
            write(out/"development_stage2.json",asdict(transform))
            summary["development_marts_passed"]=sum(transform.mart_scores.values())
        if not connection_ids:
            summary["hidden_status"]="no_submitted_terraform_connections"
            return summary
        projects=[p for p in (workspace/"elt").rglob("dbt_project.yml") if not p.is_symlink() and "dbt_packages" not in p.parts]
        profiles=[p for p in (workspace/"elt").rglob("profiles.yml") if not p.is_symlink() and "dbt_packages" not in p.parts]
        runner=DockerRunner(workspace,environment_variables()["DBT_SNOWFLAKE_RUNNER_IMAGE"],
                            lane="cloud-egress",timeout=240,host_runner=dbt_host_runner)
        source_env.stop()
        hidden={}
        for population in HIDDEN:
            print("replay",population,flush=True)
            drop_attempt_schemas(con,db)
            parent=source_env.environment_dir.parent
            env_dir=parent/("sources-"+population)
            env_dir.mkdir()
            project=_safe_project(record["task_id"],population,str(parent))
            rendered=Path(record["frozen_populations"])/population/"rendered"
            network=project+"-source-network"
            replay=SourceEnvironment(source_env.release_dir,source_env.task_id,population,env_dir,
                rendered,source_env.manifest_path,env_dir/"compose.yaml",project,network)
            server_path=Path(sys.modules[load_routes.__module__].__file__)
            replay.compose_path.write_text(yaml.safe_dump(_compose_document(
                rendered_root=rendered,manifest_path=replay.manifest_path,server_script=server_path,network=network)))
            replay.start()
            result={}
            try:
                replay.seed()
                result.update(jobs={},sync_status={},submission_failures={})
                for conn, job, status in sync_results(direct,connection_ids,sync_workers):
                    result["jobs"][conn]=job
                    result["sync_status"][conn]=status
                    hidden[population]=result
                    write(out/"hidden_progress.json",hidden)
                    if status!="succeeded":
                        reason = submission_failure(direct, conn, job, s3_definition)
                        if not reason:
                            raise AirbyteSyncFailure(conn,job,status)
                        result["submission_failures"][conn]=reason
                el=evaluate_stage1(con,answer,population=population,database=db)
                write(out/f"{population}_stage1.json",asdict(el))
                result["stage1_passed"]=el.passed and all(x=="succeeded" for x in result["sync_status"].values())
                if result["stage1_passed"] and len(projects)==len(profiles)==1:
                    try:
                        run_stage2_submission(workspace,runner=runner,project_dir=projects[0].parent,
                                              profiles_dir=profiles[0].parent,
                                              snowflake_keypair_credentials=credential)
                    except (ExecutionError, ProcessFailure) as error:
                        if isinstance(error, Stage2PreflightError) and error.code == "dbt-version":
                            raise
                        result["stage2_execution_error"]=type(error).__name__
                    transform=evaluate_stage2(con,public,answer,population=population,database=db)
                    write(out/f"{population}_stage2.json",asdict(transform))
                    result["marts_passed"]=sum(transform.mart_scores.values())
                hidden[population]=result
                write(out/"hidden_progress.json",hidden)
            finally:
                replay.stop()
        summary["hidden"]=hidden
        summary["hidden_status"]="graded"
        return summary

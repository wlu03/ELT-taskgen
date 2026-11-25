"""Run and grade one attempt with the original ELT-Bench agent."""
import base64
from argparse import ArgumentParser
import fcntl
from http.server import ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import snowflake.connector
import yaml

from common import PROFILE, REPO, SanitizedCommandRunner, cleanup_snowflake, environment_variables, read, write
from elt_taskgen.runtime.airbyte import AirbyteClient, access_token_provider
from elt_taskgen.runtime.airbyte_gateway import AirbyteGateway, BasicCredential, ForwardResult, make_handler
from elt_taskgen.runtime.bootstrap import bootstrap_task_airbyte
from elt_taskgen.runtime.process import DockerRunner, ProcessFailure
from elt_taskgen.runtime.process_compliance import find_terraform_state, terraform_state_connections
from elt_taskgen.runtime.snowflake import _existing_owned, provision_attempt, quote_identifier
from elt_taskgen.runtime.source_environment import SourceEnvironment, _compose_document, _safe_project
from elt_taskgen.runtime.source_server import load_routes

BENCH = Path(os.environ.get("ELT_BENCH_DIR", str(REPO.parent / "ELT-Bench")))
SPIDER_PYTHON = Path(os.environ.get("ELT_SPIDER_PYTHON", REPO.parent / "elt_agent_eval_run/spidervenv/bin/python"))
SWE_PYTHON = Path(os.environ.get("ELT_SWE_PYTHON", REPO.parent / "elt_agent_eval_run/agentvenv/bin/python"))
EVALUATION_AGENTS = {
    "spider": ("ELT-Bench Spider-Agent", "gpt-4o", "OPENAI_API_KEY", SPIDER_PYTHON,
               "spider_worker.py", "spider-agent", "spider_result.json"),
    "swe": ("ELT-Bench SWE-agent", "claude-opus-4-7", "ANTHROPIC_API_KEY", SWE_PYTHON,
            "swe_worker.py", "SWE-agent", "swe_result.json"),
}
UUID=re.compile(r"[0-9a-fA-F-]{36}")
PREFIX="/api/public/v1/"


def prepare_airbyte(public, answer, task_id, client, workspace_id, config):
    boot = bootstrap_task_airbyte(public, answer, task_id, client, workspace_id=workspace_id)
    definitions = {"sources": set(boot.required_source_definition_ids),
                   "destinations": {boot.destination_definition_id}}
    airbyte = config["Airbyte"]["config"]
    if boot.custom_api_definition_id:
        definitions["sources"].add(boot.custom_api_definition_id)
        airbyte["custom_api_definition_id"] = boot.custom_api_definition_id
    if config.get("flat_files"):
        response = client.request("POST", "../../v1/source_definitions/create_custom", {
            "workspaceId": workspace_id,
            "sourceDefinition": {
                "name": "File (CA-trusting)",
                "dockerRepository": "ghcr.io/wlu03/elt-taskgen-source-file",
                "dockerImageTag": "0.6.0-ca-temporal1",
                "documentationUrl": "https://docs.airbyte.com/integrations/sources/file",
            },
        })
        file_definition = response.get("sourceDefinitionId", "")
        if not isinstance(file_definition, str) or not UUID.fullmatch(file_definition):
            raise ValueError("Airbyte did not return a definition ID for the TLS File connector")
        definitions["sources"].remove(airbyte["files_definition_id"])
        definitions["sources"].add(file_definition)
        airbyte["files_definition_id"] = file_definition
    return boot, definitions


def denied():
    return ForwardResult(403,"Forbidden",(("Content-Type","application/json"),),
                         b'{"message":"Airbyte request is outside this task workspace"}')


class GatewayBridge(AirbyteGateway):
    """Scope the existing gateway to task-owned objects and a short-lived password."""
    def __init__(self, scoped, shared, workspace, definitions, sfhost, sfdb, reads):
        self.shared = "Basic "+base64.b64encode(f"{shared['username']}:{shared['password']}".encode()).decode()
        super().__init__("http://127.0.0.1:8006", None, scoped, timeout=220)
        self.workspace=workspace
        self.definitions=definitions
        self.sfhost=sfhost
        self.sfdb=sfdb.casefold()
        self.reads=reads
        self.owned={name:set() for name in ("sources","destinations","connections","jobs")}
        self.source_definitions={}
        self.destination_configs={}
        self.job_connections = {}
        self.lock=threading.Lock()
        self.requests={}
        self.submission_rejections={}

    @staticmethod
    def _manual_schedule(body):
        schedule = body.get("schedule")
        return schedule is None or schedule == {"scheduleType": "manual"}

    def _source_config_allowed(self, value, field=""):
        if isinstance(value, dict):
            return all(self._source_config_allowed(v,k) for k,v in value.items())
        if isinstance(value,list):
            return all(self._source_config_allowed(v,field) for v in value)
        if not isinstance(value,str):
            return True
        name=field.lower()
        if name in {"host","hostname","endpoint","url","connection_string","connectionstring","url_base","urlbase","endpoint_url"}:
            try:
                host=urlsplit(value if "://" in value else "//"+value).hostname
            except ValueError:
                return False
            return host in self.reads or value.startswith("s3://") and host in self.reads
        return True

    def _source_update_allowed(self, source_id, body):
        if not set(body) <= {"name", "configuration", "definitionId", "workspaceId"}:
            return False
        if body.get("workspaceId", self.workspace) != self.workspace:
            return False
        definition = self.source_definitions.get(source_id)
        if not definition or body.get("definitionId", definition) != definition:
            return False
        return "configuration" not in body or self._source_configuration_valid(body["configuration"])

    def _source_configuration_valid(self, config):
        return (isinstance(config, dict) and not config.get("jdbc_url_params")
                and self._source_config_allowed(config))

    def _allowed(self, method, parts, query, body):
        first,*remaining=parts
        if first=="workspaces":
            return method=="GET" and not query and remaining and remaining[0]==self.workspace and (
                len(remaining)==1 or len(remaining)==3 and remaining[1]=="definitions" and remaining[2] in {"sources","destinations"})
        if first=="streams":
            source = query.get("sourceId", [])
            return (method=="GET" and not remaining and set(query)=={"sourceId"}
                    and len(source)==1 and source[0] in self.owned["sources"])
        if first not in self.owned:
            return False
        objects=self.owned[first]
        if remaining:
            if query or len(remaining)!=1 or remaining[0] not in objects:
                return False
            if method=="GET":
                return True
            if first=="sources" and method=="PUT":
                return not query and self._source_update_allowed(remaining[0], body)
            if first=="connections" and method=="PATCH":
                configurable={"name","namespaceDefinition","namespaceFormat","prefix","schedule",
                              "status","configurations","nonBreakingSchemaUpdatesBehavior","dataResidency"}
                return set(body)<=configurable and self._manual_schedule(body)
            return False
        if method=="GET":
            if first == "jobs":
                return self._job_list_allowed(query)
            return first in {"sources","destinations","connections"} and self._workspace_list_allowed(query)
        if method!="POST" or query:
            return False
        if first=="jobs":
            return body.get("connectionId") in self.owned["connections"] and body.get("jobType")=="sync"
        if first=="connections":
            return (body.get("sourceId") in self.owned["sources"]
                    and body.get("destinationId") in self.owned["destinations"] and self._manual_schedule(body))
        if body.get("workspaceId")!=self.workspace or body.get("definitionId") not in self.definitions[first]:
            return False
        cfg=body.get("configuration") or {}
        if first=="destinations":
            return (cfg.get("host")==self.sfhost and str(cfg.get("database","")).casefold()==self.sfdb
                    and not cfg.get("jdbc_url_params"))
        return self._source_configuration_valid(cfg)

    def _workspace_list_allowed(self, query):
        scope = {"workspaceId", "workspaceIds"} & query.keys()
        return (len(scope) == 1 and not set(query) - (scope | {"limit", "offset"})
                and all(len(values) == 1 for values in query.values())
                and query[next(iter(scope))] == [self.workspace]
                and re.fullmatch(r"[1-9][0-9]{0,5}", query.get("limit", ["100"])[0]) is not None
                and re.fullmatch(r"0|[1-9][0-9]{0,5}", query.get("offset", ["0"])[0]) is not None)

    def _job_list_allowed(self, query):
        if not set(query) <= {"connectionId", "limit", "orderBy", "jobType"}:
            return False
        if any(len(values) != 1 for values in query.values()):
            return False
        connection = query.get("connectionId", [None])[0]
        limit = query.get("limit", ["100"])[0]
        return (
            (connection is None or connection in self.owned["connections"])
            and re.fullmatch(r"[1-9][0-9]{0,5}", limit) is not None
            and query.get("orderBy", ["createdAt|DESC"])[0] == "createdAt|DESC"
            and query.get("jobType", ["sync"])[0] == "sync"
        )

    def _list_jobs(self, query, headers):
        connection = query.get("connectionId", [None])[0]
        limit = min(int(query.get("limit", ["100"])[0]), 100)
        with self.lock:
            jobs = sorted(self.job_connections.items(), key=lambda item: int(item[0]), reverse=True)
        result = []
        for job_id, owner in jobs:
            if connection is not None and owner != connection:
                continue
            response = self._send("GET", PREFIX + f"jobs/{job_id}", headers, b"", None)
            if response.status != 200:
                return response
            payload = json.loads(response.body)
            job = payload.get("data", payload.get("job", payload))
            if not isinstance(job, dict) or job.get("connectionId", owner) != owner:
                raise ValueError("Airbyte returned a job outside its recorded connection")
            result.append({**job, "connectionId": owner, "jobId": int(job_id)})
            if len(result) == limit:
                break
        return ForwardResult(200, "OK", (("Content-Type", "application/json"),),
                             json.dumps({"data": result}).encode())

    def forward_unauthenticated(self,method,path,headers,body):
        if method=="GET" and path==PREFIX+"health":
            return ForwardResult(200,"OK",(("Content-Type","application/json"),),b'{"available":true}')
        return denied()

    def forward(self,method,path,headers,body):
        location=urlsplit(path)
        if location.netloc or not location.path.startswith(PREFIX):
            return denied()
        parts=location.path.removeprefix(PREFIX).strip("/").split("/")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+",p) for p in parts):
            return denied()
        try:
            query=parse_qs(location.query, keep_blank_values=True, max_num_fields=12)
            data=json.loads(body) if body else {}
        except ValueError:
            return denied()
        with self.lock:
            allowed=isinstance(data,dict) and self._allowed(method,parts,query,data)
        if not allowed:
            with self.lock:
                key=f"{method} {parts[0]} 403"
                self.requests[key]=self.requests.get(key,0)+1
                if (method=="POST" and parts==["connections"] and isinstance(data,dict)
                    and any(not UUID.fullmatch(str(data.get(field,""))) for field in ("sourceId","destinationId"))):
                    self.submission_rejections[key]=self.submission_rejections.get(key,0)+1
            return denied()
        outbound={k:v for k,v in headers.items() if k.lower() in {"content-type","accept"}}
        outbound["Authorization"]=self.shared
        if method == "GET" and parts == ["jobs"]:
            try:
                result = self._list_jobs(query, outbound)
            except (ValueError, AttributeError):
                result = ForwardResult(502, "Bad Gateway", (("Content-Type", "application/json"),),
                                       b'{"message":"Airbyte returned invalid job data"}')
        else:
            if method == "GET" and len(parts) == 1 and parts[0] in {"sources", "destinations", "connections"}:
                scoped_query = {"workspaceIds": self.workspace}
                scoped_query.update({key: query[key][0] for key in ("limit", "offset") if key in query})
                path = location.path + "?" + urlencode(scoped_query)
            result=self._send(method,path,outbound,body,None)
        with self.lock:
            audit_key=f"{method} {parts[0]} {result.status}"
            self.requests[audit_key]=self.requests.get(audit_key,0)+1
        if method=="POST" and result.status in (200,201,202) and len(parts)==1:
            first=parts[0]
            key={"sources":"sourceId","destinations":"destinationId","connections":"connectionId","jobs":"jobId"}[first]
            try:
                response=json.loads(result.body)
                inner=response.get("data",response.get("job",response))
                obj=inner.get(key,inner.get("id"))
                if obj and (first=="jobs" and str(obj).isdigit() or UUID.fullmatch(str(obj))):
                    with self.lock:
                        self.owned[first].add(str(obj))
                        if first == "sources":
                            self.source_definitions[str(obj)] = data["definitionId"]
                        if first == "destinations":
                            self.destination_configs[str(obj)] = data["configuration"]
                        if first == "jobs":
                            self.job_connections[str(obj)] = data["connectionId"]
            except (ValueError,AttributeError):
                pass
        return result


def start_desktop_relay(name, agent, gateway_port, sf_host, runenv):
    from desktop_relay import CONTROL_PORT
    script = Path(__file__).with_name("desktop_relay.py")
    subprocess.run([
        "docker", "run", "--rm", "--detach", "--pull=never", "--name", name,
        "--network", "bridge", "--publish", f"127.0.0.1::{CONTROL_PORT}",
        "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL", "--read-only",
        "--security-opt=no-new-privileges", "--pids-limit=96", "--memory=128m",
        "--mount", f"type=bind,src={script},dst=/relay.py,readonly",
        "--entrypoint", "python", runenv["ELT_EVAL_SWE_IMAGE"],
        "/relay.py", agent, str(gateway_port), sf_host,
    ], check=True, capture_output=True, timeout=30)
    address = subprocess.check_output(["docker", "port", name, f"{CONTROL_PORT}/tcp"],
                                      text=True, timeout=20).strip()
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", address)
    if not match:
        raise RuntimeError("Docker Desktop did not bind SWE-ReX control to loopback")
    return int(match[1])


def connect_egress(container, network):
    subprocess.run(["docker", "network", "connect", "--alias", "elt-egress", network, container],
                   check=True, capture_output=True, timeout=30)


def write_state(out, state, **extra):
    write(out/"phase.json",{"status":state,**extra})
    print(state,flush=True)


def install_attempt_inputs(public, inputs, cfg, pem, scoped, workspace, server_port, credential, sfhost):
    shutil.copytree(public, inputs)
    keyfile = inputs / "snowflake_key.p8"
    keyfile.write_bytes(pem)
    keyfile.chmod(0o600)
    ab = cfg["Airbyte"]["config"]
    ab.update(username=scoped.username, password=scoped.password, workspace_id=workspace,
              server_url=f"http://host.docker.internal:{server_port}/api/public/v1/")
    destination = cfg["snowflake"]["config"]
    destination.pop("password", None)
    destination.update(account=credential["account"], username=credential["user"],
                       role=credential["role"], warehouse=credential["warehouse"], host=sfhost,
                       private_key_file="/workspace/snowflake_key.p8")
    (inputs / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    write(inputs / "snowflake_credential.json", {
        **credential, "authenticator": "SNOWFLAKE_JWT", "private_key_file": "/workspace/snowflake_key.p8",
    })
    shutil.copyfile(Path(__file__).with_name("sync_jobs.py"), inputs / "sync_jobs.py")
    with (inputs / "documentation" / "trigger_job.md").open("a") as guide:
        guide.write(
            "\n\n## Trigger and monitor syncs in this workspace\n\n"
            "After `terraform apply`, run `python /workspace/sync_jobs.py trigger`. "
            "Run `python /workspace/sync_jobs.py status` to check the jobs. "
            "The helper reads your connection IDs from Terraform state and uses the real password "
            "directly from config.yaml. Tool output masks the password as `[scoped credential]`. "
            "Use the helper for syncs and status instead of copying the display placeholder. "
            "After repairing a failed sync, append `--retry-failed` to the trigger command.\n"
        )
    with (inputs / "documentation" / "destination_snowflake.md").open("a") as guide:
        guide.write(
            '\n\nUse the scoped key from config.yaml. In Terraform, set '
            '`configuration.credentials.key_pair_authentication.private_key = file("/workspace/snowflake_key.p8")`. '
            'In your submitted dbt profile, set `private_key_path: /workspace/snowflake_key.p8` '
            'and use the account, user, role, and warehouse from config.yaml.\n'
        )
    if cfg.get("flat_files"):
        with (inputs / "documentation" / "source_file.md").open("a") as guide:
            guide.write(
                "\n\nUse the workspace's `Airbyte.config.files_definition_id` value from config.yaml "
                "for every flat-file source. It trusts the fixture's TLS certificate.\n"
            )


def replay_saved_submission(archived, workspace, inputs, out, runenv, scoped, pem):
    original = read(archived / "result.json")
    if original["status"] != "infrastructure_error":
        raise ValueError("Saved replay requires an attempt interrupted during grading")
    expected_connections = original["airbyte_request_counts"]["POST connections 200"]
    shutil.copytree(inputs, workspace)
    shutil.copytree(archived / "submission", workspace / "elt", dirs_exist_ok=True)
    elt = workspace / "elt"
    runner = DockerRunner(workspace, runenv["TERRAFORM_RUNNER_IMAGE"], lane="cloud-egress", timeout=240,
                          host_runner=SanitizedCommandRunner([scoped.password, *pem.decode().splitlines()[1:-1]]))
    runner.run(("terraform", "init", "-input=false"), cwd=elt)
    try:
        runner.run(("terraform", "apply", "-input=false", "-auto-approve", "-parallelism=1"), cwd=elt)
    except ProcessFailure:
        # A failed apply can still register the subset of connections recorded by the original attempt.
        if not find_terraform_state(elt):
            raise
    state = find_terraform_state(elt)
    if not state or len(terraform_state_connections(state)) != expected_connections:
        raise ValueError("Archived Terraform did not recreate the original number of connections")
    shutil.copyfile(archived / "spider_result.json", out / "spider_result.json")
    shutil.copyfile(archived / "agent_status.json", out / "agent_status.json")
    return {"agent": original["agent"], "saved_submission_replayed": str(archived),
            "replay_model_calls": 0, "original_registered_connections": expected_connections}


def run_one(root, out, selected, *, archived=None, agent="spider", smoke=False):
    agent_name, model, key_name, interpreter, worker_script, upstream_dir, result_file = EVALUATION_AGENTS[agent]
    roster=read(root/"readiness.json")["records"]
    record=next(r for r in roster.values() if r["task_id"]==selected)
    public=Path(record["public"])
    answer=Path(record["private"])/"answer_key"
    rendered=Path(record["frozen_populations"])/"development"/"rendered"
    db=record["database"]
    cfg=yaml.safe_load((public/"config.yaml").read_text())
    runenv=environment_variables()
    if archived is None and not smoke and not runenv.get(key_name):
        raise ValueError(f"{key_name} is missing")
    base=read(REPO/"secrets"/"airbyte-base.json")
    shared=read(REPO/"secrets"/"airbyte-gateway.json")
    host_api=runenv["AIRBYTE_HARNESS_URL"]
    direct=AirbyteClient(host_api,token_provider=access_token_provider(host_api,base["client_id"],base["client_secret"]),timeout=30)
    scoped=BasicCredential("airbyte",secrets.token_urlsafe(28))
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    with TemporaryDirectory(prefix="elt-spider-gpt4o-") as tmp:
        tmp=Path(tmp)
        env_dir=tmp/"sources"
        env_dir.mkdir()
        project=_safe_project(selected,"development",str(tmp))
        network=project+"-source-network"
        env=SourceEnvironment(root,selected,"development",env_dir,rendered,answer/"sources_serving.json",
                              env_dir/"compose.yaml",project,network)
        source_server=Path(sys.modules[load_routes.__module__].__file__)
        env.compose_path.write_text(yaml.safe_dump(_compose_document(
            rendered_root=rendered,manifest_path=env.manifest_path,server_script=source_server,network=network)))
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        public_key=base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        admin=None
        workspace=None
        provisioned=False
        started=False
        server=None
        container="spider-gpt4o-"+secrets.token_hex(8)
        summary={"task_id":selected,"model":model,"agent":agent_name,"solver_population":"development"}
        try:
            write_state(out,"provisioning_snowflake",task_id=selected)
            admin=snowflake.connector.connect(connection_name=PROFILE,
                connections_file_path=Path.home()/".snowflake"/"connections.toml",login_timeout=15,network_timeout=30)
            with admin.cursor() as cur:
                if any(_existing_owned(cur,db).values()):
                    raise ValueError("The attempt namespace already exists")
            credential=provision_attempt(admin,db,account=str(admin.account),public_key=public_key)
            provisioned=True
            sfhost=str(admin.host)
            write_state(out,"preparing_airbyte",task_id=selected)
            workspace=direct.create_workspace("ELT GPT4o Spider "+db)
            _,definitions=prepare_airbyte(public,answer,selected,direct,workspace,cfg)
            fixture_hosts={"elt-postgres","elt-mongodb","elt-localstack","elt-api","elt-files"}
            manifest=read(env.manifest_path)
            fixture_hosts.update(x["bucket"] for x in manifest["tables"].values() if x.get("bucket"))
            bridge=GatewayBridge(scoped,shared,workspace,definitions,sfhost,db,fixture_hosts)
            server=ThreadingHTTPServer(("0.0.0.0",0),make_handler(bridge))
            threading.Thread(target=server.serve_forever,daemon=True).start()
            write_state(out,"seeding_development_sources",task_id=selected)
            env.start()
            started=True
            env.seed()
            inputs=tmp/"input"
            install_attempt_inputs(public, inputs, cfg, pem, scoped, workspace,
                                   server.server_port, credential, sfhost)
            if archived:
                write_state(out, "replaying_saved_terraform", task_id=selected)
                summary.update(replay_saved_submission(archived, tmp/"workspace", inputs, out, runenv, scoped, pem))
            else:
                spec={"task_id":selected,"input_dir":str(inputs),"airbyte_password":scoped.password,
                      "container":container,"network":network,"workspace":str(tmp/"workspace"),"cache":str(tmp/"cache"),
                      "upstream":str(BENCH/"agents"/upstream_dir),"status":str(out/"agent_status.json"),
                      "result":str(out/result_file),"budget_usd":6.0,"smoke":smoke}
                write(tmp/"spec.json",spec)
                docker_host=subprocess.check_output(["docker","context","inspect","--format","{{.Endpoints.docker.Host}}"],text=True).strip()
                write_state(out,"swe_agent_running" if agent == "swe" else "spider_agent_running",task_id=selected)
                worker=subprocess.run([str(interpreter),str(Path(__file__).with_name(worker_script)),str(tmp/"spec.json")],
                                      env={**os.environ,key_name:runenv.get(key_name,""),"DOCKER_HOST":docker_host},
                                      capture_output=True,text=True,timeout=3600)
                summary["worker_exit_code"]=worker.returncode
                if (out/"agent_status.json").is_file():
                    summary["agent"]=read(out/"agent_status.json")
                if worker.returncode or not (out/result_file).is_file():
                    stderr=worker.stderr
                    for line in [scoped.password,*pem.decode().splitlines()[1:-1],runenv.get(key_name,"")]:
                        if line:
                            stderr=stderr.replace(line,"[redacted]")
                    summary["worker_error"]=stderr[-2600:]
                    raise RuntimeError(f"{agent_name} worker exited before submission")
            summary["airbyte_request_counts"]=bridge.requests.copy()
            summary["airbyte_submission_rejections"]=bridge.submission_rejections.copy()
            write_state(out,"grading",task_id=selected)
            from grade import grade_and_capture
            summary.update(grade_and_capture(tmp/"workspace",out,public,answer,record,credential,key,
                                             direct,env,workspace))
            summary["status"]="completed"
        except Exception as exc:
            summary.update(status="infrastructure_error",error_type=type(exc).__name__,
                           error=str(exc).replace(scoped.password,"[redacted]")[:350])
        finally:
            removed={}
            if server:
                server.shutdown()
                server.server_close()
            for item,enabled,action in (
                ("container",True,lambda:subprocess.run(["docker","rm","-f",container],capture_output=True)),
                ("airbyte_workspace",workspace is not None,lambda:direct.delete_workspace(workspace)),
                ("sources",started,env.stop),
                ("snowflake",provisioned,lambda:cleanup_snowflake(admin,db))):
                if enabled:
                    try: action(); removed[item]="removed"
                    except Exception as exc: removed[item]=type(exc).__name__
            if admin:
                admin.close()
            summary["cleanup"]=removed
            write(out/"result.json",summary)
            write_state(out,summary["status"],task_id=selected)
    return summary


if __name__=="__main__":
    logging.disable(logging.CRITICAL)
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--replay-submission", type=Path)
    parser.add_argument("--agent", choices=tuple(EVALUATION_AGENTS), default="spider")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.smoke and args.agent != "swe":
        parser.error("--smoke is supported for SWE-agent")
    root = args.root.resolve()
    selected = args.task_id or read(root/"source_canary"/"report.json")["task_id"]
    with (root/"spider-runtime.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result = run_one(root,args.out.resolve(),selected,archived=args.replay_submission,
                         agent=args.agent,smoke=args.smoke)
    raise SystemExit(0 if result["status"] == "completed" else 1)

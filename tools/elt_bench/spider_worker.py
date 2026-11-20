"""Invoke the original Spider-Agent loop for one prepared public workspace."""
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

logging.disable(logging.CRITICAL)
spec = json.loads(Path(sys.argv[1]).read_text())
sys.path.insert(0, spec["upstream"])
from spider_agent.agent import agents
from spider_agent.envs.spider_agent import Spider_Agent_Env

class BudgetReached(Exception):
    pass

provider_call = agents.call_llm
turns = 0
cost = 0.0

def write_status(status):
    p = Path(spec["status"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"task_id": spec["task_id"], "status": status,
                              "model_calls": turns, "model_cost_usd": round(cost, 6)}) + "\n")
    tmp.replace(p)

pem_lines = (Path(spec["input_dir"]) / "snowflake_key.p8").read_text().splitlines()[1:-1]
secret_values = [*pem_lines, spec["airbyte_password"]]

def redact(text):
    text = re.sub(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
                  "[scoped private key]", text, flags=re.S)
    for value in secret_values:
        text = text.replace(value, "[scoped credential]")
    return text

def sanitize(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k:sanitize(v) for k,v in value.items()}
    return value

def call(body):
    global turns, cost
    if cost >= spec["budget_usd"]:
        raise BudgetReached
    turns += 1
    write_status("requesting_model")
    ok, text, billed = provider_call(sanitize(body))
    cost += billed
    write_status("agent_running" if ok else "provider_call_failed")
    return ok, text, billed

agents.call_llm = call
instruction = ("You are required to build an ELT pipeline by extracting data from multiple sources, "
    "such as custom APIs, Postgres, MongoDB, flat files and cloud service S3. The extracted data will "
    "be loaded into a target system, Snowflake, followed by writing transformation queries to construct "
    "final tables for downstream use. "
    "Runtime authentication for this attempt is described in config.yaml. For Snowflake, use the RSA key file "
    "path from snowflake.config.private_key_file. Use file(path) under the Terraform destination "
    "configuration.credentials.key_pair_authentication.private_key, and private_key_path in the dbt profile. "
    "For the Airbyte Terraform provider, read username and password directly using "
    "yamldecode(file(\"../config.yaml\")).Airbyte.config in the elt directory. "
    "Tool output redacts scoped credentials. Reference the config fields in code.")
agent = agents.PromptAgent(model="gpt-4o",max_tokens=2500,top_p=0.9,temperature=0,
                           max_memory_length=25,max_steps=100,use_plan=False)
env = None
try:
    env = Spider_Agent_Env(
        env_config={"init_args":{"name":spec["container"],"work_dir":"/workspace",
                                 "network":spec["network"]},"image_name":"elt_agent-image"},
        task_config={"instance_id":spec["task_id"],"instruction":instruction,
                     "config":[{"type":"copy_all_subfiles","parameters":{"dirs":[spec["input_dir"]]}}]},
        cache_dir=spec["cache"],mnt_dir=spec["workspace"])
    agent.set_env_and_task(env)
    write_status("agent_running")
    try:
        done, result = agent.run()
        status = "finished"
    except BudgetReached:
        done, result, status = False, "Model cost limit reached", "budget_limit"
    record = sanitize({"finished":done,"result":result,"status":status,"model_calls":turns,
                       "model_cost_usd":round(cost,6),**agent.get_trajectory()})
    Path(spec["result"]).write_text(json.dumps(record,indent=2,default=str)+"\n")
    write_status(status)
finally:
    subprocess.run(["docker","rm","-f",spec["container"]],capture_output=True,check=False)

"""Run a frozen ELT-Bench evaluation roster and write scores."""
from argparse import ArgumentParser
import csv
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import environment_variables, read, write
from attempt import EVALUATION_AGENTS, run_one

HIDDEN=("primary","resampled","counterfactual","stress")


def summarize(output, plan, state, blocked=None):
    rows=[]
    for item in plan["tasks"]:
        path=output/item["output"]/"result.json"
        if not path.is_file():
            continue
        report=read(path)
        row={"task_id":item["task_id"],"status":report["status"],
             "model_cost_usd":report.get("agent",{}).get("model_cost_usd",0),
             "expected_marts":item["marts"]}
        if report["status"]=="completed":
            for pop in HIDDEN:
                record=report.get("hidden",{}).get(pop,{})
                if record:
                    row[pop+"_el"]=int(record.get("stage1_passed",False))
                    row[pop+"_marts_correct"]=record.get("marts_passed",0)
                elif report.get("hidden_status")=="no_submitted_terraform_connections":
                    row[pop+"_el"]=0
                    row[pop+"_marts_correct"]=0
        rows.append(row)
    complete=[r for r in rows if r["status"]=="completed"]
    value={"status":state,"planned_tasks":len(plan["tasks"]),"attempted_tasks":len(rows),
           "completed_tasks":len(complete),"primary_el_pass":sum(r.get("primary_el",0) for r in complete),
           "primary_marts_correct":sum(r.get("primary_marts_correct",0) for r in complete),
           "completed_task_marts":sum(r["expected_marts"] for r in complete),
           "reported_model_cost_usd":round(sum(r["model_cost_usd"] for r in rows),6)}
    value.update(model=plan["model"], agent=plan["agent"])
    if blocked: value["blocking_reason"]=blocked
    write(output/"batch_status.json",value)
    columns=["task_id","status","model_cost_usd","expected_marts",*[f"{p}_{end}" for p in HIDDEN for end in ("el","marts_correct")]]
    with (output/"scores.csv").open("w",newline="") as stream:
        w=csv.DictWriter(stream,fieldnames=columns);w.writeheader();w.writerows(rows)


def unresolved_gateway_errors(out, report):
    counts=report.get("airbyte_request_counts",{})
    known=report.get("airbyte_submission_rejections",{}).copy()
    review_path=out/"gateway_review.json"
    if review_path.is_file():
        review=read(review_path)
        digest=hashlib.sha256((out/"result.json").read_bytes()).hexdigest()
        terraform=hashlib.sha256((out/"submission"/"main.tf").read_bytes()).hexdigest()
        if review.get("result_sha256")!=digest or review.get("submitted_terraform_sha256")!=terraform:
            raise ValueError("Archived gateway review does not bind to this attempt")
        for request,n in review["confirmed_submission_rejections"].items():
            known[request]=known.get(request,0)+n
    return sum(max(0,n-known.get(request,0)) for request,n in counts.items()
               if request.endswith((" 401"," 403"," 502")))


def opus_api_preflight():
    key = environment_variables().get("ANTHROPIC_API_KEY")
    if not key:
        return "ANTHROPIC_API_KEY is missing in ELT-taskgen/.env"
    request = Request(
        "https://api.anthropic.com/v1/models/claude-opus-4-7",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            if json.load(response).get("id") != "claude-opus-4-7":
                return "Anthropic returned an unexpected model ID"
    except HTTPError as exc:
        return f"Anthropic model lookup returned HTTP {exc.code} for ANTHROPIC_API_KEY"
    except (URLError, TimeoutError, OSError, ValueError):
        return "Anthropic model lookup was unavailable"
    return None


def main(root,canary,agent="spider",run_first=False):
    output=root/"agent_runs"
    first=read(root/"source_canary/report.json")["task_id"]
    roster=read(root/"readiness.json")["records"]
    lookup={r["task_id"]:r for r in roster.values()}
    assert len(lookup)==50
    tasks=[first,*sorted(set(lookup)-{first})]
    agent_name, model, *_ = EVALUATION_AGENTS[agent]
    plan={"model":model,"agent":agent_name,
          "model_budget_stop_usd_per_task":6,"solver_population":"development",
          "hidden_populations":HIDDEN,
          "tasks":[{"task_id":t,"output":canary if i==0 else f"task-{i+1:02d}",
                    "marts":lookup[t]["marts"]} for i,t in enumerate(tasks)]}
    if agent == "spider":
        plan["turns_per_task"] = 100
    write(output/"batch_plan.json",plan)
    if agent == "swe":
        blocked = opus_api_preflight()
        if blocked:
            summarize(output, plan, "paused", blocked)
            print(blocked, flush=True)
            return
    summarize(output,plan,"running" if run_first else "waiting_for_live_canary")
    until=time.monotonic()+7200
    for item in plan["tasks"]:
        out=output/item["output"]
        if item["output"]==canary and not run_first:
            while not (out/"result.json").is_file():
                if time.monotonic()>until:
                    summarize(output,plan,"paused","Canary result missing")
                    return
                time.sleep(10)
            report=read(out/"result.json")
        else:
            summarize(output,plan,"running")
            if (out/"result.json").is_file():
                report=read(out/"result.json")
            else:
                with (root/"spider-runtime.lock").open("a") as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    report=run_one(root,out,item["task_id"],agent=agent)
        if report["status"]!="completed":
            summarize(output,plan,"paused",f"Infrastructure error in {item['output']}")
            return
        blocked_requests=unresolved_gateway_errors(out,report)
        if blocked_requests:
            summarize(output,plan,"paused",f"Review {blocked_requests} gateway errors in {item['output']}")
            return
        summarize(output,plan,"running")
    summarize(output,plan,"completed")


if __name__=="__main__":
    parser=ArgumentParser()
    parser.add_argument("root",type=Path)
    parser.add_argument("--canary",default="canary-02")
    parser.add_argument("--agent", choices=tuple(EVALUATION_AGENTS), default="spider")
    parser.add_argument("--run-first", action="store_true")
    args=parser.parse_args()
    main(args.root.resolve(),args.canary,args.agent,args.run_first)

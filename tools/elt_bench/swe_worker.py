"""Run ELT-Bench SWE-agent with scoped development inputs and Claude Opus 4.7."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys

import yaml


def load_runner(path):
    spec = importlib.util.spec_from_file_location("upstream_elt_swe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_redactor(inputs):
    cfg = yaml.safe_load((inputs / "config.yaml").read_text())["Airbyte"]["config"]
    pair = f"{cfg['username']}:{cfg['password']}"
    pem_lines = (inputs / "snowflake_key.p8").read_text().splitlines()[1:-1]
    sensitive = [
        cfg["password"], base64.b64encode(pair.encode()).decode(),
        *pem_lines,
    ]

    def sanitize(value):
        if isinstance(value, str):
            for secret in sensitive:
                if secret:
                    value = value.replace(secret, "[scoped credential]")
            return value
        if isinstance(value, list):
            return [sanitize(child) for child in value]
        if isinstance(value, dict):
            return {key: sanitize(child) for key, child in value.items()}
        return value

    return sanitize


def write_status(path, status, calls=0, cost=0.0):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"status": status, "model_calls": calls, "model_cost_usd": cost}) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def run(spec):
    upstream_root = Path(spec["upstream"]).resolve()
    sys.path.insert(0, str(upstream_root))
    upstream = load_runner(upstream_root / "run_elt.py")
    import litellm
    from sweagent.run.run_single import RunSingle, RunSingleConfig

    inputs = Path(spec["input_dir"])
    work = Path(spec["workspace"])
    attempt = work.parent
    upstream.copy_task_input(inputs, work)
    config_file = attempt / "swe.yaml"
    upstream.write_destination_config("snowflake", config_file)
    config = yaml.safe_load(config_file.read_text())
    smoke = spec.get("smoke", False)
    model = "instant_empty_submit" if smoke else "claude-opus-4-7"
    config["agent"]["model"] = {
        "name": model, "temperature": 1.0, "per_instance_cost_limit": spec["budget_usd"],
    }
    config["env"] = {
        "repo": {"type": "preexisting", "repo_name": "workspace"},
        "deployment": {
            "type": "docker", "image": "elt-swe:latest",
            "docker_args": ["--network", spec["network"], "-v", f"{work}:/workspace"],
        },
    }
    config["problem_statement"] = {"type": "empty", "id": spec["task_id"]}
    config["output_dir"] = str(attempt / "swe-trajectory")
    empty_env = attempt / "swe-empty.env"
    empty_env.touch()
    config["env_var_path"] = str(empty_env)

    sanitize = make_redactor(inputs)
    status = Path(spec["status"])
    real_completion = litellm.completion
    litellm.drop_params = True
    successful_calls = 0

    def completion(*args, **kwargs):
        nonlocal successful_calls
        kwargs["messages"] = sanitize(kwargs["messages"])
        write_status(status, "requesting_model", successful_calls)
        result = real_completion(*args, **kwargs)
        successful_calls += 1
        write_status(status, "agent_running", successful_calls)
        return result

    litellm.completion = completion
    os.chdir(upstream_root)
    runner = RunSingle.from_config(RunSingleConfig.model_validate(config))
    runner.env.deployment._get_container_name = lambda: spec["container"]
    try:
        write_status(status, "starting_environment")
        runner.run()
        file = Path(config["output_dir"]) / spec["task_id"] / (spec["task_id"] + ".traj")
        trajectory = json.loads(file.read_text())
        info = trajectory.get("info", {})
        stats = info.get("model_stats", {})
        calls = stats.get("api_calls", 0)
        cost = round(stats.get("instance_cost", 0.0), 6)
        exit_status = str(info.get("exit_status", ""))
        if not exit_status or "exit_error" in exit_status or (not smoke and not calls):
            raise RuntimeError(f"SWE-agent finished with {exit_status or 'no status'} and {calls} model calls")
        result = Path(spec["result"])
        result.write_text(json.dumps(sanitize(trajectory), indent=2, default=str) + "\n")
        result.chmod(0o600)
        write_status(status, exit_status, calls if not smoke else 0, cost)
    finally:
        runner.env.close()


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))

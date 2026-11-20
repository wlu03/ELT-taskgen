"""Trigger and inspect Airbyte syncs using the task's installed config."""
from argparse import ArgumentParser
import base64
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

FAILED = {"failed", "cancelled", "canceled"}


def connection_ids(state):
    return sorted({
        instance["attributes"]["connection_id"]
        for resource in state["resources"] if resource["type"] == "airbyte_connection"
        for instance in resource["instances"]
        if instance.get("attributes", {}).get("connection_id")
    })


def request(config, method, path, payload=None):
    pair = f"{config['username']}:{config['password']}".encode()
    req = Request(
        config["server_url"].rstrip("/") + "/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Basic " + base64.b64encode(pair).decode(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(req, timeout=45) as response:
            return json.load(response)
    except HTTPError as error:
        raise SystemExit(f"Airbyte {method} jobs returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise SystemExit(f"Airbyte {method} jobs could not be reached") from None


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("trigger", "status"))
    parser.add_argument("--state", type=Path, default=Path("/workspace/elt/terraform.tfstate"))
    parser.add_argument("--config", type=Path, default=Path("/workspace/config.yaml"))
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())["Airbyte"]["config"]
    wanted = connection_ids(json.loads(args.state.read_text()))
    if not wanted:
        raise SystemExit("Terraform state has no Airbyte connections")
    latest = {}
    for job in request(config, "GET", "jobs?limit=100&orderBy=createdAt%7CDESC")["data"]:
        latest.setdefault(job["connectionId"], job)
    for connection in wanted:
        prior = latest.get(connection, {})
        status = prior.get("status", "not_started")
        if args.command == "trigger" and (status == "not_started" or args.retry_failed and status in FAILED):
            job = request(config, "POST", "jobs", {"jobType": "sync", "connectionId": connection})
            job = job.get("data", job.get("job", job))
            print(f"{connection} job {job['jobId']} triggered")
        else:
            print(f"{connection} job {prior.get('jobId', '-')} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Monitor Airbyte jobs created by the task Terraform state."""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def connection_ids(state_path: Path) -> list[str]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return sorted(
        str(instance["attributes"]["connection_id"])
        for resource in state.get("resources", [])
        if resource.get("type") == "airbyte_connection"
        for instance in resource.get("instances", [])
    )


def airbyte_api_root(server: str) -> str:
    """Return a normalized Airbyte public-API root.

    A bare host retains the helper's original HTTP shorthand.  Full HTTP(S)
    URLs are used as written, so an installed ``.../api/public/v1/`` URL is
    never prefixed with a second scheme or API path.  An origin-only URL gets
    Airbyte OSS's conventional public-API path.
    """
    value = str(server).strip()
    if not value:
        raise ValueError("Airbyte server URL is required")
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Airbyte server must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Airbyte credentials must not be embedded in the URL")
    if parsed.query or parsed.fragment:
        raise ValueError("Airbyte server URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/api/public/v1"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path + "/", "", ""))


def _basic_headers(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")
    return {"Authorization": f"Basic {token}", "accept": "application/json"}


def _oauth_headers(api_root: str, client_id: str, client_secret: str) -> dict[str, str]:
    body = json.dumps(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant-type": "client_credentials",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        urljoin(api_root, "applications/token"),
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urlopen(request, timeout=30)
        with response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        # Airbyte error bodies can echo credentials; never include them here.
        raise RuntimeError("could not obtain an Airbyte access token") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Airbyte token response is not an object")
    token = payload.get("access_token") or payload.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Airbyte token response contains no access token")
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


def _job_payload(url: str, headers: dict[str, str]) -> dict:
    query = urlencode({"limit": 100, "orderBy": "createdAt|DESC"})
    request = Request(url + "?" + query, headers=headers, method="GET")
    try:
        response = urlopen(request, timeout=30)
        with response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        raise RuntimeError("could not query Airbyte jobs") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Airbyte jobs response is not an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--state", type=Path, default=Path("elt/terraform.tfstate"))
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args()
    basic_supplied = args.username is not None or args.password is not None
    oauth_supplied = args.client_id is not None or args.client_secret is not None
    if basic_supplied and oauth_supplied:
        parser.error("choose Basic or OAuth Airbyte authentication, not both")
    if basic_supplied and (not args.username or not args.password):
        parser.error("--username and --password must be supplied together")
    if oauth_supplied and (not args.client_id or not args.client_secret):
        parser.error("--client-id and --client-secret must be supplied together")
    if not basic_supplied and not oauth_supplied:
        parser.error(
            "supply --username/--password or --client-id/--client-secret"
        )
    wanted = set(connection_ids(args.state))
    if not wanted:
        raise SystemExit("no Airbyte connections found in Terraform state")
    try:
        api_root = airbyte_api_root(args.server)
    except ValueError as exc:
        parser.error(str(exc))
    url = urljoin(api_root, "jobs")
    deadline = time.monotonic() + args.timeout
    while True:
        if oauth_supplied:
            headers = _oauth_headers(api_root, args.client_id, args.client_secret)
        else:
            headers = _basic_headers(args.username, args.password)
        statuses = {}
        for job in _job_payload(url, headers).get("data", []):
            connection_id = str(job.get("connectionId"))
            if connection_id in wanted and connection_id not in statuses:
                statuses[connection_id] = str(job.get("status", "")).lower()
        failed = sorted(
            cid
            for cid, status in statuses.items()
            if status in {"failed", "cancelled", "canceled", "incomplete"}
        )
        if failed:
            print("failed:", ", ".join(failed))
            return 1
        if wanted and all(statuses.get(cid) == "succeeded" for cid in wanted):
            print("succeeded:", ", ".join(sorted(wanted)))
            return 0
        if time.monotonic() >= deadline:
            pending = sorted(cid for cid in wanted if statuses.get(cid) != "succeeded")
            print("timed out:", ", ".join(pending))
            return 1
        time.sleep(max(0.0, args.poll_interval))


if __name__ == "__main__":
    raise SystemExit(main())

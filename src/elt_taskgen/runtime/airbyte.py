"""Airbyte OSS client for benchmark runtime operations.

Publish REST definitions, check connectors, and run connections found in
Terraform state. Solver-created task sources, destinations, and connections
remain outside this client.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class AirbyteError(RuntimeError):
    """The Airbyte control plane could not satisfy a runtime operation."""


@dataclass(frozen=True)
class AirbyteSyncReceipt:
    """Exact jobs followed for one ordered set of connection syncs.

    Status-only callers retain the historical ``trigger_and_wait`` API.  A
    certification runner consumes this receipt instead, so the job ids
    returned by each trigger are not discarded after polling them.
    """

    job_ids: dict[str, int]
    statuses: dict[str, str]


_WORKSPACE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def _items(payload: Any) -> list[dict[str, Any]]:
    """Return the list carried by common Airbyte list-response envelopes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "data",
        "sourceDefinitions",
        "destinationDefinitions",
        "declarativeSourceDefinitions",
        "workspaces",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get("data")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _job_value(job: Mapping[str, Any], *keys: str) -> Any:
    """Read a job field across the flat and nested Airbyte response shapes."""

    candidates: list[Mapping[str, Any]] = [job]
    nested = job.get("job")
    if isinstance(nested, Mapping):
        candidates.append(nested)
    for candidate in candidates:
        for key in keys:
            if key in candidate:
                return candidate[key]
    return None


def _job_timestamp(value: Any) -> float | None:
    """Normalize Airbyte epoch or ISO-8601 timestamps for local ordering."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _job_id(job: Mapping[str, Any]) -> int | None:
    raw_id = _job_value(job, "jobId", "job_id", "id")
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _latest_job_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Select newest rows without trusting list order or partial metadata."""

    if len(rows) <= 1:
        return list(rows)
    created = [
        _job_timestamp(_job_value(row, "createdAt", "created_at", "created"))
        for row in rows
    ]
    ids = [_job_id(row) for row in rows]
    updated = [
        _job_timestamp(_job_value(row, "updatedAt", "updated_at", "updated"))
        for row in rows
    ]
    if all(value is not None for value in created):
        keys = [
            (float(created_value), id_value if id_value is not None else -1)
            for created_value, id_value in zip(created, ids)
        ]
    elif all(value is not None for value in ids):
        # Job ids are monotonically allocated by Airbyte and remain a safer
        # fallback than preferring the subset that happened to carry a date.
        keys = [(int(value),) for value in ids]
    elif all(value is not None for value in updated):
        keys = [(float(value),) for value in updated]
    else:
        raise AirbyteError(
            "Airbyte returned jobs without comparable recency metadata"
        )
    newest = max(keys)
    return [row for row, key in zip(rows, keys) if key == newest]


class AirbyteClient:
    """Basic-auth client for Airbyte's public ``/v1`` API.

    ``base_url`` may be either the API root itself or the upstream
    ``.../api/public/v1/`` value from ``config.yaml``.  An injectable opener
    keeps all tests local and lets a production harness supply its own HTTP
    transport if required.
    """

    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        bearer_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not str(base_url).strip():
            raise ValueError("Airbyte base URL is required")
        has_basic = bool(username) or bool(password)
        if has_basic and (not username or not password):
            raise ValueError("Airbyte username and password must be supplied together")
        auth_count = int(has_basic) + int(bool(bearer_token)) + int(token_provider is not None)
        if auth_count != 1:
            raise ValueError("supply exactly one Airbyte authentication method")
        self.base_url = str(base_url).rstrip("/") + "/"
        self._token_provider = token_provider
        if bearer_token:
            authorization = f"Bearer {bearer_token}"
        elif has_basic:
            token = base64.b64encode(
                f"{username}:{password}".encode("utf-8")
            ).decode("ascii")
            authorization = f"Basic {token}"
        else:
            authorization = ""
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if authorization:
            self._headers["Authorization"] = authorization
        self._opener = opener or urllib.request.urlopen
        self.timeout = float(timeout)

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        url = urllib.parse.urljoin(self.base_url, path.lstrip("/"))
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = dict(self._headers)
        if self._token_provider is not None:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise AirbyteError("Airbyte token provider returned no access token")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            with response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Do not include response bodies: an Airbyte error can echo source
            # or destination credentials.
            raise AirbyteError(
                f"Airbyte {method.upper()} {urllib.parse.urlparse(url).path} "
                f"failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AirbyteError(
                f"Airbyte {method.upper()} {urllib.parse.urlparse(url).path} "
                "could not be reached"
            ) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AirbyteError("Airbyte returned a non-JSON response") from exc

    def list_workspaces(self) -> list[dict[str, Any]]:
        return _items(self.request("GET", "workspaces"))

    def iter_workspaces(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """Every workspace on the instance, following the public API's paging."""
        if not isinstance(page_size, int) or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        workspaces: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = urllib.parse.urlencode({"limit": page_size, "offset": offset})
            page = _items(self.request("GET", f"workspaces?{query}"))
            workspaces.extend(page)
            if len(page) < page_size:
                return workspaces
            offset += page_size

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete one workspace and everything it owns (sources, connections)."""
        if not isinstance(workspace_id, str) or not _WORKSPACE_ID.match(workspace_id):
            raise ValueError("workspace_id must be a UUID")
        self.request("DELETE", f"workspaces/{workspace_id}")

    def create_workspace(self, name: str) -> str:
        result = self.request("POST", "workspaces", {"name": str(name)})
        candidates = [result]
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            candidates.append(result["data"])
        for candidate in candidates:
            if isinstance(candidate, dict):
                value = candidate.get("workspaceId") or candidate.get("id")
                if isinstance(value, str) and value:
                    return value
        raise AirbyteError("Airbyte created a workspace but returned no workspace id")

    def list_source_definitions(self, workspace_id: str) -> list[dict[str, Any]]:
        return _items(
            self.request(
                "GET", f"workspaces/{workspace_id}/definitions/sources"
            )
        )

    def list_destination_definitions(
        self, workspace_id: str
    ) -> list[dict[str, Any]]:
        return _items(
            self.request(
                "GET", f"workspaces/{workspace_id}/definitions/destinations"
            )
        )

    def list_connections(self, workspace_id: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"workspaceIds": str(workspace_id), "limit": 100, "offset": 0}
        )
        return _items(self.request("GET", f"connections?{query}"))

    def publish_declarative_source_definition(
        self,
        workspace_id: str,
        *,
        name: str,
        manifest: Mapping[str, Any],
    ) -> str:
        """Publish one low-code connector and return its definition UUID.

        This is the API form of the manual Builder -> Import YAML -> Publish
        step in upstream ELT-Bench.  Response key names have changed between
        Airbyte API generations, so the parser accepts the documented/common
        variants but still fails closed if no definition id is present.
        """
        result = self.request(
            "POST",
            f"workspaces/{workspace_id}/definitions/declarative_sources",
            {"name": name, "manifest": dict(manifest)},
        )
        if not isinstance(result, dict):
            raise AirbyteError(
                "Airbyte declarative-source response is not an object"
            )
        candidates: list[Mapping[str, Any]] = [result]
        for key in ("data", "declarativeSourceDefinition"):
            value = result.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        for candidate in candidates:
            for key in (
                "id",
                "definitionId",
                "sourceDefinitionId",
                "declarativeSourceDefinitionId",
            ):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    return value
        raise AirbyteError(
            "Airbyte created the declarative source but returned no definition id"
        )

    def trigger_sync(self, connection_id: str) -> Any:
        return self.request(
            "POST",
            "jobs",
            {"jobType": "sync", "connectionId": str(connection_id)},
        )

    def job_status(self, job_id: int) -> str:
        payload = self.request("GET", f"jobs/{int(job_id)}")
        candidates = [payload]
        if isinstance(payload, dict):
            for key in ("data", "job"):
                value = payload.get(key)
                if isinstance(value, dict):
                    candidates.append(value)
        for candidate in candidates:
            if isinstance(candidate, dict):
                value = candidate.get("status")
                if isinstance(value, str) and value:
                    return value.lower()
        raise AirbyteError(f"Airbyte job {job_id} returned no status")

    def connection_job_statuses(
        self, connection_ids: Sequence[str]
    ) -> dict[str, str]:
        wanted = {str(value) for value in connection_ids}
        query = urllib.parse.urlencode(
            {"limit": 100, "orderBy": "createdAt|DESC"}
        )
        payload = self.request("GET", f"jobs?{query}")
        jobs = _items(payload)
        statuses: dict[str, str] = {}
        candidates: dict[str, list[dict[str, Any]]] = {
            connection_id: [] for connection_id in wanted
        }
        for job in jobs:
            connection_id = str(
                _job_value(job, "connectionId", "connection_id") or ""
            )
            if connection_id in wanted:
                candidates[connection_id].append(job)
        # Ignore response order. Select newest rows by timestamp, then job ID;
        # conflicting statuses at equal metadata are ambiguous.
        for connection_id, rows in candidates.items():
            if not rows:
                continue
            latest = _latest_job_rows(rows)
            latest_statuses = {
                str(_job_value(job, "status") or "").lower() for job in latest
            }
            if len(latest_statuses) != 1:
                raise AirbyteError(
                    "Airbyte returned ambiguous latest job states for a connection"
                )
            statuses[connection_id] = latest_statuses.pop()
        return statuses

    def trigger_and_wait_receipt(
        self,
        connection_ids: Sequence[str],
        *,
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> AirbyteSyncReceipt:
        ids = tuple(dict.fromkeys(str(value) for value in connection_ids))
        if not ids:
            raise AirbyteError("Terraform state contains no Airbyte connections")
        # Run connections serially because connector preflight checks reuse
        # temporary objects in the shared destination schema.
        statuses: dict[str, str] = {}
        job_ids: dict[str, int] = {}
        deadline = monotonic() + float(timeout)
        terminal_failures = {"failed", "cancelled", "canceled", "incomplete"}
        for connection_id in ids:
            result = self.trigger_sync(connection_id)
            candidates = [result]
            if isinstance(result, dict):
                for key in ("data", "job"):
                    value = result.get(key)
                    if isinstance(value, dict):
                        candidates.append(value)
            job_id: int | None = None
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                raw = (
                    candidate["jobId"]
                    if "jobId" in candidate
                    else candidate.get("id")
                )
                # Accept only positive integer IDs; decimal strings support APIs
                # that stringify identifiers.
                if isinstance(raw, bool):
                    continue
                if isinstance(raw, int):
                    parsed = raw
                elif isinstance(raw, str) and raw.isdecimal():
                    parsed = int(raw)
                else:
                    continue
                if parsed <= 0:
                    continue
                job_id = parsed
                break
            if job_id is None:
                raise AirbyteError(
                    f"Airbyte sync for connection {connection_id} returned no job id "
                    "(a positive integer is required)"
                )
            if job_id in job_ids.values():
                raise AirbyteError(
                    "Airbyte reused one job id for distinct connections: "
                    f"{job_id}"
                )
            job_ids[connection_id] = job_id
            while True:
                status = self.job_status(job_id)
                statuses[connection_id] = status
                if status in terminal_failures:
                    raise AirbyteError(
                        f"Airbyte sync failed: {connection_id}={status}"
                    )
                if status == "succeeded":
                    break
                if monotonic() >= deadline:
                    pending = ids[ids.index(connection_id) :]
                    raise AirbyteError(
                        "timed out waiting for Airbyte connections: "
                        + ", ".join(sorted(pending))
                    )
                sleep(max(0.0, float(poll_interval)))
        return AirbyteSyncReceipt(
            job_ids={connection_id: job_ids[connection_id] for connection_id in ids},
            statuses={connection_id: statuses[connection_id] for connection_id in ids},
        )

    def trigger_and_wait(
        self,
        connection_ids: Sequence[str],
        *,
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, str]:
        """Compatibility view of :meth:`trigger_and_wait_receipt` statuses."""

        return self.trigger_and_wait_receipt(
            connection_ids,
            poll_interval=poll_interval,
            timeout=timeout,
            sleep=sleep,
            monotonic=monotonic,
        ).statuses


def definition_ids(definitions: Sequence[Mapping[str, Any]]) -> set[str]:
    """Extract connector definition IDs from API list results."""
    found: set[str] = set()
    for item in definitions:
        for key in (
            "id",
            "definitionId",
            "sourceDefinitionId",
            "destinationDefinitionId",
        ):
            value = item.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return found


def definition_versions(
    definitions: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Return definition-id -> image tag across current and legacy API shapes."""

    versions: dict[str, str] = {}
    for item in definitions:
        identifiers = definition_ids((item,))
        tag = item.get("dockerImageTag") or item.get("docker_image_tag")
        if len(identifiers) == 1 and isinstance(tag, str) and tag:
            versions[next(iter(identifiers))] = tag
    return versions


def access_token_provider(
    base_url: str,
    client_id: str,
    client_secret: str,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = 30.0,
) -> Callable[[], str]:
    """Return a provider that obtains a fresh short-lived Airbyte token.

    Airbyte documents three-minute Cloud tokens and recommends refreshing
    before each request. The provider follows that rule, which also keeps long
    Stage 1 job polling valid.
    """
    if not client_id or not client_secret:
        raise ValueError("Airbyte client_id and client_secret are required")
    endpoint = urllib.parse.urljoin(
        str(base_url).rstrip("/") + "/", "applications/token"
    )
    open_request = opener or urllib.request.urlopen

    def provide() -> str:
        body = json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant-type": "client_credentials",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = open_request(request, timeout=float(timeout))
            with response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            raise AirbyteError("could not obtain an Airbyte access token") from exc
        if not isinstance(payload, dict):
            raise AirbyteError("Airbyte token response is not an object")
        token = payload.get("access_token") or payload.get("accessToken")
        if not isinstance(token, str) or not token:
            raise AirbyteError("Airbyte token response contains no access token")
        return token

    return provide

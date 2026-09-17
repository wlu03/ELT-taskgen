"""Model an attempt-local Airbyte lifecycle without invoking Airbyte.

The proxy consumes normalized Terraform intent and delegates bytes to trusted
local sync.
"""

from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.models import PopulationName, canonical_json
from elt_taskgen.training.local_sync import LocalSyncExecution, run_local_sync
from elt_taskgen.training.namespace import quote_duckdb_identifier
from elt_taskgen.training.package import WorkspacePackage
from elt_taskgen.training.terraform_intent import TerraformIntentGraph


AIRBYTE_PROXY_SCHEMA_VERSION = "airbyte-protocol-proxy-v1"


class AirbyteProxyErrorCode(str, Enum):
    INVALID_GRAPH = "airbyte_proxy_invalid_graph"
    WRONG_DESTINATION = "airbyte_proxy_wrong_destination"
    WRONG_NAMESPACE = "airbyte_proxy_wrong_namespace"
    DUPLICATE_CONNECTION = "airbyte_proxy_duplicate_connection"
    INVALID_STREAM = "airbyte_proxy_invalid_stream"
    INVALID_SYNC_MODE = "airbyte_proxy_invalid_sync_mode"
    CONNECTION_INVALID = "airbyte_proxy_connection_invalid"
    JOB_NOT_FOUND = "airbyte_proxy_job_not_found"
    JOB_NOT_CANCELLABLE = "airbyte_proxy_job_not_cancellable"
    STATE_INVALID = "airbyte_proxy_state_invalid"


class AirbyteJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AirbyteResource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    kind: str
    identity: str
    valid: bool = True
    error_code: AirbyteProxyErrorCode | None = None


class AirbyteJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    connection_ids: tuple[str, ...]
    status: AirbyteJobStatus
    created_tick: int = Field(ge=0)
    updated_tick: int = Field(ge=0)
    error_code: AirbyteProxyErrorCode | None = None


class AirbyteProxyState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: str = AIRBYTE_PROXY_SCHEMA_VERSION
    release_id: str
    task_id: str
    population: str
    destination: str
    logical_tick: int = Field(ge=0)
    workspace: AirbyteResource
    sources: tuple[AirbyteResource, ...]
    destinations: tuple[AirbyteResource, ...]
    connections: tuple[AirbyteResource, ...]
    jobs: tuple[AirbyteJob, ...] = ()


def deterministic_airbyte_id(
    *, release_id: str, task_id: str, population: str, destination: str,
    resource_kind: str, identity: str,
) -> str:
    material = "\x00".join(
        (release_id, task_id, population, destination, resource_kind, identity)
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{resource_kind}_{digest[:24]}"


class AirbyteProtocolProxy:
    """One isolated deterministic state machine bound to an attempt runtime."""

    def __init__(
        self,
        package: WorkspacePackage,
        population: PopulationName | str,
        graph: TerraformIntentGraph,
        runtime_dir: Path,
        *,
        sync: Callable[..., LocalSyncExecution] = run_local_sync,
    ) -> None:
        self.package = package
        self.population = (
            population.value if isinstance(population, PopulationName) else str(population)
        )
        self.graph = graph
        self.runtime_dir = Path(runtime_dir)
        self._sync = sync
        self._execution: LocalSyncExecution | None = None
        self._prepare_runtime()
        self.state = self._initial_state()
        self._persist()

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "airbyte-state.json"

    @property
    def execution(self) -> LocalSyncExecution | None:
        return self._execution

    def _prepare_runtime(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=False)
        if self.runtime_dir.is_symlink() or not self.runtime_dir.is_dir():
            raise ValueError(AirbyteProxyErrorCode.STATE_INVALID.value)

    def _id(self, kind: str, identity: str) -> str:
        return deterministic_airbyte_id(
            release_id=self.package.manifest.release_id,
            task_id=self.package.task_id,
            population=self.population,
            destination=self.graph.destination.kind,
            resource_kind=kind,
            identity=identity,
        )

    def _initial_state(self) -> AirbyteProxyState:
        workspace = AirbyteResource(
            id=self._id("workspace", "default"), kind="workspace", identity="default"
        )
        sources = tuple(
            AirbyteResource(
                id=self._id("source", source.source_key),
                kind="source",
                identity=source.source_key,
            )
            for source in sorted(self.graph.sources, key=lambda item: item.source_key)
        )
        destination = AirbyteResource(
            id=self._id("destination", self.graph.destination.kind),
            kind="destination",
            identity=self.graph.destination.kind,
            valid=self.graph.destination.kind == self.package.destination.value,
            error_code=(
                None
                if self.graph.destination.kind == self.package.destination.value
                else AirbyteProxyErrorCode.WRONG_DESTINATION
            ),
        )
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        connections: list[AirbyteResource] = []
        expected = {table.name for table in self.package.task.tables}
        for index, connection in enumerate(self.graph.connections):
            key = (connection.source_key, connection.streams)
            error: AirbyteProxyErrorCode | None = None
            if key in seen:
                error = AirbyteProxyErrorCode.DUPLICATE_CONNECTION
            elif connection.namespace_definition != "destination":
                error = AirbyteProxyErrorCode.WRONG_NAMESPACE
            elif any(mode != "full_refresh_append" for _, mode in connection.streams):
                error = AirbyteProxyErrorCode.INVALID_SYNC_MODE
            elif any(stream not in expected for stream, _ in connection.streams):
                error = AirbyteProxyErrorCode.INVALID_STREAM
            seen.add(key)
            identity = f"{index}:{connection.source_key}:" + ",".join(
                f"{stream}:{mode}" for stream, mode in connection.streams
            )
            connections.append(
                AirbyteResource(
                    id=self._id("connection", identity),
                    kind="connection",
                    identity=identity,
                    valid=error is None,
                    error_code=error,
                )
            )
        return AirbyteProxyState(
            release_id=self.package.manifest.release_id,
            task_id=self.package.task_id,
            population=self.population,
            destination=self.graph.destination.kind,
            logical_tick=0,
            workspace=workspace,
            sources=sources,
            destinations=(destination,),
            connections=tuple(connections),
        )

    def _persist(self) -> None:
        payload = canonical_json(self.state.model_dump(mode="json")) + "\n"
        temporary = self.runtime_dir / ".airbyte-state.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def validate_connections(self) -> tuple[AirbyteResource, ...]:
        """Return stable validation resources; invalid means sync cannot start."""
        return self.state.connections

    def queue_sync(self) -> AirbyteJob:
        """Create a cancellable pending job without executing source readers."""

        tick = self.state.logical_tick + 1
        job = AirbyteJob(
            id=self._id("job", f"sync:{len(self.state.jobs)}"),
            connection_ids=tuple(item.id for item in self.state.connections),
            status=AirbyteJobStatus.PENDING,
            created_tick=tick,
            updated_tick=tick,
        )
        self.state = self.state.model_copy(
            update={"logical_tick": tick, "jobs": (*self.state.jobs, job)}
        )
        self._persist()
        return job

    def trigger_sync(self, database_path: Path) -> AirbyteJob:
        invalid = [item for item in (*self.state.destinations, *self.state.connections) if not item.valid]
        tick = self.state.logical_tick + 1
        job_id = self._id("job", f"sync:{len(self.state.jobs)}")
        running = AirbyteJob(
            id=job_id,
            connection_ids=tuple(item.id for item in self.state.connections),
            status=AirbyteJobStatus.RUNNING,
            created_tick=tick,
            updated_tick=tick,
        )
        self.state = self.state.model_copy(
            update={"logical_tick": tick, "jobs": (*self.state.jobs, running)}
        )
        self._persist()
        if invalid:
            terminal = running.model_copy(
                update={
                    "status": AirbyteJobStatus.FAILED,
                    "updated_tick": tick + 1,
                    "error_code": invalid[0].error_code or AirbyteProxyErrorCode.CONNECTION_INVALID,
                }
            )
        else:
            if self._execution is None:
                self._execution = self._sync(
                    self.package, self.population, self.graph, Path(database_path)
                )
            else:
                # full_refresh_append preserves the existing destination rows;
                # replay the already-validated raw relations exactly once.
                import duckdb

                connection = duckdb.connect(str(self._execution.database_path))
                try:
                    connection.execute("BEGIN TRANSACTION")
                    for selection in self._execution.selected_streams:
                        relation = (
                            f"{quote_duckdb_identifier(self._execution.namespace.raw_schema)}."
                            f"{quote_duckdb_identifier(selection.stream_name)}"
                        )
                        connection.execute(f"INSERT INTO {relation} SELECT * FROM {relation}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                finally:
                    connection.close()
            terminal = running.model_copy(
                update={
                    "status": (
                        AirbyteJobStatus.SUCCEEDED
                        if self._execution.sync_lifecycle
                        else AirbyteJobStatus.FAILED
                    ),
                    "updated_tick": tick + 1,
                    "error_code": (
                        None
                        if self._execution.sync_lifecycle
                        else AirbyteProxyErrorCode.CONNECTION_INVALID
                    ),
                }
            )
        self.state = self.state.model_copy(
            update={"logical_tick": tick + 1, "jobs": (*self.state.jobs[:-1], terminal)}
        )
        self._persist()
        return terminal

    def poll_job(self, job_id: str) -> AirbyteJob:
        for job in self.state.jobs:
            if job.id == job_id:
                return job
        raise ValueError(AirbyteProxyErrorCode.JOB_NOT_FOUND.value)

    def cancel_job(self, job_id: str) -> AirbyteJob:
        jobs = list(self.state.jobs)
        for index, job in enumerate(jobs):
            if job.id != job_id:
                continue
            if job.status not in {AirbyteJobStatus.PENDING, AirbyteJobStatus.RUNNING}:
                raise ValueError(AirbyteProxyErrorCode.JOB_NOT_CANCELLABLE.value)
            tick = self.state.logical_tick + 1
            jobs[index] = job.model_copy(
                update={"status": AirbyteJobStatus.CANCELLED, "updated_tick": tick}
            )
            self.state = self.state.model_copy(update={"logical_tick": tick, "jobs": tuple(jobs)})
            self._persist()
            return jobs[index]
        raise ValueError(AirbyteProxyErrorCode.JOB_NOT_FOUND.value)


__all__ = [
    "AIRBYTE_PROXY_SCHEMA_VERSION",
    "AirbyteJob",
    "AirbyteJobStatus",
    "AirbyteProtocolProxy",
    "AirbyteProxyErrorCode",
    "AirbyteProxyState",
    "AirbyteResource",
    "deterministic_airbyte_id",
]

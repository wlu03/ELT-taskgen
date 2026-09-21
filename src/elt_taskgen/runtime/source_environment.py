"""Manage an isolated source population.

Drive generated table artifacts from ``sources_serving.json`` through the
ELT-Bench Docker source stack. Airbyte sources and connections remain solver
work.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from elt_taskgen.export.release import (
    COMBINED_CORPUS_PROFILE,
    COMBINED_PUBLIC_LAYOUT,
    ReleaseManifest,
)
from elt_taskgen.runtime.process import ProcessFailure, Runner, SubprocessRunner
from elt_taskgen.package_resources import resource_path
from elt_taskgen.runtime.source_images import SOURCE_SERVICE_IMAGES
from elt_taskgen.runtime.source_server import load_routes

_SAFE_DATABASE = re.compile(r"[a-z0-9][a-z0-9_]*")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
_SAFE_S3_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_SAFE_PROJECT = re.compile(r"[^a-z0-9_-]+")
_SAFE_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
#: The flat-file service listens on TLS here. The Airbyte source-file connector
#: rewrites every URL to https://<host>, so the port carries a TLS listener
#: rather than the plain HTTP the other source services speak.
FLAT_FILES_PORT = 8443

#: Certificate material for that listener, shipped with the package because the
#: connector image trusts this authority and nothing generates one per run.
SOURCE_TLS_RESOURCE_DIR = "source_tls"

_SOURCE_SERVICES = (
    "elt-postgres",
    "elt-mongodb",
    "elt-localstack",
    "elt-api",
    "elt-files",
)

DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS = 180.0
DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS = 30.0
DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS = 5.0
_AIRBYTE_CONTROL_PLANE_PROBES = (
    ("airbyte-server", "http://127.0.0.1:80/api/public/v1/health"),
    ("kube-apiserver", "https://127.0.0.1:6443/readyz"),
    ("kube-scheduler", "https://127.0.0.1:10259/readyz"),
    ("kube-controller-manager", "https://127.0.0.1:10257/healthz"),
)


class SourceEnvironmentError(ValueError):
    """A release cannot be represented as an isolated source environment."""


def _positive_finite_seconds(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceEnvironmentError(f"{label} must be a positive number of seconds")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise SourceEnvironmentError(f"{label} must be a positive number of seconds")
    return seconds


def _validated_airbyte_container(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_CONTAINER.fullmatch(value):
        raise SourceEnvironmentError("invalid Airbyte control-plane container name")
    return value


def _unready_airbyte_control_plane_components(
    runner: Runner | None,
    airbyte_container: str,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[tuple[str, ...], bool]:
    """Return fixed labels for failed Airbyte and kind component probes.

    Probe component endpoints directly and omit external output from errors.
    """

    unready: list[str] = []
    for component, endpoint in _AIRBYTE_CONTROL_PLANE_PROBES:
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            return tuple(unready), True
        # Curl has a five-second in-container deadline. A fresh host runner per
        # probe also bounds Docker itself by the smaller remaining global budget.
        probe_runner = runner or SubprocessRunner(timeout=min(7.0, remaining))
        curl_timeout = min(5.0, remaining)
        try:
            probe_runner.run(
                (
                    "docker",
                    "exec",
                    airbyte_container,
                    "curl",
                    "--insecure",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--output",
                    "/dev/null",
                    "--connect-timeout",
                    "2",
                    "--max-time",
                    f"{curl_timeout:g}",
                    endpoint,
                )
            )
        except ProcessFailure:
            unready.append(component)
        if monotonic() >= deadline:
            return tuple(unready), True
    return tuple(unready), False


def wait_for_airbyte_control_plane(
    *,
    airbyte_container: str = "airbyte-abctl-control-plane",
    timeout: float = DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS,
    stable_for: float = DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS,
    poll_interval: float = DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for all local control-plane probes to remain healthy.

    A failed sample resets the bounded stability window. Timeout errors contain
    only fixed component labels.
    """

    airbyte_container = _validated_airbyte_container(airbyte_container)
    timeout_seconds = _positive_finite_seconds(
        timeout, label="Airbyte readiness timeout"
    )
    stability_seconds = _positive_finite_seconds(
        stable_for, label="Airbyte stability window"
    )
    poll_seconds = _positive_finite_seconds(
        poll_interval, label="Airbyte readiness poll interval"
    )
    if stability_seconds >= timeout_seconds:
        raise SourceEnvironmentError(
            "Airbyte stability window must be shorter than the readiness timeout"
        )
    if poll_seconds > stability_seconds / 2.0:
        raise SourceEnvironmentError(
            "Airbyte readiness poll interval cannot exceed half the stability window"
        )

    started_at = monotonic()
    deadline = started_at + timeout_seconds
    stable_since: float | None = None
    last_observation = "no readiness sample completed"

    while True:
        if monotonic() >= deadline:
            raise SourceEnvironmentError(
                "Airbyte control plane did not remain ready for "
                f"{stability_seconds:g} seconds within {timeout_seconds:g} "
                f"seconds ({last_observation})"
            )
        unready, deadline_expired = _unready_airbyte_control_plane_components(
            runner,
            airbyte_container,
            deadline=deadline,
            monotonic=monotonic,
        )
        observed_at = monotonic()
        if deadline_expired or observed_at >= deadline:
            if unready:
                last_observation = "unready: " + ", ".join(unready)
            elif last_observation == "no readiness sample completed":
                last_observation = "readiness deadline expired during probe sample"
            raise SourceEnvironmentError(
                "Airbyte control plane did not remain ready for "
                f"{stability_seconds:g} seconds within {timeout_seconds:g} "
                f"seconds ({last_observation})"
            )
        if unready:
            stable_since = None
            last_observation = "unready: " + ", ".join(unready)
        else:
            if stable_since is None:
                stable_since = observed_at
            stable_elapsed = max(0.0, observed_at - stable_since)
            if stable_elapsed >= stability_seconds:
                return
            last_observation = (
                "all components ready; sustained for "
                f"{stable_elapsed:g} of {stability_seconds:g} seconds"
            )

        remaining = deadline - observed_at
        if remaining <= 0.0:
            raise SourceEnvironmentError(
                "Airbyte control plane did not remain ready for "
                f"{stability_seconds:g} seconds within {timeout_seconds:g} "
                f"seconds ({last_observation})"
            )
        sleep(min(poll_seconds, remaining))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceEnvironmentError(f"invalid JSON artifact: {path}") from exc


def _safe_project(task_id: str, population: str, attempt_key: str = "") -> str:
    prefix = _SAFE_PROJECT.sub("-", f"elt-{task_id}-{population}".lower()).strip("-_")
    digest = hashlib.sha256(
        f"{task_id}\0{population}\0{attempt_key}".encode("utf-8")
    ).hexdigest()[:10]
    prefix = prefix[:42].rstrip("-_") or "elt-task"
    return f"{prefix}-{digest}"


def _release_manifest(release_dir: Path) -> ReleaseManifest:
    path = release_dir / "release_manifest.json"
    try:
        manifest = ReleaseManifest.model_validate(_load_json(path))
    except Exception as exc:
        raise SourceEnvironmentError(f"invalid release manifest: {path}") from exc
    if (
        manifest.corpus_profile != COMBINED_CORPUS_PROFILE
        or manifest.public_layout != COMBINED_PUBLIC_LAYOUT
    ):
        raise SourceEnvironmentError(
            "source environments require a schema-3 combined ELT-Bench release"
        )
    return manifest


def _compose_document(
    *, rendered_root: Path, manifest_path: Path, server_script: Path, network: str
) -> dict[str, Any]:
    tls_dir = resource_path(SOURCE_TLS_RESOURCE_DIR)
    for name in ("ca.crt", "server.crt", "server.key"):
        if not (tls_dir / name).is_file():
            raise SourceEnvironmentError(
                f"source TLS material is missing: {tls_dir / name}"
            )
    common_mounts = [
        f"{server_script.resolve()}:/app/source_server.py:ro",
        f"{manifest_path.resolve()}:/config/sources_serving.json:ro",
        f"{rendered_root.resolve()}:/data:ro",
    ]
    server_health = [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://localhost:%s/healthz', timeout=2).read()",
    ]
    return {
        "services": {
            "elt-postgres": {
                "image": SOURCE_SERVICE_IMAGES["elt-postgres"],
                "environment": {
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_PASSWORD": "testelt",
                    "POSTGRES_DB": "postgres",
                },
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U postgres -d postgres"],
                    "interval": "2s",
                    "timeout": "5s",
                    "retries": 30,
                },
            },
            "elt-mongodb": {
                # MongoDB 8.0 refuses to start on Linux 6.19+ (including current
                # Docker Desktop VMs). 8.2 supports that kernel while preserving
                # the MongoDB 8 source contract used by the certification corpus.
                "image": SOURCE_SERVICE_IMAGES["elt-mongodb"],
                "command": ["--replSet", "rs0", "--bind_ip_all", "--port", "27017"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "mongosh --quiet --eval \"try { rs.status().ok } catch (e) { rs.initiate({_id:'rs0',members:[{_id:0,host:'elt-mongodb:27017'}]}).ok }\" | grep 1",
                    ],
                    "interval": "3s",
                    "timeout": "10s",
                    "retries": 40,
                },
            },
            "elt-localstack": {
                # Keep the runtime reproducible enough for pilot certification:
                # never let a source-service stack silently follow `latest`.
                "image": SOURCE_SERVICE_IMAGES["elt-localstack"],
                "environment": {"SERVICES": "s3", "DEBUG": "0"},
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "curl -fsS http://localhost:4566/_localstack/health >/dev/null",
                    ],
                    "interval": "2s",
                    "timeout": "5s",
                    "retries": 40,
                },
            },
            "elt-api": {
                "image": SOURCE_SERVICE_IMAGES["elt-api"],
                "command": [
                    "python",
                    "/app/source_server.py",
                    "--manifest",
                    "/config/sources_serving.json",
                    "--rendered-root",
                    "/data",
                    "--backend",
                    "rest",
                    "--port",
                    "5005",
                ],
                "volumes": common_mounts,
                "healthcheck": {
                    "test": [value % "5005" if "%s" in value else value for value in server_health],
                    "interval": "2s",
                    "timeout": "5s",
                    "retries": 30,
                },
            },
            # Served over TLS on FLAT_FILES_PORT: the Airbyte source-file
            # connector discards the URL scheme and always fetches
            # https://<host>, so a plain-HTTP file server is unreachable for it.
            "elt-files": {
                "image": SOURCE_SERVICE_IMAGES["elt-files"],
                "command": [
                    "python",
                    "/app/source_server.py",
                    "--manifest",
                    "/config/sources_serving.json",
                    "--rendered-root",
                    "/data",
                    "--backend",
                    "files",
                    "--port",
                    str(FLAT_FILES_PORT),
                    "--certfile",
                    "/tls/server.crt",
                    "--keyfile",
                    "/tls/server.key",
                ],
                "volumes": [*common_mounts, f"{tls_dir.resolve()}:/tls:ro"],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import ssl, urllib.request; "
                        "urllib.request.urlopen("
                        f"'https://localhost:{FLAT_FILES_PORT}/healthz', timeout=2, "
                        "context=ssl.create_default_context(cafile='/tls/ca.crt')"
                        ").read()",
                    ],
                    "interval": "2s",
                    "timeout": "5s",
                    "retries": 30,
                },
            },
        },
        "networks": {"default": {"name": network}},
    }


@dataclass(frozen=True)
class SourceEnvironment:
    release_dir: Path
    task_id: str
    population: str
    environment_dir: Path
    rendered_root: Path
    manifest_path: Path
    compose_path: Path
    project: str
    network: str

    def _compose(self, *args: str) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.compose_path),
            *args,
        )

    def start(
        self,
        runner: Runner | None = None,
        *,
        airbyte_container: str = "airbyte-abctl-control-plane",
    ) -> None:
        """Start source containers and attach Airbyte to this fresh network."""
        runner = runner or SubprocessRunner()
        airbyte_container = _validated_airbyte_container(airbyte_container)
        inspection = runner.run(
            (
                "docker",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                airbyte_container,
            )
        )
        try:
            attached = json.loads(inspection.stdout)
        except json.JSONDecodeError as exc:
            raise SourceEnvironmentError(
                "could not inspect Airbyte control-plane networks"
            ) from exc
        if not isinstance(attached, dict):
            raise SourceEnvironmentError(
                "Airbyte control-plane network inspection returned an invalid shape"
            )
        source_networks = sorted(
            str(name)
            for name in attached
            if str(name).endswith("-source-network")
        )
        if source_networks:
            raise SourceEnvironmentError(
                "Airbyte control plane is already attached to a generated source "
                "network; stop that source environment before starting another"
            )
        try:
            runner.run(self._compose("up", "--detach", "--wait"))
            runner.run(("docker", "network", "connect", self.network, airbyte_container))
        except BaseException:
            # A timed-out network connect may still succeed; detach the endpoint
            # before partial-start cleanup removes the generated network.
            try:
                runner.run(
                    (
                        "docker",
                        "network",
                        "disconnect",
                        self.network,
                        airbyte_container,
                    )
                )
            except BaseException:
                pass
            try:
                runner.run(self._compose("down", "--volumes", "--remove-orphans"))
            except BaseException:
                pass
            raise

    def seed(self, runner: Runner | None = None) -> None:
        """Load database fixtures and LocalStack-compatible object fixtures."""
        runner = runner or SubprocessRunner()
        manifest = _load_json(self.manifest_path)
        tables = manifest.get("tables") if isinstance(manifest, dict) else None
        database = manifest.get("database") if isinstance(manifest, dict) else None
        if not isinstance(tables, dict) or not isinstance(database, str):
            raise SourceEnvironmentError("serving manifest has no database/tables")
        if not _SAFE_DATABASE.fullmatch(database):
            raise SourceEnvironmentError(f"unsafe generated database name: {database!r}")

        entries: list[tuple[str, dict[str, Any]]] = []
        for name, raw in sorted(tables.items()):
            if not isinstance(raw, dict):
                raise SourceEnvironmentError(
                    f"serving entry for table {name!r} is not an object"
                )
            entries.append((str(name), raw))
        postgres = [(name, raw) for name, raw in entries if raw.get("backend") == "postgres"]
        mongodb = [(name, raw) for name, raw in entries if raw.get("backend") == "mongodb"]
        s3 = [(name, raw) for name, raw in entries if raw.get("backend") == "s3"]
        files = [(name, raw) for name, raw in entries if raw.get("backend") == "files"]

        if postgres:
            runner.run(
                self._compose(
                    "exec",
                    "--no-TTY",
                    "elt-postgres",
                    "dropdb",
                    "-U",
                    "postgres",
                    "--if-exists",
                    "--force",
                    database,
                )
            )
            runner.run(
                self._compose(
                    "exec",
                    "--no-TTY",
                    "elt-postgres",
                    "createdb",
                    "-U",
                    "postgres",
                    database,
                )
            )
            for table, raw in postgres:
                path = self._artifact_file(raw, "rendered_file", table)
                runner.run(
                    self._compose(
                        "exec",
                        "--no-TTY",
                        "elt-postgres",
                        "psql",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        "postgres",
                        "-d",
                        database,
                    ),
                    stdin_path=path,
                )

        if mongodb:
            runner.run(
                self._compose(
                    "exec",
                    "--no-TTY",
                    "elt-mongodb",
                    "mongosh",
                    "--quiet",
                    "--eval",
                    f'db.getSiblingDB("{database}").dropDatabase()',
                )
            )
            for table, raw in mongodb:
                collection = str(raw.get("collection") or table)
                if not _SAFE_IDENTIFIER.fullmatch(collection):
                    raise SourceEnvironmentError(
                        f"unsafe generated MongoDB collection: {collection!r}"
                    )
                path = self._artifact_file(raw, "rendered_file", table)
                if path.stat().st_size:
                    runner.run(
                        self._compose(
                            "exec",
                            "--no-TTY",
                            "elt-mongodb",
                            "mongoimport",
                            "--uri",
                            "mongodb://localhost:27017/?directConnection=true",
                            "--db",
                            database,
                            "--collection",
                            collection,
                        ),
                        stdin_path=path,
                    )
                else:
                    runner.run(
                        self._compose(
                            "exec",
                            "--no-TTY",
                            "elt-mongodb",
                            "mongosh",
                            "--quiet",
                            "--eval",
                            f'db.getSiblingDB("{database}").createCollection("{collection}")',
                        )
                    )

        buckets = {str(raw.get("bucket") or "") for _, raw in s3}
        file_compatibility_uploads: list[tuple[str, Path]] = []
        if buckets:
            # Seed the same file in LocalStack so the declared S3 source can
            # replace an unavailable Files connector.
            native_keys = {
                str(raw.get("object_key") or "").strip("/")
                for _, raw in s3
                if raw.get("object_key") is not None
            }
            compatibility_keys: set[str] = set()
            for table, raw in files:
                path = self._artifact_file(raw, "rendered_file", table)
                object_key = path.name
                if (
                    not object_key
                    or not _SAFE_S3_KEY.fullmatch(object_key)
                    or object_key in {".", ".."}
                ):
                    raise SourceEnvironmentError(
                        f"unsafe generated flat-file S3 compatibility key for "
                        f"{table!r}: {object_key!r}"
                    )
                if object_key in native_keys or object_key in compatibility_keys:
                    raise SourceEnvironmentError(
                        "duplicate generated S3 object key for flat-file "
                        f"compatibility: {object_key!r}"
                    )
                compatibility_keys.add(object_key)
                file_compatibility_uploads.append((object_key, path))
        for bucket in sorted(buckets):
            if not _SAFE_BUCKET.fullmatch(bucket):
                raise SourceEnvironmentError(f"unsafe generated bucket name: {bucket!r}")
            runner.run(
                self._compose(
                    "exec",
                    "--no-TTY",
                    "elt-localstack",
                    "sh",
                    "-ceu",
                    'awslocal s3 rb "$1" --force >/dev/null 2>&1 || true; awslocal s3 mb "$1"',
                    "sh",
                    f"s3://{bucket}",
                )
            )
        for table, raw in s3:
            bucket = str(raw["bucket"])
            directory = self._artifact_dir(raw, "rendered_dir", table)
            pattern = str(
                raw.get("rendered_parts_glob")
                or raw.get("parts_glob")  # legacy private manifests
                or "part-*.jsonl"
            )
            parts = sorted(directory.glob(pattern))
            if not parts:
                raise SourceEnvironmentError(f"S3 table {table!r} has no {pattern} parts")
            object_key = raw.get("object_key")
            if object_key is not None:
                object_key = str(object_key).strip("/")
                if (
                    not object_key
                    or object_key.startswith("/")
                    or not _SAFE_S3_KEY.fullmatch(object_key)
                    or any(part in ("", ".", "..") for part in object_key.split("/"))
                ):
                    raise SourceEnvironmentError(
                        f"unsafe generated S3 object key for {table!r}: {object_key!r}"
                    )
                # Concatenate generated JSONL chunks at deployment to preserve
                # ELT-Bench's single-object contract.
                merged = self.environment_dir / (
                    ".s3-upload-"
                    + hashlib.sha256(table.encode("utf-8")).hexdigest()[:12]
                    + ".jsonl"
                )
                if merged.exists():
                    raise SourceEnvironmentError(
                        f"temporary S3 upload already exists: {merged}"
                    )
                try:
                    with merged.open("xb") as destination:
                        for part in parts:
                            with part.open("rb") as source:
                                shutil.copyfileobj(source, destination)
                    merged.chmod(0o600)
                    runner.run(
                        self._compose(
                            "exec",
                            "--no-TTY",
                            "elt-localstack",
                            "awslocal",
                            "s3",
                            "cp",
                            "-",
                            f"s3://{bucket}/{object_key}",
                        ),
                        stdin_path=merged,
                    )
                finally:
                    merged.unlink(missing_ok=True)
                continue
            prefix = str(raw.get("key_prefix") or f"{table}/").strip("/")
            if not prefix or any(part in ("", ".", "..") for part in prefix.split("/")):
                raise SourceEnvironmentError(
                    f"unsafe generated S3 key prefix for {table!r}: {prefix!r}"
                )
            for part in parts:
                runner.run(
                    self._compose(
                        "exec",
                        "--no-TTY",
                        "elt-localstack",
                        "awslocal",
                        "s3",
                        "cp",
                        "-",
                        f"s3://{bucket}/{prefix}/{part.name}",
                    ),
                    stdin_path=part,
                )
        for bucket in sorted(buckets):
            for object_key, path in file_compatibility_uploads:
                runner.run(
                    self._compose(
                        "exec",
                        "--no-TTY",
                        "elt-localstack",
                        "awslocal",
                        "s3",
                        "cp",
                        "-",
                        f"s3://{bucket}/{object_key}",
                    ),
                    stdin_path=path,
                )

    def stop(
        self,
        runner: Runner | None = None,
        *,
        airbyte_container: str = "airbyte-abctl-control-plane",
    ) -> None:
        """Detach Airbyte and remove this attempt's containers and volumes."""
        runner = runner or SubprocessRunner()
        airbyte_container = _validated_airbyte_container(airbyte_container)
        try:
            runner.run(
                ("docker", "network", "disconnect", self.network, airbyte_container)
            )
        except ProcessFailure:
            # It may already be disconnected after an interrupted startup.
            # `compose down` remains authoritative and will fail if a foreign
            # attachment still prevents network removal.
            pass
        runner.run(self._compose("down", "--volumes", "--remove-orphans"))

    def _artifact_file(self, raw: dict[str, Any], key: str, table: str) -> Path:
        path = self._artifact(raw, key, table)
        if not path.is_file():
            raise SourceEnvironmentError(f"source file is missing: {path}")
        return path

    def _artifact_dir(self, raw: dict[str, Any], key: str, table: str) -> Path:
        path = self._artifact(raw, key, table)
        if not path.is_dir():
            raise SourceEnvironmentError(f"source directory is missing: {path}")
        return path

    def _artifact(self, raw: dict[str, Any], key: str, table: str) -> Path:
        rel = raw.get(key)
        if not isinstance(rel, str) or not rel:
            raise SourceEnvironmentError(f"table {table!r} has no {key}")
        path = (self.rendered_root / rel).resolve()
        root = self.rendered_root.resolve()
        if path != root and root not in path.parents:
            raise SourceEnvironmentError(f"table {table!r} artifact escapes its population")
        return path


def prepare_source_environment(
    release_dir: Path,
    task_id: str,
    population: str,
    environment_dir: Path,
) -> SourceEnvironment:
    """Create an immutable compose plan for one released population.

    ``environment_dir`` must be fresh and separate from the solver workdir so
    private population fixtures never cross the task boundary.
    """
    release_dir = Path(release_dir).resolve()
    environment_dir = Path(environment_dir).resolve()
    manifest = _release_manifest(release_dir)
    if task_id not in manifest.tasks:
        raise SourceEnvironmentError(f"task {task_id!r} is not in this release")
    source_rel = (manifest.el_sources.get(task_id) or {}).get(population)
    if not source_rel:
        raise SourceEnvironmentError(
            f"task {task_id!r} has no released source population {population!r}"
        )
    rendered_root = (release_dir / source_rel).resolve()
    release_private = (release_dir / "private" / task_id).resolve()
    if rendered_root != release_private and release_private not in rendered_root.parents:
        raise SourceEnvironmentError("release source mapping escapes the private task root")
    manifest_path = release_private / "answer_key" / "sources_serving.json"
    if environment_dir.exists():
        raise SourceEnvironmentError(
            f"source environment destination already exists: {environment_dir}"
        )
    if not rendered_root.is_dir() or not manifest_path.is_file():
        raise SourceEnvironmentError("released source population/serving manifest is missing")
    # Validate HTTP artifacts before writing or starting anything.
    load_routes(manifest_path, rendered_root, mode="both")

    project = _safe_project(task_id, population, str(environment_dir))
    network = f"{project}-source-network"
    staging = environment_dir.with_name(f".{environment_dir.name}.staging-{project[-10:]}")
    if staging.exists():
        raise SourceEnvironmentError(f"stale source-environment staging path: {staging}")
    staging.mkdir(parents=True)
    try:
        server_script = staging / "source_server.py"
        shutil.copy2(Path(__file__).with_name("source_server.py"), server_script)
        compose_path = staging / "compose.yaml"
        compose_path.write_text(
            yaml.safe_dump(
                _compose_document(
                    rendered_root=rendered_root,
                    manifest_path=manifest_path,
                    # Compose is consumed after the staged tree is renamed.
                    # Pin the final path, not the temporary staging path.
                    server_script=environment_dir / "source_server.py",
                    network=network,
                ),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        staging.rename(environment_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SourceEnvironment(
        release_dir=release_dir,
        task_id=task_id,
        population=population,
        environment_dir=environment_dir,
        rendered_root=rendered_root,
        manifest_path=manifest_path,
        compose_path=environment_dir / "compose.yaml",
        project=project,
        network=network,
    )


def load_source_environment(
    release_dir: Path,
    task_id: str,
    population: str,
    environment_dir: Path,
) -> SourceEnvironment:
    """Re-open a previously prepared plan without regenerating or mutating it."""
    release_dir = Path(release_dir).resolve()
    environment_dir = Path(environment_dir).resolve()
    manifest = _release_manifest(release_dir)
    if task_id not in manifest.tasks:
        raise SourceEnvironmentError(f"task {task_id!r} is not in this release")
    source_rel = (manifest.el_sources.get(task_id) or {}).get(population)
    if not source_rel:
        raise SourceEnvironmentError(
            f"task {task_id!r} has no released source population {population!r}"
        )
    rendered_root = (release_dir / source_rel).resolve()
    manifest_path = release_dir / "private" / task_id / "answer_key" / "sources_serving.json"
    compose_path = environment_dir / "compose.yaml"
    server_script = environment_dir / "source_server.py"
    if not compose_path.is_file() or not server_script.is_file():
        raise SourceEnvironmentError(
            f"source environment is not prepared at {environment_dir}"
        )
    load_routes(manifest_path, rendered_root, mode="both")
    project = _safe_project(task_id, population, str(environment_dir))
    return SourceEnvironment(
        release_dir=release_dir,
        task_id=task_id,
        population=population,
        environment_dir=environment_dir,
        rendered_root=rendered_root,
        manifest_path=manifest_path,
        compose_path=compose_path,
        project=project,
        network=f"{project}-source-network",
    )

"""Run bounded subprocesses and containers without exposing secrets.

``SubprocessRunner`` kills the child process group at its deadline.
``DockerRunner`` requires an explicit network lane, an unprivileged user, and
an argument list without privileged options. On a timeout, an interruption or a
client error it kills the container by id, not only the Docker client.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "CommandResult",
    "CLOUD_EGRESS_NETWORK",
    "DEFAULT_SUBPROCESS_TIMEOUT_SECONDS",
    "DOCKER_LANES",
    "DOCKER_LANE_CLOUD_EGRESS",
    "DOCKER_LANE_NONE",
    "DOCKER_LANE_PROXY_BRIDGE",
    "DockerRunner",
    "PROXY_BRIDGE_NETWORK",
    "ProcessFailure",
    "ProcessTimeout",
    "Runner",
    "SandboxArgvError",
    "SubprocessRunner",
    "lint_sandbox_argv",
]

#: Every runner is bounded: a runtime program that never returns is a fault,
#: not a wait.  Matches the CLI's ``--runner-timeout`` default.
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin_path: Path | None = None,
    ) -> CommandResult: ...


class ProcessFailure(RuntimeError):
    """A runtime program failed; output is intentionally not embedded."""


class ProcessTimeout(ProcessFailure):
    """A runtime program exceeded its wall-clock limit and was killed."""


class _StreamTail:
    """Bounded rolling capture of one child stream: the last ``cap`` bytes.

    Draining on a thread keeps parent memory at ``cap`` plus one read chunk
    per stream no matter how much the child writes, while the retained tail
    equals the historical ``full_output[-cap:]`` truncation exactly.
    """

    _CHUNK = 65536

    def __init__(self, stream, cap: int) -> None:
        self._stream = stream
        self._cap = cap
        self._tail = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            while True:
                # read1 returns what one read of the pipe yields, so a short
                # write is captured at once rather than after a full chunk.
                chunk = self._stream.read1(self._CHUNK)
                if not chunk:
                    break
                self._tail += chunk
                overflow = len(self._tail) - self._cap
                if overflow > 0:
                    del self._tail[:overflow]
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def text(self) -> str:
        """Decode the tail of a stream that has reached EOF."""
        if self.thread.is_alive():
            raise RuntimeError("output capture has not finished draining")
        return bytes(self._tail).decode("utf-8", errors="replace")


def _validate_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive number of seconds")
    value = float(timeout)
    if not math.isfinite(value) or not value > 0.0:
        raise ValueError("timeout must be a positive number of seconds")
    return value


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's whole session, then the child itself as a backstop.

    The child was started with ``start_new_session=True`` so its pid is the
    process-group id: grandchildren that inherited the pipes die with it
    instead of surviving the deadline as orphans.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


class SubprocessRunner:
    """Run argv without a shell and fail without echoing arguments/secrets."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.timeout = _validate_timeout(timeout)
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin_path: Path | None = None,
    ) -> CommandResult:
        if not argv:
            raise ValueError("cannot execute an empty command")
        merged_env = os.environ.copy()
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        handle = Path(stdin_path).open("rb") if stdin_path is not None else None
        try:
            process = subprocess.Popen(
                [str(value) for value in argv],
                cwd=str(cwd) if cwd is not None else None,
                env=merged_env,
                stdin=handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + self.timeout
            stdout_tail = _StreamTail(process.stdout, self.max_output_bytes)
            stderr_tail = _StreamTail(process.stderr, self.max_output_bytes)
            try:
                returncode = process.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
                # Output is complete only at EOF on both pipes, and a
                # descendant that inherited them keeps them open after the
                # direct child exits. Draining shares the one deadline; if it
                # has not finished by then, the run timed out.
                for tail in (stdout_tail, stderr_tail):
                    tail.thread.join(max(0.0, deadline - time.monotonic()))
                if stdout_tail.thread.is_alive() or stderr_tail.thread.is_alive():
                    raise subprocess.TimeoutExpired(process.args, self.timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_process_group(process)
                process.wait()
                executable = Path(str(argv[0])).name
                raise ProcessTimeout(f"{executable} exceeded its runtime limit") from exc
            except BaseException:
                # The child runs in its own session, so a terminal interrupt
                # no longer reaches it: an interrupted parent takes it along.
                _kill_process_group(process)
                process.wait()
                raise
            finally:
                # Bounded joins after a kill: descendants in the killed group
                # close the pipes; one outside it must not hang the parent.
                stdout_tail.thread.join(5.0)
                stderr_tail.thread.join(5.0)
        finally:
            if handle is not None:
                handle.close()
        if returncode:
            executable = Path(str(argv[0])).name
            raise ProcessFailure(
                f"{executable} failed with exit code {returncode}; "
                "command output is withheld from the public error"
            )
        return CommandResult(returncode, stdout_tail.text(), stderr_tail.text())


_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$")

#: No network at all: the lane for every local tool and grader.
DOCKER_LANE_NONE = "none"
#: Reserved for the sparse cloud lane (Phase 5): a dedicated bridge whose
#: only endpoint is the Airbyte control plane (D-09).  Never a local default.
DOCKER_LANE_PROXY_BRIDGE = "proxy-bridge"
#: Explicit outbound network for cloud warehouse clients.  Unlike the Airbyte
#: proxy lane it does not add a host-gateway alias.  Never a local default.
DOCKER_LANE_CLOUD_EGRESS = "cloud-egress"
DOCKER_LANES = frozenset(
    {DOCKER_LANE_NONE, DOCKER_LANE_PROXY_BRIDGE, DOCKER_LANE_CLOUD_EGRESS}
)
#: The operator-provisioned Docker network the proxy-bridge lane attaches to.
PROXY_BRIDGE_NETWORK = "elt-proxy-bridge"
#: The operator-provisioned Docker network used for warehouse egress.
CLOUD_EGRESS_NETWORK = "elt-cloud-egress"

_CONTAINER_USER = re.compile(r"^[0-9]{1,10}:[0-9]{1,10}$")
#: runc refuses ids outside 0..2**31-1.
_MAX_CONTAINER_ID = 2**31 - 1
_HOST_NAMESPACE_FLAGS = ("--network", "--net", "--pid", "--ipc", "--uts", "--userns")
_FORBIDDEN_SWITCHES = frozenset({"--privileged", "--cap-add", "--device"})
_MOUNT_FLAGS = frozenset({"-v", "--volume", "--mount", "--tmpfs"})
_UNCONFINED = re.compile(r"(?:seccomp|apparmor)\s*=\s*unconfined")


class SandboxArgvError(ValueError):
    """A container argv asked for a privilege the sandbox never grants."""


def _refuse(reason: str) -> None:
    raise SandboxArgvError(f"sandbox argv refused: {reason}")


def lint_sandbox_argv(argv: Sequence[str]) -> None:
    """Reject container arguments that weaken sandbox isolation.

    Check privileges, capabilities, devices, host namespaces, security
    profiles, and socket mounts across the full argument list.
    """
    items = [str(value) for value in argv]
    for index, item in enumerate(items):
        flag, separator, inline_value = item.partition("=")
        following = items[index + 1] if index + 1 < len(items) else ""
        value = inline_value if separator else following
        if flag in _FORBIDDEN_SWITCHES:
            _refuse(f"{flag} is never granted")
        if flag in _HOST_NAMESPACE_FLAGS and value.strip().lower() == "host":
            _refuse(f"{flag} host shares a host namespace")
        if flag == "--security-opt" and _UNCONFINED.search(value.lower()):
            _refuse("an unconfined security profile")
        if _UNCONFINED.search(item.lower()):
            _refuse("an unconfined security profile")
        if flag in _MOUNT_FLAGS and ".sock" in value.lower():
            _refuse("a socket mount")
        if item.lower().endswith(".sock") or "docker.sock" in item.lower():
            _refuse("a socket mount")


def _default_container_user() -> str:
    """The invoking uid:gid, so the bind-mounted workspace stays writable
    without ever running the container as root; ``nobody`` elsewhere."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return "65534:65534"
    return f"{getuid()}:{getgid()}"


class DockerRunner:
    """Run solver commands with one mounted work tree and no host environment.

    Require a content-addressed image. ``lane`` selects no network, the proxy
    bridge, or the dedicated cloud-egress bridge.
    """

    def __init__(
        self,
        workspace: Path,
        image: str,
        *,
        timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        memory: str = "4g",
        cpus: str = "2",
        host_runner: Runner | None = None,
        lane: str = DOCKER_LANE_NONE,
        user: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("solver workspace must be a directory")
        if not _DIGEST_IMAGE.fullmatch(str(image)):
            raise ValueError(
                "solver runner image must use an immutable @sha256 digest"
            )
        if lane not in DOCKER_LANES:
            raise ValueError(
                f"unknown sandbox lane {lane!r}; expected one of {sorted(DOCKER_LANES)}"
            )
        resolved_user = _default_container_user() if user is None else str(user)
        if not _CONTAINER_USER.fullmatch(resolved_user):
            raise ValueError("container user must be a numeric uid:gid")
        # Docker reads the ids as numbers, so "00:1000" is root: compare the
        # parsed uid and forward the canonical spelling.
        uid, gid = (int(part) for part in resolved_user.split(":"))
        if uid > _MAX_CONTAINER_ID or gid > _MAX_CONTAINER_ID:
            raise ValueError(f"container uid and gid must be in range 0-{_MAX_CONTAINER_ID}")
        if uid == 0:
            raise ValueError("container user must not be root")
        resolved_user = f"{uid}:{gid}"
        self.image = str(image)
        self.memory = str(memory)
        self.cpus = str(cpus)
        self.lane = str(lane)
        self.user = resolved_user
        self.host_runner = host_runner or SubprocessRunner(timeout=timeout)

    def _relative(self, path: Path | None, *, label: str) -> Path:
        resolved = (path or self.workspace).resolve(strict=True)
        try:
            return resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(f"{label} must remain inside the solver workspace") from exc

    def _network_options(self) -> list[str]:
        if self.lane == DOCKER_LANE_PROXY_BRIDGE:
            return [
                "--network",
                PROXY_BRIDGE_NETWORK,
                "--add-host",
                "host.docker.internal:host-gateway",
            ]
        if self.lane == DOCKER_LANE_CLOUD_EGRESS:
            return ["--network", CLOUD_EGRESS_NETWORK]
        return ["--network", "none"]

    def _kill_container(self, cidfile: Path, cause: BaseException) -> bool:
        """``docker kill`` this run's container by its recorded id.

        Return False when an id was recorded but the kill did not run; the
        caller then keeps the id file, and ``cause`` gains a note naming it.
        A missing or empty id file means docker never recorded a container.
        """
        try:
            container_id = cidfile.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return True
        except (OSError, UnicodeError):
            container_id = None
        if container_id == "":
            return True
        if container_id is not None and re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
            try:
                self.host_runner.run(("docker", "kill", container_id))
                return True
            except (ProcessFailure, OSError, ValueError):
                pass
        cause.add_note(f"container cleanup did not run; its id file is kept at {cidfile}")
        return False

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin_path: Path | None = None,
    ) -> CommandResult:
        if not argv:
            raise ValueError("cannot execute an empty solver command")
        relative_cwd = self._relative(cwd, label="solver working directory")
        if stdin_path is not None:
            self._relative(Path(stdin_path), label="solver stdin")
        container_cwd = Path("/workspace") / relative_cwd
        cid_dir = Path(tempfile.mkdtemp(prefix="elt-taskgen-cid-"))
        cidfile = cid_dir / "container.id"
        keep_cid_dir = False
        try:
            command = [
                "docker",
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=256",
                "--user",
                self.user,
                *self._network_options(),
                "--memory",
                self.memory,
                "--cpus",
                self.cpus,
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=512m",
                "--mount",
                # Bind mounts are writable by default. Docker 29 rejects a
                # bare `rw` field in --mount's key=value grammar.
                f"type=bind,src={self.workspace},dst=/workspace",
                "--workdir",
                str(container_cwd),
            ]
            if stdin_path is not None:
                # docker run keeps container stdin closed without it, so the
                # file would reach only the client. No --tty: a terminal would
                # alter the bytes.
                command.append("--interactive")
            forwarded_env: dict[str, str] = {}
            for key, value in sorted((env or {}).items()):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
                    raise ValueError(f"invalid solver environment key {key!r}")
                # Pass only names; values remain outside argv and command logs.
                name = str(key)
                command.extend(("--env", name))
                forwarded_env[name] = str(value)
            command.append(self.image)
            command.extend(str(value) for value in argv)
            lint_sandbox_argv(command)
            try:
                return self.host_runner.run(
                    tuple(command),
                    env=forwarded_env or None,
                    stdin_path=stdin_path,
                )
            except BaseException as exc:
                if isinstance(exc, ProcessFailure) and not isinstance(exc, ProcessTimeout):
                    # The client exited with the container's own status, and
                    # --rm has already removed the container.
                    raise
                # A deadline, an interruption or a client error ends the docker
                # client, not the container: the daemon keeps it running until
                # it is killed by id. The id stays on disk until the kill runs.
                keep_cid_dir = True
                keep_cid_dir = not self._kill_container(cidfile, exc)
                raise
        finally:
            if not keep_cid_dir:
                shutil.rmtree(cid_dir, ignore_errors=True)

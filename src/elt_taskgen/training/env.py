"""Provide a Docker-free, single-step environment for ELT artifacts.

Install a public task, write one ``elt/``-confined artifact, seal it, and score
the complete hidden set. The real grader runs in a killable child process. An
outer deadline yields an unlabelled harness fault; only grader-internal candidate
limits yield measured zeroes. In-process scoring is reserved for test doubles.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from elt_taskgen.review.session import (
    SandboxFault,
    SessionFault,
    ToolDeadlineExceeded,
    ToolHarnessFault,
)
from elt_taskgen.training.contract import (
    WORKSPACE_ERROR_CLASS_BY_CODE,
    WORKSPACE_SCORER_VERSION,
    WorkspaceErrorCode,
)
from elt_taskgen.training.dbt_runner import DbtRunnerLimits, DbtRuntimeConfig
from elt_taskgen.training.models import (
    WorkspaceArtifactFile,
    WorkspaceFailure,
    WorkspaceScoreResult,
)
from elt_taskgen.training.package import WorkspacePackage, load_workspace_package
from elt_taskgen.training.scorer import score_workspace
from elt_taskgen.training.signal import (
    DEFAULT_W_T,
    WorkspaceTrainingSignal,
    drop_group_if_unlabelled,
    training_signal,
)
from elt_taskgen.training.workspace import (
    AttemptWorkspace,
    SealedWorkspace,
    WorkspaceLifecycleError,
    install_workspace,
    seal_workspace,
)


#: Minimum harness deadline for one grader call. Candidate limits size the
#: actual default so measured timeouts cannot become unlabeled harness faults.
DEFAULT_GRADER_DEADLINE_S = 1800.0

#: Per-population budget for trusted replay, sync, validation, and loading.
DEFAULT_TRUSTED_PHASE_BUDGET_S = 200.0

#: Slack keeps candidate dbt caps inside the measured scoring window.
DEFAULT_GRADER_SLACK_S = 300.0

#: dbt commands a candidate runs per graded population inside the grader
#: (parse, compile, run), each bounded by ``DbtRunnerLimits.command_timeout``.
_DBT_COMMANDS_PER_POPULATION = 3


def default_grader_deadline_s(
    dbt_limits: DbtRunnerLimits | None = None,
    *,
    populations: int | None = None,
    trusted_phase_budget_s: float = DEFAULT_TRUSTED_PHASE_BUDGET_S,
    slack_s: float = DEFAULT_GRADER_SLACK_S,
) -> float:
    """Size the outer deadline above candidate caps plus trusted work and slack.

    The result never falls below ``DEFAULT_GRADER_DEADLINE_S``.
    """
    from elt_taskgen.verification.gates import GRADED_POPULATIONS

    count = len(GRADED_POPULATIONS) if populations is None else int(populations)
    command_timeout = float(
        (dbt_limits or DbtRunnerLimits()).command_timeout_seconds
    )
    candidate_dbt_s = count * _DBT_COMMANDS_PER_POPULATION * command_timeout
    trusted_s = count * float(trusted_phase_budget_s)
    sized = candidate_dbt_s + trusted_s + float(slack_s)
    return max(DEFAULT_GRADER_DEADLINE_S, sized)


#: Public name used for faults from the harness-owned grader child.
GRADER_NAME = "score_workspace"

#: Public grader entry; tests may inject an offline compatible double.
GRADER_ENTRY = "elt_taskgen.training.scorer:score_workspace"

#: How long the supervisor waits, after killing the grader child's process
#: group, for the child to be reaped.
_KILL_WAIT_S = 10.0

_ZERO_DIGEST = "0" * 64

_MAX_DOCUMENTATION_BYTES = 4 * 1024 * 1024

#: Portable component-length limit enforced before filesystem writes.
_NAME_MAX_BYTES = 255

#: Path-attributable write errors; all other OS failures remain harness faults.
_POLICY_WRITE_ERRNOS: frozenset[int] = frozenset({errno.ENAMETOOLONG, errno.EINVAL})

#: Workspace failures the declarative policy cannot create. They indicate
#: harness I/O or seal corruption and therefore produce no training label.
_HARNESS_ONLY_WORKSPACE_CODES: frozenset[WorkspaceErrorCode] = frozenset(
    {
        WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
        WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
        WorkspaceErrorCode.WORKSPACE_SYMLINK,
        WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
    }
)

#: Replay-only failures after the artifact has passed policy and been sealed.
#: They reflect harness-owned state or I/O and must not produce a zero label.
_HARNESS_ONLY_REPLAY_CODES: frozenset[WorkspaceErrorCode] = (
    _HARNESS_ONLY_WORKSPACE_CODES
    | frozenset(
        {
            WorkspaceErrorCode.SUBMISSION_INVALID,
            WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
            WorkspaceErrorCode.TASK_ID_MISMATCH,
        }
    )
)


@dataclass(frozen=True)
class Observation:
    """Public task and writable paths exposed at reset; no private evidence."""

    task_id: str
    attempt_root: Path
    task_dir: Path
    elt_dir: Path
    documentation: str
    public_files: tuple[str, ...]


@dataclass(frozen=True)
class StepResult:
    """Terminal result with either a training signal or an unlabelled harness fault."""

    observation: Observation
    signal: WorkspaceTrainingSignal
    result: WorkspaceScoreResult | None
    done: bool = True
    harness_fault: str = ""
    harness_fault_code: str = ""
    seal_sha256: str = ""
    sealed_dir: Path | None = None


def unlabelled_signal(
    *,
    artifact_sha256: str = _ZERO_DIGEST,
    scorer_version: str = WORKSPACE_SCORER_VERSION,
    w_t: float = DEFAULT_W_T,
) -> WorkspaceTrainingSignal:
    """The signal of a rollout the grader could not measure: ``label_valid``
    false, reward ``None``, no phase attributed, no diagnostics."""
    return WorkspaceTrainingSignal(
        scorer_version=scorer_version,
        artifact_sha256=artifact_sha256,
        label_valid=False,
        el_pass=False,
        policy_violation=False,
        r_el=0.0,
        r_t=0.0,
        w_t=float(w_t),
        reward=None,
        first_failed_phase="none",
        populations={},
    )


def supervised_score(
    scorer: Callable[..., WorkspaceScoreResult],
    *args: Any,
    deadline_s: float,
    name: str = GRADER_NAME,
    **kwargs: Any,
) -> tuple[WorkspaceScoreResult | None, SessionFault | None]:
    """Run an injected scorer double in-process under a harness deadline.

    Return a result, ``ToolDeadlineExceeded``, or class-only ``ToolHarnessFault``.
    Late answers never become labels. Real grading uses ``score_in_child``.
    """
    deadline = float(deadline_s)
    if not deadline > 0.0:
        raise ValueError("deadline_s must be > 0")
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = scorer(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — classified, never re-raised raw
            outcome["error"] = exc

    worker = threading.Thread(target=run, name=f"{name}-grader", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        return None, ToolDeadlineExceeded(name, deadline_s=deadline)
    if "error" in outcome:
        return None, ToolHarnessFault.from_exception(name, outcome["error"])
    result = outcome.get("result")
    if not isinstance(result, WorkspaceScoreResult):
        return None, ToolHarnessFault(name, code="non_result")
    return result, None


# ---------------------------------------------------------------------------
# The env-owned grader child (finding 2-3): the REAL score_workspace runs in
# its own session and process group, and the deadline KILLS it.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GraderChildSpec:
    """Harness paths and seal digest needed by the isolated grader child."""

    release_dir: Path
    task_id: str
    verify_release: bool
    sealed_dir: Path
    seal_sha256: str
    attempts_root: Path
    runtime_config: DbtRuntimeConfig
    dbt_limits: DbtRunnerLimits | None
    entry: str = GRADER_ENTRY

    def to_json(self) -> str:
        return json.dumps(
            {
                "release_dir": os.path.abspath(self.release_dir),
                "task_id": self.task_id,
                "verify_release": bool(self.verify_release),
                "sealed_dir": os.path.abspath(self.sealed_dir),
                "seal_sha256": self.seal_sha256,
                "attempts_root": os.path.abspath(self.attempts_root),
                "runtime_config": {
                    "python": os.path.abspath(self.runtime_config.python),
                    "manifest": os.path.abspath(self.runtime_config.manifest),
                    "verification_scope": self.runtime_config.verification_scope.value,
                },
                "dbt_limits": (
                    None if self.dbt_limits is None else self.dbt_limits.model_dump(mode="json")
                ),
                "entry": self.entry,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "GraderChildSpec":
        from elt_taskgen.training.dbt_runner import DbtRuntimeVerificationScope

        raw = json.loads(text)
        config = raw["runtime_config"]
        limits = raw.get("dbt_limits")
        return cls(
            release_dir=Path(raw["release_dir"]),
            task_id=str(raw["task_id"]),
            verify_release=bool(raw["verify_release"]),
            sealed_dir=Path(raw["sealed_dir"]),
            seal_sha256=str(raw["seal_sha256"]),
            attempts_root=Path(raw["attempts_root"]),
            runtime_config=DbtRuntimeConfig(
                python=Path(config["python"]),
                manifest=Path(config["manifest"]),
                verification_scope=DbtRuntimeVerificationScope(config["verification_scope"]),
            ),
            dbt_limits=None if limits is None else DbtRunnerLimits.model_validate(limits),
            entry=str(raw.get("entry") or GRADER_ENTRY),
        )


def _load_entry(entry: str) -> Callable[..., Any]:
    module_name, _, attribute = str(entry).partition(":")
    if not module_name or not attribute:
        raise ValueError("a grader entry is 'module:function'")
    return getattr(importlib.import_module(module_name), attribute)


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` so the supervisor can never read a half-written file: a
    killed child leaves at most a stray temporary, never a truncated result."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _grader_child_main(argv: Sequence[str] | None = None) -> int:
    """Reload, authenticate, score, and write child results without printing.

    Fault files contain exception class only; success exits zero.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 2
    spec_path, result_path, fault_path = (Path(item) for item in args)
    try:
        spec = GraderChildSpec.from_json(spec_path.read_text(encoding="utf-8"))
        grader = _load_entry(spec.entry)
        package = load_workspace_package(
            spec.release_dir, spec.task_id, verify=spec.verify_release
        )
        result = grader(
            package,
            spec.sealed_dir,
            attempts_root=spec.attempts_root,
            runtime_config=spec.runtime_config,
            dbt_limits=spec.dbt_limits,
            expected_seal_sha256=spec.seal_sha256,
        )
        if not isinstance(result, WorkspaceScoreResult):
            _write_atomic(fault_path, json.dumps({"code": "non_result", "cause_type": ""}))
            return 1
        _write_atomic(result_path, result.model_dump_json())
        return 0
    except BaseException as exc:  # noqa: BLE001 — reported by class, never re-raised raw
        try:
            _write_atomic(
                fault_path,
                json.dumps({"code": "harness_exception", "cause_type": type(exc).__name__}),
            )
        except BaseException:  # noqa: BLE001
            pass
        return 1


_CHILD_BOOTSTRAP = (
    "import sys; from elt_taskgen.training.env import _grader_child_main; "
    "sys.exit(_grader_child_main())"
)

#: The only codes a grader child reports in its fault file.
_GRADER_CHILD_FAULT_CODES: frozenset[str] = frozenset({"harness_exception", "non_result"})


def _descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Return transitive child PIDs from ``ps``, or empty when unavailable."""
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    pending = [int(root_pid)]
    while pending:
        current = pending.pop()
        for child in children.get(current, ()):
            if child not in found:
                found.append(child)
                pending.append(child)
    return tuple(found)


def _kill_grader_tree(process: subprocess.Popen[Any]) -> None:
    """Kill and wait for the grader group and any detached descendants."""
    descendants = _descendant_pids(process.pid)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    for pid in descendants:
        for kill in (lambda p: os.killpg(p, signal.SIGKILL), lambda p: os.kill(p, signal.SIGKILL)):
            try:
                kill(pid)
            except OSError:
                pass
    try:
        process.wait(timeout=_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def _child_environment() -> dict[str, str]:
    """The child's environment: the parent's, with the parent's import path
    handed down so the package and a test's entry resolve identically."""
    env = dict(os.environ)
    inherited = [entry for entry in sys.path if entry]
    existing = env.get("PYTHONPATH", "")
    if existing:
        inherited.extend(part for part in existing.split(os.pathsep) if part)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(inherited))
    return env


def score_in_child(
    spec: GraderChildSpec,
    *,
    deadline_s: float,
    scratch_dir: Path,
    name: str = GRADER_NAME,
) -> tuple[WorkspaceScoreResult | None, SessionFault | None]:
    """Run the real grader in a killable, authenticated child process.

    Deadlines return ``ToolDeadlineExceeded``; reported faults expose class only;
    unreported death returns ``SandboxFault``. Child output never becomes a label.
    """
    deadline = float(deadline_s)
    if not deadline > 0.0:
        raise ValueError("deadline_s must be > 0")
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    spec_path = scratch / "grader_spec.json"
    result_path = scratch / "grader_result.json"
    fault_path = scratch / "grader_fault.json"
    for stale in (result_path, fault_path):
        if stale.exists():
            stale.unlink()
    spec_path.write_text(spec.to_json(), encoding="utf-8")
    with (scratch / "grader.stderr").open("wb") as stderr:
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", _CHILD_BOOTSTRAP, str(spec_path), str(result_path), str(fault_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=_child_environment(),
                start_new_session=True,
            )
        except OSError:
            return None, SandboxFault(
                "the grader child could not be started", code="grader_child_spawn"
            )
        try:
            process.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            _kill_grader_tree(process)
            return None, ToolDeadlineExceeded(name, deadline_s=deadline)
    if result_path.is_file():
        try:
            return WorkspaceScoreResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            ), None
        except ValueError:
            return None, ToolHarnessFault(name, code="non_result")
    if fault_path.is_file():
        code, cause = "harness_exception", ""
        try:
            reported = json.loads(fault_path.read_text(encoding="utf-8"))
            code = str(reported.get("code") or code)
            cause = str(reported.get("cause_type") or "")
        except (ValueError, AttributeError):
            pass
        # The child wrote one of two fixed codes and a class NAME; anything
        # else (a damaged file) is the generic code and no class.
        if code not in _GRADER_CHILD_FAULT_CODES:
            code = "harness_exception"
        if not cause.isidentifier():
            cause = ""
        return None, ToolHarnessFault(name, code=code, cause_type=cause)
    return None, SandboxFault(
        "the grader child exited without reporting a result", code="grader_child_died"
    )


def _lifecycle_result(
    package: WorkspacePackage,
    *,
    code: WorkspaceErrorCode,
    artifact_sha256: str = _ZERO_DIGEST,
) -> WorkspaceScoreResult:
    """A classified result for an artifact the seal refused, built from the
    public models only: a label-eligible class scores 0.0, any other class
    is no label."""
    classification = WORKSPACE_ERROR_CLASS_BY_CODE[code]
    return WorkspaceScoreResult(
        release_id=package.manifest.release_id,
        task_id=package.task_id,
        task_content_hash=package.task.content_hash(),
        artifact_sha256=artifact_sha256,
        valid_submission=False,
        reward=0.0 if classification.label_eligible else None,
        failure=WorkspaceFailure(classification=classification, error_code=code),
    )


def _normalize_artifact(artifact: Mapping[str, Any]) -> dict[str, bytes]:
    """Normalize an artifact to ``elt/``-confined POSIX paths and bytes.

    Reject absolute paths, traversal, backslashes, the bare root, ancestor/file
    conflicts, control characters, oversized components, and non-text/byte
    values before writing. Normalize names to NFC and reject case-folded or
    normalized collisions for host-independent seals.
    """
    if not isinstance(artifact, Mapping):
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.SUBMISSION_INVALID,
            "an artifact is a mapping of elt/ paths to bytes",
        )
    normalized: dict[str, bytes] = {}
    seen_casefold: dict[str, str] = {}
    for key, value in artifact.items():
        if not isinstance(key, str) or not key:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, "artifact path must be a non-empty string"
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in key):
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                "artifact path contains a control character",
            )
        if isinstance(value, str):
            try:
                data = value.encode("utf-8")
            except UnicodeEncodeError as error:
                # A lone surrogate is the policy's own bytes: a measured
                # label, never an unclassified crash (finding 2-2, residual).
                raise WorkspaceLifecycleError(
                    WorkspaceErrorCode.SUBMISSION_INVALID,
                    "artifact text is not UTF-8 encodable",
                ) from error
        elif isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
        else:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.SUBMISSION_INVALID,
                "artifact values must be bytes or text",
            )
        path = key if key == "elt" or key.startswith("elt/") else f"elt/{key}"
        path = unicodedata.normalize("NFC", path)
        if path == "elt":
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                "artifact path is the elt/ root, not a file",
            )
        try:
            WorkspaceArtifactFile(
                path=path,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        except ValueError as error:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                "artifact path is not confined beneath elt/",
            ) from error
        if any(len(part.encode("utf-8")) > _NAME_MAX_BYTES for part in path.split("/")):
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                "an artifact path component is longer than NAME_MAX",
            )
        folded = unicodedata.normalize("NFC", path.casefold())
        if folded in seen_casefold:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_CASE_COLLISION,
                "artifact names one path twice (case- or normalization-insensitively)",
            )
        seen_casefold[folded] = path
        normalized[path] = data
    _reject_prefix_conflicts(seen_casefold)
    return normalized


def _reject_prefix_conflicts(paths_by_casefold: Mapping[str, str]) -> None:
    """Reject case-folded file paths that are proper ancestors of other paths."""
    folded = set(paths_by_casefold)
    for path in folded:
        prefix = ""
        parts = path.split("/")
        for part in parts[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix in folded:
                raise WorkspaceLifecycleError(
                    WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                    "one artifact path is a directory of another",
                )


def _write_artifact(attempt: AttemptWorkspace, files: Mapping[str, bytes]) -> None:
    """Write normalized files beneath ``elt/``.

    Candidate-caused path errors become ``WORKSPACE_PATH_ESCAPE``; other OS
    errors propagate as harness faults.
    """
    for path, data in files.items():
        relative = PurePosixPath(path).relative_to("elt")
        target = attempt.elt_dir.joinpath(*relative.parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except ValueError as exc:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                "artifact path is not writable as a file name",
            ) from exc
        except OSError as exc:
            if exc.errno in _POLICY_WRITE_ERRNOS:
                raise WorkspaceLifecycleError(
                    WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                    "artifact path is not writable as a file name",
                ) from exc
            raise


def _public_listing(task_dir: Path) -> tuple[str, ...]:
    listing: list[str] = []
    for directory, _dirs, files in os.walk(task_dir, followlinks=False):
        for name in files:
            listing.append(Path(directory, name).relative_to(task_dir).as_posix())
    return tuple(sorted(listing))


def _read_documentation(attempt: AttemptWorkspace, package: WorkspacePackage) -> str:
    relative = package.documentation_path.relative_to(package.public_dir)
    path = attempt.task_dir.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        return ""
    data = path.read_bytes()[:_MAX_DOCUMENTATION_BYTES]
    return data.decode("utf-8", errors="replace")


def _remove_owned_tree(root: Path) -> None:
    """Remove a tree this module created (installed attempts are read-only)."""
    if not root.exists():
        return
    for directory, child_dirs, _files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if not current.is_symlink():
            current.chmod(0o700)
        for name in child_dirs:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(root)


@dataclass
class _Episode:
    task_id: str
    package: WorkspacePackage
    root: Path
    attempt: AttemptWorkspace
    observation: Observation
    stepped: bool = False


class DeclarativeEltEnv:
    """Single-step environment over a verified combined release.

    ``reset`` installs a fresh public attempt; one ``step`` writes, seals,
    scores the complete hidden set, and projects the result. Real scoring uses
    a killable child; injected test doubles run through ``supervised_score``.
    Private infeasibility canaries never enter observations.
    """

    def __init__(
        self,
        release_dir: Path,
        *,
        attempts_root: Path,
        runtime_config: DbtRuntimeConfig,
        dbt_limits: DbtRunnerLimits | None = None,
        grader_deadline_s: float | None = None,
        w_t: float = DEFAULT_W_T,
        scorer: Callable[..., WorkspaceScoreResult] = score_workspace,
        grader_entry: str = GRADER_ENTRY,
        infeasible_tasks: Iterable[str] = (),
        verify_release: bool = True,
    ) -> None:
        self.release_dir = Path(release_dir)
        self.attempts_root = Path(attempts_root)
        self.runtime_config = runtime_config
        self.dbt_limits = dbt_limits
        # Default deadline covers candidate caps, trusted work, and slack.
        self.grader_deadline_s = (
            float(grader_deadline_s)
            if grader_deadline_s is not None
            else default_grader_deadline_s(dbt_limits)
        )
        if not self.grader_deadline_s > 0.0:
            raise ValueError("grader_deadline_s must be > 0")
        self.w_t = float(w_t)
        self._scorer = scorer
        self._grader_entry = str(grader_entry)
        self._infeasible = frozenset(str(item) for item in infeasible_tasks)
        self._verify_release = bool(verify_release)
        self._packages: dict[str, WorkspacePackage] = {}
        self._episode: _Episode | None = None
        self._episodes = 0

    # -- package access --------------------------------------------------

    def package(self, task_id: str) -> WorkspacePackage:
        """The verified private package of ``task_id`` (loaded once)."""
        cached = self._packages.get(task_id)
        if cached is None:
            cached = load_workspace_package(
                self.release_dir, task_id, verify=self._verify_release
            )
            self._packages[task_id] = cached
        return cached

    @property
    def observation(self) -> Observation | None:
        return self._episode.observation if self._episode is not None else None

    # -- the two moves ------------------------------------------------------

    def reset(self, task_id: str) -> Observation:
        """Install a fresh public attempt and return its observation."""
        package = self.package(task_id)
        episodes = self.attempts_root / "episodes"
        episodes.mkdir(parents=True, exist_ok=True)
        self._episodes += 1
        root = Path(
            tempfile.mkdtemp(prefix=f"{self._episodes:04d}-", dir=episodes)
        )
        attempt = install_workspace(package, root / "attempt")
        observation = Observation(
            task_id=package.task_id,
            attempt_root=attempt.root,
            task_dir=attempt.task_dir,
            elt_dir=attempt.elt_dir,
            documentation=_read_documentation(attempt, package),
            public_files=_public_listing(attempt.task_dir),
        )
        self._episode = _Episode(
            task_id=package.task_id,
            package=package,
            root=root,
            attempt=attempt,
            observation=observation,
        )
        return observation

    def step(
        self,
        artifact: Mapping[str, Any],
        *,
        aborted_infeasible: bool = False,
    ) -> StepResult:
        """Write, seal, score, and project one terminal artifact.

        Harness-recorded infeasibility earns credit only for planted canaries.
        """
        episode = self._episode
        if episode is None:
            raise RuntimeError("step() before reset()")
        if episode.stepped:
            raise RuntimeError("the declarative environment is single-step; reset() first")
        episode.stepped = True
        package = episode.package
        infeasible = episode.task_id in self._infeasible

        def project(result: WorkspaceScoreResult) -> WorkspaceTrainingSignal:
            return training_signal(
                result,
                w_t=self.w_t,
                infeasible_task=infeasible,
                aborted_infeasible=bool(aborted_infeasible),
            )

        def policy_step(result: WorkspaceScoreResult, *, sealed: SealedWorkspace | None = None) -> StepResult:
            return StepResult(
                observation=episode.observation,
                signal=project(result),
                result=result,
                seal_sha256=sealed.seal_sha256 if sealed is not None else "",
                sealed_dir=sealed.root if sealed is not None else None,
            )

        def harness_step(fault: SessionFault, *, sealed: SealedWorkspace | None = None) -> StepResult:
            digest = (
                sealed.submission.artifact_sha256 if sealed is not None else _ZERO_DIGEST
            )
            return StepResult(
                observation=episode.observation,
                signal=unlabelled_signal(artifact_sha256=digest, w_t=self.w_t),
                result=None,
                harness_fault=type(fault).__name__,
                harness_fault_code=str(getattr(fault, "code", "") or ""),
                seal_sha256=sealed.seal_sha256 if sealed is not None else "",
                sealed_dir=sealed.root if sealed is not None else None,
            )

        # Normalize and write the confined artifact. Policy refusals are
        # measured; unrelated write errors are unlabeled harness failures.
        try:
            files = _normalize_artifact(artifact)
            _write_artifact(episode.attempt, files)
        except WorkspaceLifecycleError as error:
            return policy_step(_lifecycle_result(package, code=error.code))
        except OSError:
            return harness_step(
                SandboxFault("declarative env could not write the artifact", code="artifact_write_io")
            )

        # Seal the artifact. Content failures are measured; public-tree I/O
        # failures are harness faults and produce no label.
        try:
            sealed: SealedWorkspace = seal_workspace(
                episode.attempt, package, episode.root / "sealed"
            )
        except WorkspaceLifecycleError as error:
            if (
                isinstance(error.__cause__, OSError)
                or error.code in _HARNESS_ONLY_WORKSPACE_CODES
            ):
                return harness_step(
                    SandboxFault(
                        "declarative env seal hit a transient I/O fault",
                        code="seal_io",
                    )
                )
            return policy_step(_lifecycle_result(package, code=error.code))

        # Score real graders in a killable child; test doubles use the thread path.
        if self._scorer is score_workspace:
            result, fault = score_in_child(
                GraderChildSpec(
                    release_dir=self.release_dir,
                    task_id=package.task_id,
                    verify_release=self._verify_release,
                    sealed_dir=sealed.root,
                    seal_sha256=sealed.seal_sha256,
                    attempts_root=episode.root / "grader",
                    runtime_config=self.runtime_config,
                    dbt_limits=self.dbt_limits,
                    entry=self._grader_entry,
                ),
                deadline_s=self.grader_deadline_s,
                scratch_dir=episode.root / "supervisor",
            )
        else:
            result, fault = supervised_score(
                self._scorer,
                package,
                sealed,
                attempts_root=episode.root / "grader",
                runtime_config=self.runtime_config,
                dbt_limits=self.dbt_limits,
                expected_seal_sha256=sealed.seal_sha256,
                deadline_s=self.grader_deadline_s,
            )
        if fault is not None or result is None:
            fault = fault or ToolHarnessFault(GRADER_NAME, code="non_result")
            return harness_step(fault, sealed=sealed)
        # Replay failures impossible after sealing are harness faults.
        # Candidate-caused policy failures keep their labels.
        failure = result.failure
        if failure is not None and failure.error_code in _HARNESS_ONLY_REPLAY_CODES:
            return harness_step(
                SandboxFault(
                    "grader re-detected a public-tree fault the policy cannot cause",
                    code="grader_workspace_io",
                ),
                sealed=sealed,
            )
        return policy_step(result, sealed=sealed)

    def close(self) -> None:
        """Remove the CURRENT episode's directory (attempt, seal, grader
        scratch) — the exact tree ``reset`` created, nothing else."""
        episode = self._episode
        self._episode = None
        if episode is not None:
            _remove_owned_tree(episode.root)


__all__ = [
    "DEFAULT_GRADER_DEADLINE_S",
    "DEFAULT_GRADER_SLACK_S",
    "DEFAULT_TRUSTED_PHASE_BUDGET_S",
    "GRADER_ENTRY",
    "GRADER_NAME",
    "DeclarativeEltEnv",
    "GraderChildSpec",
    "default_grader_deadline_s",
    "Observation",
    "StepResult",
    "drop_group_if_unlabelled",  # the rollout adapter's rule (training/signal.py)
    "score_in_child",
    "supervised_score",
    "unlabelled_signal",
]

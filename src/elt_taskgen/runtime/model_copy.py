"""Separate credential-free model work from credentialed replay.

The harness creates the replay copy only after the model exits. The grader lane
allows harness commands and rejects agent commands. This module does not start
processes or read credential values.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from elt_taskgen.destinations import (
    Destination,
    DestinationContract,
    destination_contract,
    destination_from_config,
    normalize_destination,
)
from elt_taskgen.export.eltbench import assert_public_runtime_shape
from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.review.tools.credential_sweep import (
    NAME_RULES,
    CredentialFinding,
    format_findings,
    sweep_credential_shaped_files,
)
from elt_taskgen.runtime.install import install_task
from elt_taskgen.runtime.process import (
    DOCKER_LANE_NONE,
    DOCKER_LANE_PROXY_BRIDGE,
    CommandResult,
    Runner,
)

__all__ = [
    "AGENT_ARGV_MARKERS",
    "AttemptCopies",
    "CANDIDATE_TREE_DOTENV_RULE",
    "CLEANUP_RECEIPT_SCHEMA_VERSION",
    "CleanupReceipt",
    "DOCKER_ALLOWED_SUBCOMMANDS",
    "DOCKER_REFUSED_FLAGS",
    "GRADER_LANE_PROGRAMS",
    "INTERPRETER_ARGV_TOKENS",
    "GraderLaneRunner",
    "LANE_CLOUD",
    "LANE_LOCAL",
    "MODEL_COPY_DIRNAME",
    "ModelCopyError",
    "PrincipalRevocationError",
    "REPLAY_COPY_DIRNAME",
    "MAX_CANDIDATE_TREE_ENTRIES",
    "ReplayCredentials",
    "ScopedPrincipal",
    "assert_candidate_tree_is_image_safe",
    "assert_model_copy_is_credential_free",
    "assert_docker_argv_is_replay_shaped",
    "assert_no_agent_process",
    "assert_placeholder_credential",
    "candidate_tree_dotenv_paths",
    "docker_lane_for",
    "install_task_for_model",
    "live_credential_findings",
    "placeholder_credential",
    "publish_cleanup_receipt",
    "recompute_receipt_digest",
    "seal_cleanup_receipt",
    "verify_cleanup_receipt",
]


#: Ordinary RLVR rollouts: DuckDB only, no cloud endpoint at all (C2, C8).
LANE_LOCAL = "duckdb"
#: Certification lane with a real warehouse and attempt-scoped principal.
LANE_CLOUD = "cloud"
LANES: tuple[str, ...] = (LANE_LOCAL, LANE_CLOUD)

#: The Docker network lane each attempt lane runs under. Reused from
#: ``runtime/process.py`` so there is exactly one spelling in the tree.
_DOCKER_LANE_BY_LANE: dict[str, str] = {
    LANE_LOCAL: DOCKER_LANE_NONE,
    LANE_CLOUD: DOCKER_LANE_PROXY_BRIDGE,
}

#: The two directories of one attempt. ``task/`` is the only one a model ever
#: sees; ``replay/`` is created after the model is gone and never handed over.
MODEL_COPY_DIRNAME = "task"
REPLAY_COPY_DIRNAME = "replay"

#: Placeholder recognized by the credential scanner during installation.
_SENTINEL = "placeholder"

#: Required destination fields used to build placeholder credentials.
_SENTINEL_DESTINATION_FIELDS: dict[Destination, tuple[str, ...]] = {
    Destination.SNOWFLAKE: ("account", "user", "password", "role", "warehouse"),
    Destination.DATABRICKS: (
        "hostname",
        "http_path",
        "database",
        "client_id",
        "secret",
    ),
    Destination.REDSHIFT: (
        "host",
        "database",
        "username",
        "password",
        "s3_bucket_name",
        "s3_bucket_region",
        "access_key_id",
        "secret_access_key",
    ),
}

#: Programs allowed in replay containers. Full argv validation keeps Docker
#: replay-shaped and forbids interpreters or agent processes.
GRADER_LANE_PROGRAMS: frozenset[str] = frozenset({"dbt", "docker", "terraform"})

#: Case-insensitive markers for model or agent commands.
AGENT_ARGV_MARKERS: tuple[str, ...] = (
    "aider",
    "anthropic",
    "claude",
    "codex",
    "elt_taskgen.review",
    "openai",
    "run_bounded_session",
)

#: Exact interpreter and shell tokens rejected anywhere in an argument list.
INTERPRETER_ARGV_TOKENS: frozenset[str] = frozenset(
    {
        "ash",
        "bash",
        "bun",
        "csh",
        "dash",
        "deno",
        "fish",
        "ksh",
        "node",
        "nodejs",
        "perl",
        "php",
        "pwsh",
        "python",
        "python2",
        "python3",
        "ruby",
        "sh",
        "tclsh",
        "uv",
        "uvx",
        "zsh",
    }
)

#: Docker options that can alter the pinned replay environment.
DOCKER_REFUSED_FLAGS: tuple[str, ...] = (
    "--entrypoint",
    "--privileged",
    "--cap-add",
    "--security-opt",
    "--pid",
    "--userns",
    "--device",
    "-v",
    "--volume",
    "--mount",
)
#: ``docker`` subcommands the lane permits. Everything else — `exec`, `cp`,
#: `commit`, `build` — is refused.
DOCKER_ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset({"run", "image", "pull", "version", "info"})

#: Shared credential-scan rule for dotenv file names.
CANDIDATE_TREE_DOTENV_RULE = "dotenv"

#: Bound on the candidate-tree walk: an unbounded tree is refused, not scanned.
MAX_CANDIDATE_TREE_ENTRIES = 200_000

#: Allowed format for attempt and principal identifiers.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")

CLEANUP_RECEIPT_SCHEMA_VERSION = "1.0"


class ModelCopyError(ValueError):
    """A model-facing copy, a replay copy or a grader command was refused."""


class PrincipalRevocationError(ModelCopyError):
    """Cleanup could not revoke the attempt-scoped principal.

    Loud on purpose: an unrevoked principal outlives its attempt. The receipt
    built before the failure is carried on ``.receipt`` so the caller can
    still record what cleanup did manage to do.
    """

    def __init__(self, message: str, *, receipt: "CleanupReceipt") -> None:
        super().__init__(message)
        self.receipt = receipt


# ---------------------------------------------------------------------------
# Placeholder credential shapes
# ---------------------------------------------------------------------------


def placeholder_credential(public_task_dir: Path) -> dict[str, Any]:
    """Return the frozen public bundle's PLACEHOLDER credential document.

    The exporter writes only the destination's template into a public bundle
    and ``assert_public_runtime_shape`` refuses anything else, so this is the
    authority on the placeholder shape without importing a private name from
    ``export/eltbench.py``. The document is validated before it is returned.
    """
    source = Path(public_task_dir)
    contract = destination_contract(_public_destination(source))
    payload = _read_credential_document(source / contract.credential_filename)
    assert_placeholder_credential(payload, label=contract.credential_filename)
    return payload


def assert_placeholder_credential(
    payload: Mapping[str, Any], *, label: str = "credential file"
) -> None:
    """Require empty string credentials except for approved template fields.

    Errors include field names but never values.
    """
    if not isinstance(payload, Mapping):
        raise ModelCopyError(f"{label} is not a JSON object")
    populated: list[str] = []
    malformed: list[str] = []
    for key, value in payload.items():
        if not isinstance(key, str):
            malformed.append(str(key))
            continue
        if isinstance(value, str):
            if value != "":
                populated.append(key)
        elif isinstance(value, bool) or not isinstance(value, int):
            malformed.append(key)
    if malformed:
        raise ModelCopyError(
            f"{label} has values that are not a placeholder shape: "
            f"{sorted(malformed)}"
        )
    if populated:
        raise ModelCopyError(
            f"{label} carries populated credential fields {sorted(populated)}; "
            "the model-facing copy must carry only empty placeholders"
        )


def live_credential_findings(
    root: Path, *, include_policy_tree: bool = True
) -> list[CredentialFinding]:
    """Find credential-shaped files that contain non-placeholder values.

    Exclude ``elt/`` when checking only the harness-owned surface.
    """
    def prune_policy_tree(relative: Path) -> bool:
        return relative.parts[:1] == ("elt",)

    return sweep_credential_shaped_files(
        Path(root),
        live_only=True,
        exclude=None if include_policy_tree else prune_policy_tree,
    )


def assert_model_copy_is_credential_free(
    root: Path, *, include_policy_tree: bool = True
) -> None:
    """Require placeholder credentials, no live values, and owner-only access."""
    tree = Path(root)
    if not tree.is_dir():
        raise ModelCopyError("model-facing copy does not exist")
    contract = destination_contract(_public_destination(tree))
    payload = _read_credential_document(tree / contract.credential_filename)
    assert_placeholder_credential(payload, label=contract.credential_filename)
    findings = live_credential_findings(tree, include_policy_tree=include_policy_tree)
    if findings:
        raise ModelCopyError(
            "model-facing copy carries live credential-shaped files:\n"
            + format_findings(findings)
        )
    shared = _group_or_world_readable(
        tree, include_policy_tree=include_policy_tree
    )
    if shared:
        raise ModelCopyError(
            f"model-facing copy is not owner-private: {shared[:8]}"
        )


# ---------------------------------------------------------------------------
# The attempt-scoped principal and the cleanup receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopedPrincipal:
    """Name-only record for an attempt-scoped warehouse principal.

    :meth:`AttemptCopies.cleanup` performs teardown through an injected hook.
    This record cannot hold secrets; the hook resolves them operator-side.
    """

    principal_id: str
    destination: Destination
    attempt_id: str = ""

    def __post_init__(self) -> None:
        _assert_identifier(self.principal_id, label="principal_id")
        if self.attempt_id:
            _assert_identifier(self.attempt_id, label="attempt_id")
        normalize_destination(self.destination)


@dataclass(frozen=True)
class ReplayCredentials:
    """The LIVE values for the harness-owned replay copy.

    Constructed at activation time, after the model is gone, and dropped as
    soon as ``install_task`` has written the replay copy — an
    :class:`AttemptCopies` never stores one.
    """

    airbyte: Mapping[str, object]
    destination: Mapping[str, object]
    custom_api_definition_id: str | None = None
    airbyte_server_url: str | None = None


@dataclass(frozen=True)
class CleanupReceipt:
    """What cleanup did, sealed the way ``export/certification.py`` seals an
    attestation: the digest is taken over the canonical JSON of the record
    with the digest field blanked, so any later edit is detectable."""

    schema_version: str
    attempt_id: str
    lane: str
    docker_lane: str
    destination: str
    principal_id: str
    principal_revoked: bool
    replay_installed: bool
    replay_removed: bool
    model_removed: bool
    #: Measured credential-shaped files remaining when the receipt is created.
    #: Includes the model copy and any retained replay tree.
    live_credential_files: int
    #: Did `release_model()` run before this receipt was built? A receipt
    #: built without it asserts nothing about the model copy, and says so.
    model_released: bool
    #: Credential-shaped files the POLICY authored under ``elt/`` (reported,
    #: never a reason to refuse cleanup).
    policy_credential_findings: int
    grader_commands: int
    #: Forwarded commands that match agent or interpreter patterns.
    agent_process_count: int
    model_call_count: int
    receipt_digest: str = ""


def _receipt_digest(receipt: CleanupReceipt) -> str:
    payload = asdict(receipt)
    payload["receipt_digest"] = ""
    return sha256_hex(canonical_json(payload))


def seal_cleanup_receipt(receipt: CleanupReceipt) -> CleanupReceipt:
    """Checksum-bind the receipt (``seal_attestation``'s discipline)."""
    return replace(receipt, receipt_digest=_receipt_digest(receipt))


def recompute_receipt_digest(receipt: CleanupReceipt) -> str:
    """Re-derive the seal for verification."""
    return _receipt_digest(receipt)


def verify_cleanup_receipt(
    receipt: CleanupReceipt | Mapping[str, object],
    *,
    attempt_id: str | None = None,
    destination: Destination | str | None = None,
    require_complete: bool = False,
    expected_grader_commands: int | None = None,
) -> CleanupReceipt:
    """Parse and verify an identity-bound cleanup receipt.

    Complete cloud cleanup requires removed credential copies, a revoked
    principal, no replay-lane agent process, and the expected grader commands.
    This verifies the receipt, not the external cloud action.
    """

    if isinstance(receipt, CleanupReceipt):
        parsed = receipt
    elif isinstance(receipt, Mapping):
        expected_fields = set(CleanupReceipt.__dataclass_fields__)
        if set(receipt) != expected_fields:
            missing = sorted(expected_fields - set(receipt))
            extra = sorted(set(receipt) - expected_fields)
            raise ModelCopyError(
                "cleanup receipt has the wrong field roster "
                f"(missing={missing}, extra={extra})"
            )
        try:
            parsed = CleanupReceipt(**dict(receipt))
        except TypeError as exc:
            raise ModelCopyError("cleanup receipt is malformed") from exc
    else:
        raise ModelCopyError("cleanup receipt must be a record")

    text_fields = (
        "schema_version",
        "attempt_id",
        "lane",
        "docker_lane",
        "destination",
        "principal_id",
        "receipt_digest",
    )
    for field_name in text_fields:
        value = getattr(parsed, field_name)
        if not isinstance(value, str):
            raise ModelCopyError(f"cleanup receipt {field_name} must be text")
    bool_fields = (
        "principal_revoked",
        "replay_installed",
        "replay_removed",
        "model_removed",
        "model_released",
    )
    for field_name in bool_fields:
        if type(getattr(parsed, field_name)) is not bool:
            raise ModelCopyError(f"cleanup receipt {field_name} must be boolean")
    count_fields = (
        "live_credential_files",
        "policy_credential_findings",
        "grader_commands",
        "agent_process_count",
        "model_call_count",
    )
    for field_name in count_fields:
        value = getattr(parsed, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelCopyError(
                f"cleanup receipt {field_name} must be a non-negative integer"
            )

    if parsed.schema_version != CLEANUP_RECEIPT_SCHEMA_VERSION:
        raise ModelCopyError(
            "cleanup receipt has an unsupported schema version"
        )
    _assert_identifier(parsed.attempt_id, label="cleanup receipt attempt_id")
    if parsed.lane not in LANES:
        raise ModelCopyError("cleanup receipt names an unknown attempt lane")
    if parsed.docker_lane != _DOCKER_LANE_BY_LANE[parsed.lane]:
        raise ModelCopyError(
            "cleanup receipt Docker lane contradicts its attempt lane"
        )
    try:
        parsed_destination = normalize_destination(parsed.destination)
    except ValueError as exc:
        raise ModelCopyError("cleanup receipt names an unknown destination") from exc
    if parsed.principal_id:
        _assert_identifier(parsed.principal_id, label="cleanup receipt principal_id")
    if not parsed.receipt_digest:
        raise ModelCopyError("cleanup receipt is unsealed")
    if parsed.receipt_digest != recompute_receipt_digest(parsed):
        raise ModelCopyError("cleanup receipt digest does not reproduce")

    if attempt_id is not None and parsed.attempt_id != str(attempt_id):
        raise ModelCopyError(
            "cleanup receipt attempt_id does not match the certification attempt"
        )
    if destination is not None and parsed_destination is not normalize_destination(
        destination
    ):
        raise ModelCopyError(
            "cleanup receipt destination does not match the certified task"
        )

    if expected_grader_commands is not None:
        if (
            isinstance(expected_grader_commands, bool)
            or not isinstance(expected_grader_commands, int)
            or expected_grader_commands <= 0
        ):
            raise ModelCopyError(
                "expected_grader_commands must be a positive integer"
            )
        if not require_complete:
            raise ModelCopyError(
                "expected_grader_commands is only valid with require_complete"
            )

    if require_complete:
        failures: list[str] = []
        if parsed.lane != LANE_CLOUD:
            failures.append("lane is not cloud")
        if not parsed.principal_id:
            failures.append("principal_id is empty")
        if not parsed.principal_revoked:
            failures.append("principal was not revoked")
        if not parsed.model_released:
            failures.append("model was not released")
        if not parsed.replay_installed:
            failures.append("replay was not installed")
        if not parsed.replay_removed:
            failures.append("replay was not removed")
        if not parsed.model_removed:
            failures.append("model copy was not removed")
        if parsed.live_credential_files:
            failures.append("live credential files remain")
        if parsed.agent_process_count:
            failures.append("an agent/interpreter process reached the grader lane")
        if parsed.model_call_count:
            failures.append("the replay lane made model calls")
        if parsed.grader_commands <= 0:
            failures.append("no grader-lane commands were recorded")
        elif (
            expected_grader_commands is not None
            and parsed.grader_commands != expected_grader_commands
        ):
            failures.append(
                "grader-lane command count "
                f"{parsed.grader_commands} does not match expected "
                f"{expected_grader_commands}"
            )
        if failures:
            raise ModelCopyError(
                "cleanup receipt is not certification-complete: "
                + "; ".join(failures)
            )
    return parsed


def publish_cleanup_receipt(path: Path | str, receipt: CleanupReceipt) -> Path:
    """Publish one verified cleanup receipt by exclusive, owner-only create.

    Accept only the in-process receipt returned by ``AttemptCopies.cleanup()``.
    Never overwrite an existing receipt or write fields outside the secret-free
    dataclass schema.
    """

    verified = verify_cleanup_receipt(receipt)
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(asdict(verified), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ModelCopyError(
            f"cleanup receipt output already exists (immutable): {target}"
        ) from None
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing cleanup receipt")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        target.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    return target


# ---------------------------------------------------------------------------
# Candidate-tree hygiene: the harness-side twin of the runner-image pin
# ---------------------------------------------------------------------------


def candidate_tree_dotenv_paths(
    root: Path | str, *, max_entries: int = MAX_CANDIDATE_TREE_ENTRIES
) -> tuple[str, ...]:
    """List dotenv-shaped files by name without reading them.

    Use the shared credential-scan rule, do not follow directory symlinks, and
    reject trees that exceed the scan bound.
    """

    base = Path(root)
    if not base.is_dir():
        return ()
    pattern = _dotenv_pattern()
    seen = 0
    found: list[str] = []
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            seen += 1
            if seen > max_entries:
                raise ModelCopyError(
                    f"candidate tree at {base} exceeds {max_entries} entries; "
                    "it was not scanned (fail closed)"
                )
            if pattern.search(name):
                found.append(_relative(Path(directory) / name, base))
    return tuple(found)


def assert_candidate_tree_is_image_safe(root: Path | str) -> None:
    """Reject candidate trees containing ``.env`` files before mounting them.

    These files can alter dbt configuration outside the sealed artifact. The
    check reports paths without reading their contents.
    """

    found = candidate_tree_dotenv_paths(root)
    if found:
        raise ModelCopyError(
            "candidate tree carries dotenv files the runner images reject: "
            f"{list(found[:8])}"
        )


# ---------------------------------------------------------------------------
# The grader / replay lane process gate
# ---------------------------------------------------------------------------


def assert_no_agent_process(argv: Sequence[str]) -> None:
    """Reject commands that start a model, agent runner, or interpreter.

    The replay lane may execute only sealed artifacts and must not expose its
    credentials to model-controlled code.
    """
    if not argv:
        raise ModelCopyError("cannot run an empty command in the grader lane")
    for index, raw in enumerate(argv):
        token = str(raw).casefold()
        for marker in AGENT_ARGV_MARKERS:
            if marker in token:
                raise ModelCopyError(
                    "the grader lane refuses an agent process: argv element "
                    f"{index} names {marker!r}"
                )
        # An interpreter is judged as a whole TOKEN (or a path ending in one),
        # so `dbt run --select nodes` is untouched while `sh -c ...` is not.
        basename = Path(token).name
        if basename in INTERPRETER_ARGV_TOKENS or token in INTERPRETER_ARGV_TOKENS:
            raise ModelCopyError(
                "the grader lane refuses an interpreter: argv element "
                f"{index} names {basename!r}"
            )
        if token == "-m" and index + 1 < len(argv):
            module = str(argv[index + 1]).casefold()
            if module.startswith("elt_taskgen.review") or "provider" in module:
                raise ModelCopyError(
                    "the grader lane refuses an agent process: "
                    f"-m {module!r}"
                )


def assert_docker_argv_is_replay_shaped(argv: Sequence[str]) -> None:
    """Allow only a plain Docker run of a pinned replay image.

    Reject other subcommands, isolation changes, host mounts, and entry-point
    overrides. Agent and interpreter checks cover the container command.
    """
    command = [str(value) for value in argv]
    if not command:
        raise ModelCopyError("cannot run an empty command in the grader lane")
    if Path(command[0]).name != "docker":
        return
    subcommand = next((token for token in command[1:] if not token.startswith("-")), "")
    if subcommand.casefold() not in DOCKER_ALLOWED_SUBCOMMANDS:
        raise ModelCopyError(
            f"the grader lane refuses `docker {subcommand or '<none>'}`; "
            f"permitted: {sorted(DOCKER_ALLOWED_SUBCOMMANDS)}"
        )
    for token in command[1:]:
        head = token.split("=", 1)[0].casefold()
        if head in DOCKER_REFUSED_FLAGS:
            raise ModelCopyError(
                f"the grader lane refuses the docker option {head!r}: it turns "
                "a pinned-image run into arbitrary execution"
            )


class GraderLaneRunner:
    """Permit only approved harness commands in the replay lane.

    Validate the program, agent and interpreter markers, and Docker shape
    before forwarding. Retain forwarded argument lists and derive the agent
    process count from them.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        lane: str = LANE_LOCAL,
        programs: frozenset[str] = GRADER_LANE_PROGRAMS,
        check_candidate_tree: bool = True,
    ) -> None:
        self._runner = runner
        self.lane = _validated_lane(lane)
        self.programs = frozenset(str(name) for name in programs)
        if not self.programs:
            raise ModelCopyError("the grader lane needs at least one program")
        self._commands: list[tuple[str, ...]] = []
        self._refused = 0
        self._check_candidate_tree = bool(check_candidate_tree)
        #: Trees already judged clean, so a lane running ten dbt commands
        #: walks each tree once.
        self._checked_trees: set[Path] = set()

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        return tuple(self._commands)

    @property
    def command_count(self) -> int:
        return len(self._commands)

    @property
    def refused_count(self) -> int:
        return self._refused

    @property
    def agent_process_count(self) -> int:
        """Count forwarded commands shaped like agent or interpreter runs."""
        forwarded = 0
        for command in self._commands:
            try:
                assert_no_agent_process(command)
            except ModelCopyError:
                forwarded += 1
        return forwarded

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin_path: Path | None = None,
    ) -> CommandResult:
        command = tuple(str(value) for value in argv)
        try:
            assert_no_agent_process(command)
            assert_docker_argv_is_replay_shaped(command)
            program = Path(command[0]).name if command else ""
            if program not in self.programs:
                raise ModelCopyError(
                    f"the grader lane refuses the program {program!r}; "
                    f"permitted: {sorted(self.programs)}"
                )
            if self._check_candidate_tree and cwd is not None:
                tree = Path(cwd)
                if tree not in self._checked_trees:
                    assert_candidate_tree_is_image_safe(tree)
                    self._checked_trees.add(tree)
        except ModelCopyError:
            self._refused += 1
            raise
        self._commands.append(command)
        return self._runner.run(argv, cwd=cwd, env=env, stdin_path=stdin_path)


# ---------------------------------------------------------------------------
# The two copies
# ---------------------------------------------------------------------------


class AttemptCopies:
    """Manage one credential-free model copy and later replay copy.

    Replay installation follows model release. Cleanup removes both copies and
    revokes the cloud principal.
    """

    def __init__(
        self,
        *,
        source: Path,
        model_dir: Path,
        replay_dir: Path,
        destination: Destination,
        contract: DestinationContract,
        lane: str,
        attempt_id: str,
        principal: ScopedPrincipal | None,
        revoke_principal: Callable[[ScopedPrincipal], None] | None,
        credentials_provider: Callable[[], ReplayCredentials] | None,
        public_shape_verified: bool,
    ) -> None:
        self._source = Path(source)
        self.model_dir = Path(model_dir)
        self.replay_dir = Path(replay_dir)
        self.destination = destination
        self.contract = contract
        self.lane = lane
        self.attempt_id = attempt_id
        self.principal = principal
        self._revoke = revoke_principal
        self._credentials_provider = credentials_provider
        self.public_shape_verified = public_shape_verified
        self._model_released = False
        self._replay_installed = False
        self._policy_findings = 0
        self._grader_runners: list[GraderLaneRunner] = []
        self._receipt: CleanupReceipt | None = None

    # -- identity -----------------------------------------------------------

    @property
    def credential_filename(self) -> str:
        return self.contract.credential_filename

    @property
    def docker_lane(self) -> str:
        return _DOCKER_LANE_BY_LANE[self.lane]

    @property
    def model_released(self) -> bool:
        return self._model_released

    @property
    def replay_installed(self) -> bool:
        return self._replay_installed

    @property
    def receipt(self) -> CleanupReceipt | None:
        return self._receipt

    # -- the one-way state machine ------------------------------------------

    def release_model(self) -> tuple[CredentialFinding, ...]:
        """Record model exit and recheck harness-owned credential files.

        Return policy-authored findings under ``elt/`` without blocking cleanup.
        """
        if self._model_released:
            raise ModelCopyError("the model copy was already released")
        assert_model_copy_is_credential_free(
            self.model_dir, include_policy_tree=False
        )
        policy = tuple(
            finding
            for finding in live_credential_findings(self.model_dir)
            if finding.path.startswith("elt/")
        )
        self._policy_findings = len(policy)
        self._model_released = True
        return policy

    def install_replay(
        self, credentials: ReplayCredentials | None = None
    ) -> Path:
        """Install the credentialed replay copy after model release.

        Resolve deferred credentials only during this call.
        """
        if not self._model_released:
            raise ModelCopyError(
                "the replay copy receives live values only after the model is "
                "gone; call release_model() first"
            )
        if self._replay_installed:
            raise ModelCopyError("the replay copy is already installed")
        resolved = credentials
        if resolved is None and self._credentials_provider is not None:
            resolved = self._credentials_provider()
        if resolved is None:
            raise ModelCopyError(
                "no replay credentials were supplied for the replay copy"
            )
        install_task(
            self._source,
            self.replay_dir,
            airbyte_credentials=resolved.airbyte,
            destination_credentials=resolved.destination,
            destination=self.destination,
            custom_api_definition_id=resolved.custom_api_definition_id,
            airbyte_server_url=resolved.airbyte_server_url,
        )
        self._replay_installed = True
        return self.replay_dir

    def grader_runner(
        self,
        runner: Runner,
        *,
        programs: frozenset[str] = GRADER_LANE_PROGRAMS,
    ) -> GraderLaneRunner:
        """A process gate for the replay/grader container on this lane."""
        gate = GraderLaneRunner(runner, lane=self.lane, programs=programs)
        self._grader_runners.append(gate)
        return gate

    def _surviving_live_credentials(self) -> int:
        """Count live credential files remaining on harness-owned surfaces."""
        total = 0
        if self.model_dir.exists():
            total += len(
                live_credential_findings(self.model_dir, include_policy_tree=False)
            )
        if self.replay_dir.exists():
            total += len(live_credential_findings(self.replay_dir))
        return total

    def cleanup(
        self, *, remove_replay: bool = True, remove_model: bool = True
    ) -> CleanupReceipt:
        """Remove attempt copies, then revoke the scoped principal.

        Successful cleanup is idempotent. Failed revocation may be retried.
        """
        if self._receipt is not None:
            return self._receipt
        replay_removed = False
        if remove_replay and self.replay_dir.exists():
            shutil.rmtree(self.replay_dir, ignore_errors=True)
            replay_removed = not self.replay_dir.exists()
        model_removed = False
        if remove_model and self.model_dir.exists():
            shutil.rmtree(self.model_dir, ignore_errors=True)
            model_removed = not self.model_dir.exists()
        receipt = seal_cleanup_receipt(
            CleanupReceipt(
                schema_version=CLEANUP_RECEIPT_SCHEMA_VERSION,
                attempt_id=self.attempt_id,
                lane=self.lane,
                docker_lane=self.docker_lane,
                destination=self.destination.value,
                principal_id=(
                    self.principal.principal_id if self.principal else ""
                ),
                principal_revoked=False,
                replay_installed=self._replay_installed,
                replay_removed=replay_removed,
                model_removed=model_removed,
                live_credential_files=self._surviving_live_credentials(),
                model_released=self._model_released,
                policy_credential_findings=self._policy_findings,
                grader_commands=sum(
                    gate.command_count for gate in self._grader_runners
                ),
                agent_process_count=sum(
                    gate.agent_process_count for gate in self._grader_runners
                ),
                model_call_count=0,
            )
        )
        if self.principal is not None:
            hook = self._revoke
            if hook is None:  # pragma: no cover - refused at construction
                raise PrincipalRevocationError(
                    "cloud-lane attempt has no revocation hook", receipt=receipt
                )
            try:
                hook(self.principal)
            except Exception as exc:  # noqa: BLE001 - never echo the cause text
                raise PrincipalRevocationError(
                    "attempt-scoped principal revocation failed "
                    f"({type(exc).__name__}); the principal may still be live",
                    receipt=receipt,
                ) from None
            receipt = seal_cleanup_receipt(
                replace(receipt, principal_revoked=True, receipt_digest="")
            )
        self._receipt = receipt
        return receipt


def install_task_for_model(
    public_task_dir: Path,
    work_dir: Path,
    *,
    destination: Destination | str | None = None,
    lane: str = LANE_LOCAL,
    attempt_id: str | None = None,
    principal: ScopedPrincipal | None = None,
    revoke_principal: Callable[[ScopedPrincipal], None] | None = None,
    credentials_provider: Callable[[], ReplayCredentials] | None = None,
    model_dirname: str = MODEL_COPY_DIRNAME,
    replay_dirname: str = REPLAY_COPY_DIRNAME,
) -> AttemptCopies:
    """Install a credential-free model copy and reserve the replay path.

    The replay copy can be created only after the model is released. Cloud
    attempts require a scoped principal and revocation hook. Local attempts
    reject both because they do not use cloud credentials.
    """
    source = Path(public_task_dir)
    work = Path(work_dir)
    lane = _validated_lane(lane)
    if lane == LANE_CLOUD:
        if principal is None or revoke_principal is None:
            raise ModelCopyError(
                "the cloud lane requires an attempt-scoped principal and a "
                "revocation hook"
            )
    elif principal is not None or revoke_principal is not None:
        raise ModelCopyError(
            f"the {LANE_LOCAL} lane has no cloud principal to scope or revoke"
        )
    for name in (model_dirname, replay_dirname):
        if not name or "/" in name or name in {".", ".."}:
            raise ModelCopyError("attempt directory names must be simple names")
    if model_dirname == replay_dirname:
        raise ModelCopyError(
            "the replay copy must be a SEPARATE directory from the model copy"
        )
    resolved_attempt_id = attempt_id if attempt_id is not None else work.name
    _assert_identifier(resolved_attempt_id, label="attempt_id")

    config = _public_config(source)
    selected = destination_from_config(config)
    if destination is not None and normalize_destination(destination) is not selected:
        raise ModelCopyError(
            "explicit destination does not match the public task config"
        )
    contract = destination_contract(selected)
    credential_path = source / contract.credential_filename
    placeholder = _read_credential_document(credential_path)
    assert_placeholder_credential(placeholder, label=contract.credential_filename)
    public_credential_bytes = credential_path.read_bytes()
    public_config_bytes = (source / "config.yaml").read_bytes()
    public_shape_verified = _public_shape_holds(source)

    model_dir = work / model_dirname
    replay_dir = work / replay_dirname
    if _exists(model_dir) or _exists(replay_dir):
        raise ModelCopyError("attempt directories already exist")
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o700)

    install_task(
        source,
        model_dir,
        airbyte_credentials=_sentinel_airbyte(rest="custom_api" in config),
        destination_credentials=_sentinel_destination(selected),
        destination=selected,
    )
    try:
        _restore_public_file(model_dir / "config.yaml", public_config_bytes)
        _restore_public_file(
            model_dir / contract.credential_filename, public_credential_bytes
        )
        assert_model_copy_is_credential_free(model_dir)
        if public_shape_verified:
            # Recheck the public shape after hardening the installed copy.
            assert_public_runtime_shape(model_dir)
    except BaseException:
        # Fail closed: a copy that cannot be proven credential-free is never
        # left on disk for a policy to be handed.
        shutil.rmtree(model_dir, ignore_errors=True)
        raise
    return AttemptCopies(
        source=source,
        model_dir=model_dir,
        replay_dir=replay_dir,
        destination=selected,
        contract=contract,
        lane=lane,
        attempt_id=resolved_attempt_id,
        principal=principal,
        revoke_principal=revoke_principal,
        credentials_provider=credentials_provider,
        public_shape_verified=public_shape_verified,
    )


def docker_lane_for(lane: str) -> str:
    """The ``runtime/process.py`` Docker network lane of an attempt lane."""
    return _DOCKER_LANE_BY_LANE[_validated_lane(lane)]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validated_lane(lane: str) -> str:
    value = str(lane)
    if value not in _DOCKER_LANE_BY_LANE:
        raise ModelCopyError(f"unknown attempt lane {value!r}; expected {LANES}")
    return value


def _assert_identifier(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.match(value) is None:
        raise ModelCopyError(f"{label} must be a short, non-secret identifier")


def _exists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _public_config(source: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load((source / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        # Never chain: a malformed config may be a previously installed copy.
        raise ModelCopyError("public task config.yaml is invalid") from None
    if not isinstance(config, dict):
        raise ModelCopyError("public task config.yaml is invalid")
    return config


def _public_destination(root: Path) -> Destination:
    return destination_from_config(_public_config(root))


def _read_credential_document(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ModelCopyError(f"{path.name} is unreadable or is not JSON") from None
    if not isinstance(payload, dict):
        raise ModelCopyError(f"{path.name} is not a JSON object")
    return payload


def _public_shape_holds(source: Path) -> bool:
    """Return whether the on-disk bundle passes the strict public gate.

    Original ELT-Bench bundles may omit fields added during installation. Such
    bundles return false and rely on placeholder and sweep checks after the
    installer normalizes a shadow copy.
    """
    try:
        assert_public_runtime_shape(source)
    except (OSError, ValueError):
        return False
    return True


def _sentinel_airbyte(*, rest: bool) -> dict[str, str]:
    values = {
        "workspace_id": _SENTINEL,
        "username": _SENTINEL,
        "password": _SENTINEL,
    }
    if rest:
        values["custom_api_definition_id"] = _SENTINEL
    return values


def _sentinel_destination(destination: Destination) -> dict[str, str]:
    return {
        field: _SENTINEL for field in _SENTINEL_DESTINATION_FIELDS[destination]
    }


def _restore_public_file(path: Path, data: bytes) -> None:
    """Write the frozen public bytes back over an installed file.

    Mirrors ``install_task``'s own discipline around attempt-state files:
    0o600 before and after the write, so the file is never briefly readable
    by anyone else.
    """
    os.chmod(path, 0o600)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _group_or_world_readable(
    root: Path, *, include_policy_tree: bool = True
) -> list[str]:
    """Paths carrying a group or world bit (or a symlink), owner-only tree.

    ``include_policy_tree=False`` skips ``elt/`` for the same reason the sweep
    does: the harness answers for its own surface, and a policy's default-umask
    file is not a reason to refuse an attempt's teardown.
    """
    shared: list[str] = []
    for path in (root, *sorted(root.rglob("*"))):
        if not include_policy_tree:
            relative = path.relative_to(root).parts
            if relative[:1] == ("elt",):
                continue
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            shared.append(f"{_relative(path, root)} (symlink)")
            continue
        if stat.S_IMODE(status.st_mode) & 0o077:
            shared.append(_relative(path, root))
    return shared


def _dotenv_pattern() -> re.Pattern[str]:
    """The shared dotenv NAME rule, or a refusal if it was renamed away."""

    for rule, pattern, _opaque in NAME_RULES:
        if rule == CANDIDATE_TREE_DOTENV_RULE:
            return pattern
    raise ModelCopyError(  # pragma: no cover - pinned by test
        f"credential_sweep no longer defines the {CANDIDATE_TREE_DOTENV_RULE!r} "
        "name rule; candidate-tree hygiene has lost its vocabulary"
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:  # pragma: no cover - rglob children are always relative
        return path.name

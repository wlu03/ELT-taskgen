"""Record and validate sandbox isolation.

Attestations bind observed host, runtime, image, contamination, and mount-scan
facts without storing secret values. Observations must match the sandbox
configuration. Missing required observations fail the preflight.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform as platform_mod
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.models import canonical_json, sha256_hex

# Attestation schema, pins, and advisory floors.

#: Sealed-record schema. Version 1.2 binds run timing, mount coverage, sandbox
#: pins, and agent configuration; older records cannot back labels.
LEGACY_SANDBOX_ATTESTATION_SCHEMA_VERSION = "1.1"
SANDBOX_ATTESTATION_SCHEMA_VERSION = "1.2"
#: Schema of the sealed preflight record whose digest the attestation carries.
SANDBOX_PREFLIGHT_SCHEMA_VERSION = "1.0"

#: runc container-escape advisory: 1.4.3 is the fixed line, 1.3.6 the 1.3.x
#: backport. Anything else (including 1.4.2 and 1.3.5) is below the floor.
RUNC_ADVISORY = "GHSA-xjvp-4fhw-gc47"
RUNC_MIN_VERSION = "1.4.3"
RUNC_MIN_BACKPORT_VERSION = "1.3.6"

#: gVisor advisory; ``release-20240325.0`` is the floor. The latest reviewed
#: tag is pinned as a RECOMMENDATION: below it is advisory, not a refusal.
RUNSC_ADVISORY = "GHSA-4fj4-9m67-3mj3"
RUNSC_MIN_RELEASE = "release-20240325.0"
RUNSC_PINNED_RELEASE = "release-20260817.0"

#: cgroup v2 with the systemd driver, and a kernel new enough for it.
KERNEL_MIN_VERSION = "5.6"
REQUIRED_CGROUP_VERSION = "2"
REQUIRED_CGROUP_DRIVER = "systemd"

#: Supported controls for proving unprivileged user namespaces are enabled.
UNPRIVILEGED_USERNS_SYSCTL = "kernel.unprivileged_userns_clone"
MAX_USER_NAMESPACES_SYSCTL = "user.max_user_namespaces"

#: The containerd version is RECORDED and is never a sufficient pin on its
#: own (the escape floors above are the pins that matter).
CONTAINERD_IS_RECORDED_NOT_PINNED = True

#: ``ELT_TASKGEN_ISOLATION=dev-only`` marks a laptop run non-attesting.
ISOLATION_ENV = "ELT_TASKGEN_ISOLATION"
DEV_ONLY = "dev-only"
#: Operator declaration for a hosted microVM or gVisor sandbox (tier B).
SANDBOX_HOST_CLASS_ENV = "ELT_TASKGEN_SANDBOX_HOST_CLASS"
OPERATOR_HOST_CLASSES: frozenset[str] = frozenset({"hosted-microvm", "hosted-sandbox"})

#: Host classes this module derives. A closed vocabulary, like the tiers.
HOST_CLASS_GVISOR = "linux-gvisor"
HOST_CLASS_RUNC = "linux-runc"
HOST_CLASS_DOCKER_DESKTOP = "docker-desktop"
HOST_CLASS_MACOS = "macos"
HOST_CLASS_DEV_ONLY = "dev-only"
HOST_CLASS_UNCLASSIFIED = "unclassified"
HOST_CLASSES: frozenset[str] = frozenset(
    {
        HOST_CLASS_GVISOR,
        HOST_CLASS_RUNC,
        HOST_CLASS_DOCKER_DESKTOP,
        HOST_CLASS_MACOS,
        HOST_CLASS_DEV_ONLY,
        HOST_CLASS_UNCLASSIFIED,
        *OPERATOR_HOST_CLASSES,
    }
)

#: Tiers, most to least isolated. ``0`` is the fail-closed value.
SANDBOX_TIERS: tuple[str, ...] = ("A", "B", "C", "D", "0")
#: Only these tiers may back a batch that claims RLVR labels
#: (``export.attestation_gate`` enforces it; recorded here as the vocabulary).
ATTESTING_TIERS: frozenset[str] = frozenset({"A", "B"})

#: Contamination enforcement vocabulary (verification/contamination.py).
CONTAMINATION_MODES: frozenset[str] = frozenset({"enforce", "observe", "off"})

#: Verifier source files bound by the attestation digest.
VERIFIER_CODE_FILES: tuple[str, ...] = (
    "export/certification.py",
    "runtime/evaluation.py",
    "runtime/execution.py",
    "training/scorer.py",
    "verification/canonical_fingerprint.py",
    "verification/gates.py",
)

#: Extra credential-shaped names for container lanes. This counter matches
#: names only, never reads file contents, and allows no placeholder exception.
CREDENTIAL_NAME_PATTERNS: tuple[str, ...] = (
    "*credential*.json",
    "service_account*.json",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "kubeconfig",
    "*.kubeconfig",
)

#: Bound on the mount walk: an unbounded tree is refused, never scanned.
MAX_MOUNT_ENTRIES = 200_000

#: Memo for the shared name rules; see :func:`_shared_name_rules`.
_NAME_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] | None = None

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^(?:[^\s@]+@)?sha256:[0-9a-f]{64}$")
#: `projection.DIAGNOSTICS_VERSION`'s shape: a dotted integer version, never
#: free text (finding p5-8).
_VERSION_STRING = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
#: ISO-8601 UTC, second resolution, `Z`-suffixed — the shape
#: `export/certification.py` stamps its `started_at` / `completed_at` in.
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
#: A short run/release identifier: what binds a record to the batch it was
#: minted for (finding p5-10).
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _utc_now() -> str:
    """Now, as ISO-8601 UTC at second resolution."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_RUNC_VERSION = re.compile(r"^runc version v?(?P<v>[0-9]+(?:\.[0-9]+){0,3})", re.M)
_RUNSC_VERSION = re.compile(
    r"^runsc version (?P<v>release-[0-9]{8}\.[0-9]+)", re.M
)
_CONTAINERD_VERSION = re.compile(
    r"^containerd\s+\S+\s+v?(?P<v>[0-9][^\s]*)", re.M
)
_NUMERIC_VERSION = re.compile(r"^(?P<v>[0-9]+(?:\.[0-9]+){0,3})")
_RUNSC_RELEASE = re.compile(r"^release-(?P<date>[0-9]{8})\.(?P<serial>[0-9]+)$")

#: Values that must never appear inside an attestation. Cheap, explicit and
#: name-based: the closed field roster is what really keeps secrets out, and
#: this is the belt that proves it in a test.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "private_key",
    "private-key",
    "-----begin",
    "authorization",
    "bearer ",
    "aws_secret",
    "client_secret",
    "passphrase",
    "session_token",
)

StrictBoolean = Annotated[bool, Field(strict=True)]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


class SandboxAttestationError(RuntimeError):
    """A sandbox observation cannot be trusted, or contradicts the pin.

    Carries a stable ``code`` so the CLI can print ``ERROR [code]`` and exit 2
    the way ``metrology.ToolchainPinError`` already does; nothing has been
    measured when this is raised.
    """

    code = "sandbox_attestation_refused"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class IsolationPreflightError(SandboxAttestationError):
    """The host did not meet the isolation floors (fail closed)."""

    code = "isolation_preflight_failed"


class SandboxPinMismatch(SandboxAttestationError):
    """An observed sandbox value differs from the pinned declaration (R-F)."""

    code = "sandbox_pin_mismatch"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class PreflightCheck(BaseModel):
    """One host assertion: what was required, what was seen, and its verdict.

    ``code`` names the FAILURE this check can produce, so a caller reads
    ``result.failures`` as a closed vocabulary instead of parsing prose.
    ``advisory`` checks are recorded and never gate (the containerd version,
    the recommended gVisor tag, the observations a hosted sandbox cannot make).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    required: str
    observed: str
    passed: StrictBoolean
    advisory: StrictBoolean = False


class PreflightResult(BaseModel):
    """The sealed, secret-free isolation observation of one host.

    ``preflight_sha256`` is sha256 over this record with that field blanked —
    the same discipline ``export/certification.py`` uses for its seals — and it
    is the value stamped into :class:`SandboxAttestation`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    preflight_schema_version: str = SANDBOX_PREFLIGHT_SCHEMA_VERSION
    isolation_mode: Literal["attesting", "dev-only"]
    tier: Literal["0", "A", "B", "C", "D"]
    host_class: str = Field(min_length=1)
    platform: str
    kernel: str
    runtime: str = Field(min_length=1)
    cgroup: str
    userns: StrictBoolean
    toolchain: dict[str, str]
    checks: tuple[PreflightCheck, ...]
    failures: tuple[str, ...]
    passed: StrictBoolean
    preflight_sha256: str = ""


class SandboxAttestation(BaseModel):
    """Immutable, checksum-bound record of the isolation a run executed under.

    Roadmap Table 9's field roster, closed (``extra="forbid"``) and frozen.
    Observed state: stamped on label-bearing records, compared to the pin at
    run start, never hashed into the admission fingerprint (R-F).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attestation_schema_version: str = SANDBOX_ATTESTATION_SCHEMA_VERSION
    tier: Literal["0", "A", "B", "C", "D"]
    host_class: str = Field(min_length=1)
    kernel: str
    runtime: str = Field(min_length=1)
    platform: str
    cgroup: str
    userns: StrictBoolean
    image_digest: str
    package_digest: str
    runtime_json_sha256: str
    oci_config_sha256: str
    tool_manifest_sha256: str
    diagnostics_version: str
    verifier_code_sha256: str
    loop_limits: dict[str, StrictNonNegativeInt]
    #: The exact ``metrology.sandbox`` declaration used to mint this record.
    #: Defaults preserve parsing and seal verification of schema-1.1 records;
    #: the label gate requires the schema-1.2 value to be present and valid.
    sandbox_pin: dict[str, str] = Field(default_factory=dict)
    #: sha256 of the canonical, parsed complete agents configuration.  This
    #: prevents a record minted under config A from being replayed under B
    #: even when the two documents happen to carry the same sandbox pin.
    agents_config_sha256: str = ""
    toolchain: dict[str, str]
    preflight_sha256: str
    contamination_mode: Literal["enforce", "observe", "off"]
    credential_files_in_mount: StrictNonNegativeInt
    #: Whether the mount was actually scanned. A zero count without this flag
    #: cannot support an RLVR label.
    mount_scanned: StrictBoolean = False
    #: Directory entries the mount walk actually saw, so a scan of the wrong
    #: (empty) tree is visible in the record rather than silent.
    mount_entries_scanned: StrictNonNegativeInt = 0
    #: Isolation observation time as ISO-8601 UTC.
    attested_at: str = ""
    #: The run / release this record was minted FOR. The gate refuses an
    #: attestation whose id does not match the batch it is admitting.
    run_id: str = ""
    #: sha256 over the canonical record with this field blanked; sealing
    #: writes it, verification recomputes it (tamper detection).
    attestation_digest: str = ""


# ---------------------------------------------------------------------------
# Small shared helpers (the conventions of runtime_matrix.py / certification.py)
# ---------------------------------------------------------------------------


def _canonical_text(value: Any, *, label: str, allow_empty: bool = True) -> str:
    """One bounded, secret-free observation string.

    Mirrors ``export/certification.py`` ``_canonical_observation``: text, no
    control characters, no surrounding whitespace, bounded length. Empty is
    allowed for the fields a pin legitimately leaves blank.
    """

    if not isinstance(value, str):
        raise SandboxAttestationError(f"{label} must be text")
    if not value:
        if allow_empty:
            return ""
        raise SandboxAttestationError(f"{label} must not be empty")
    if (
        value != value.strip()
        or len(value) > 2048
        or any(ord(char) < 32 for char in value)
    ):
        raise SandboxAttestationError(f"{label} is not canonical text")
    return value


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting ambiguous duplicate names.

    The same guard ``runtime_matrix._runner_manifest`` applies to the runner
    manifest: a duplicate key is a parse refusal, never a last-one-wins read.
    """

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _numeric_version(text: str) -> tuple[int, ...] | None:
    """Leading dotted numeric version of ``text`` (``6.8.0-45`` -> (6, 8, 0))."""

    match = _NUMERIC_VERSION.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group("v").split("."))


def _padded(version: tuple[int, ...], width: int = 3) -> tuple[int, ...]:
    return tuple(list(version[:width]) + [0] * max(0, width - len(version)))


def runc_meets_floor(version: str) -> bool:
    """``>= 1.4.3``, or ``>= 1.3.6`` on the 1.3.x backport line (``RUNC_ADVISORY``)."""

    parsed = _numeric_version(version)
    if parsed is None:
        return False
    padded = _padded(parsed)
    if padded[:2] == _padded(_numeric_version(RUNC_MIN_BACKPORT_VERSION))[:2]:
        return padded >= _padded(_numeric_version(RUNC_MIN_BACKPORT_VERSION))
    return padded >= _padded(_numeric_version(RUNC_MIN_VERSION))


def _runsc_release(tag: str) -> tuple[int, int] | None:
    match = _RUNSC_RELEASE.match(tag.strip())
    if match is None:
        return None
    return (int(match.group("date")), int(match.group("serial")))


def runsc_meets_floor(tag: str) -> bool:
    """``>= release-20240325.0`` (``RUNSC_ADVISORY``)."""

    observed = _runsc_release(tag)
    floor = _runsc_release(RUNSC_MIN_RELEASE)
    return observed is not None and floor is not None and observed >= floor


def kernel_meets_floor(release: str) -> bool:
    """``>= 5.6`` (cgroup v2 + the namespace behaviour the lane relies on)."""

    parsed = _numeric_version(release)
    if parsed is None:
        return False
    return _padded(parsed, 2) >= _padded(_numeric_version(KERNEL_MIN_VERSION), 2)


# ---------------------------------------------------------------------------
# Host observation (every command goes through an injectable Runner)
# ---------------------------------------------------------------------------


def _default_runner():
    """The repository's bounded subprocess runner (never unbounded)."""

    from elt_taskgen.runtime.process import SubprocessRunner

    return SubprocessRunner()


def _run_text(runner: Any, argv: Sequence[str]) -> str | None:
    """Stdout of one bounded command, or None when it cannot be observed.

    Never raises: an unobservable fact becomes a FAILED check upstream, which
    is what "fails closed" means here.
    """

    try:
        result = runner.run(list(argv))
    except Exception:  # noqa: BLE001 - any runner fault is "not observed"
        return None
    # The bounded runner raises on a non-zero exit; a runner that returns one
    # instead must not have its partial output read as an observation.
    if getattr(result, "returncode", 0):
        return None
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str) or len(stdout) > (1 << 20):
        return None
    return stdout


def _docker_info(runner: Any) -> dict[str, Any] | None:
    """Read only approved, non-secret fields from ``docker info``."""

    raw = _run_text(runner, ("docker", "info", "--format", "{{json .}}"))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw, object_pairs_hook=_strict_json_object)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _info_text(info: Mapping[str, Any] | None, key: str) -> str:
    value = (info or {}).get(key)
    return value.strip() if isinstance(value, str) else ""


def _docker_runtimes(info: Mapping[str, Any] | None) -> tuple[str, ...]:
    runtimes = (info or {}).get("Runtimes")
    if not isinstance(runtimes, Mapping):
        return ()
    return tuple(sorted(str(name) for name in runtimes))


def _match_version(pattern: re.Pattern[str], text: str | None) -> str:
    if not text:
        return ""
    match = pattern.search(text)
    return match.group("v") if match else ""


def _sysctl(runner: Any, name: str) -> str:
    return (_run_text(runner, ("sysctl", "-n", name)) or "").strip()


# ---------------------------------------------------------------------------
# The preflight
# ---------------------------------------------------------------------------


def _operator_host_class(
    declared: str | None, environ: Mapping[str, str]
) -> str:
    raw = declared if declared is not None else environ.get(SANDBOX_HOST_CLASS_ENV, "")
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if value not in OPERATOR_HOST_CLASSES:
        raise SandboxAttestationError(
            f"{SANDBOX_HOST_CLASS_ENV}={value!r} is not one of "
            f"{sorted(OPERATOR_HOST_CLASSES)} (fail closed)",
            code="sandbox_host_class_unknown",
        )
    return value


def isolation_preflight(
    *,
    runner: Any | None = None,
    environ: Mapping[str, str] | None = None,
    platform_system: str | None = None,
    operator_host_class: str | None = None,
) -> PreflightResult:
    """Check and seal the host's isolation properties.

    Missing required Linux, runtime, cgroup, or user-namespace facts fail.
    ``dev-only`` records tier D. Tests may inject a process runner.
    """

    environ = dict(os.environ if environ is None else environ)
    system = (
        platform_mod.system() if platform_system is None else str(platform_system)
    ).strip()
    dev_only = environ.get(ISOLATION_ENV, "").strip().lower() == DEV_ONLY
    declared_class = _operator_host_class(operator_host_class, environ)
    runner = _default_runner() if runner is None else runner

    info = _docker_info(runner)
    runc_version = _match_version(_RUNC_VERSION, _run_text(runner, ("runc", "--version")))
    runsc_version = _match_version(
        _RUNSC_VERSION, _run_text(runner, ("runsc", "--version"))
    )
    containerd_version = _match_version(
        _CONTAINERD_VERSION, _run_text(runner, ("containerd", "--version"))
    )
    kernel = (_run_text(runner, ("uname", "-r")) or "").strip().splitlines()
    kernel_release = kernel[0].strip() if kernel else _info_text(info, "KernelVersion")
    userns_clone = _sysctl(runner, UNPRIVILEGED_USERNS_SYSCTL)
    max_user_ns = _sysctl(runner, MAX_USER_NAMESPACES_SYSCTL)

    runtimes = _docker_runtimes(info)
    default_runtime = _info_text(info, "DefaultRuntime")
    cgroup_version = _info_text(info, "CgroupVersion")
    cgroup_driver = _info_text(info, "CgroupDriver")
    operating_system = _info_text(info, "OperatingSystem")
    docker_desktop = "docker desktop" in operating_system.casefold()
    is_linux = system.casefold() == "linux"
    # A hosted sandbox may omit `docker info`, but any returned engine data is
    # still enforced; declarations never override contradictory observations.
    docker_required = not declared_class
    docker_observed = info is not None
    # A registered gVisor runtime requires an observable runsc version.
    gvisor_present = "runsc" in runtimes or default_runtime == "runsc"

    userns_ok = userns_clone == "1" or (
        userns_clone == ""
        and (_numeric_version(max_user_ns) or (0,))[0] >= 1
    )

    checks: list[PreflightCheck] = [
        PreflightCheck(
            name="platform_is_linux",
            code="platform_not_linux",
            required="Linux",
            observed=system,
            passed=is_linux,
        ),
        PreflightCheck(
            name="not_docker_desktop",
            code="docker_desktop_host",
            required="a Linux engine, not Docker Desktop",
            observed=operating_system,
            passed=not docker_desktop,
        ),
        PreflightCheck(
            name="docker_info_observed",
            code="docker_info_unavailable",
            required="docker info --format {{json .}}",
            observed="observed" if info is not None else "",
            passed=info is not None,
            # A declared hosted sandbox may not expose a Docker engine.
            advisory=not docker_required,
        ),
        PreflightCheck(
            name="kernel_floor",
            code="kernel_below_floor",
            required=f">= {KERNEL_MIN_VERSION}",
            observed=kernel_release,
            passed=kernel_meets_floor(kernel_release),
        ),
        PreflightCheck(
            name="runc_floor",
            code="runc_below_floor",
            required=(
                f">= {RUNC_MIN_VERSION} or >= {RUNC_MIN_BACKPORT_VERSION} "
                f"({RUNC_ADVISORY})"
            ),
            observed=runc_version,
            passed=runc_meets_floor(runc_version),
        ),
        PreflightCheck(
            name="runsc_floor",
            code="runsc_below_floor",
            required=f">= {RUNSC_MIN_RELEASE} ({RUNSC_ADVISORY})",
            observed=runsc_version,
            # gVisor absent is tier C, not an unsafe host; gVisor PRESENT and
            # below the floor is a refusal.
            passed=not runsc_version or runsc_meets_floor(runsc_version),
        ),
        PreflightCheck(
            name="runsc_version_observed",
            code="runsc_version_unobserved",
            required=(
                "a parseable `runsc --version` whenever the engine registers "
                "runsc"
            ),
            observed=runsc_version,
            # Reject gVisor when its version is missing or unparseable; an
            # unidentified build cannot satisfy the toolchain floor.
            passed=not gvisor_present or bool(runsc_version),
        ),
        PreflightCheck(
            name="runsc_pinned_release",
            code="runsc_below_pinned_release",
            required=f"= {RUNSC_PINNED_RELEASE} (recommended)",
            observed=runsc_version,
            passed=runsc_version == RUNSC_PINNED_RELEASE,
            advisory=True,
        ),
        PreflightCheck(
            name="docker_runtimes_include_runsc",
            code="docker_runtime_missing_runsc",
            required="runsc registered as a Docker runtime",
            observed=", ".join(runtimes),
            passed="runsc" in runtimes,
            # Its absence selects tier C rather than refusing the host.
            advisory=True,
        ),
        PreflightCheck(
            name="cgroup_v2",
            code="cgroup_not_v2",
            required=f"cgroup v{REQUIRED_CGROUP_VERSION}",
            observed=cgroup_version,
            passed=cgroup_version == REQUIRED_CGROUP_VERSION,
            # Ignore this only when a declared host exposes no engine data.
            advisory=not docker_required and not docker_observed,
        ),
        PreflightCheck(
            name="cgroup_driver_systemd",
            code="cgroup_driver_not_systemd",
            required=REQUIRED_CGROUP_DRIVER,
            observed=cgroup_driver,
            passed=cgroup_driver == REQUIRED_CGROUP_DRIVER,
            advisory=not docker_required and not docker_observed,
        ),
        PreflightCheck(
            name="declared_host_class_corroborated",
            code="declared_host_class_uncorroborated",
            required=(
                "an operator-declared host class is corroborated by an "
                "observed container runtime or cgroup"
            ),
            observed=f"{declared_class or ''};{default_runtime or ''};{cgroup_version or ''}",
            # A declaration alone cannot establish tier B.
            passed=(
                not declared_class
                or bool(default_runtime)
                or bool(runtimes)
                or bool(cgroup_version)
            ),
        ),
        PreflightCheck(
            name="unprivileged_userns",
            code="unprivileged_userns_disabled",
            required=(
                f"{UNPRIVILEGED_USERNS_SYSCTL}=1 or "
                f"{MAX_USER_NAMESPACES_SYSCTL}>=1"
            ),
            observed=f"{UNPRIVILEGED_USERNS_SYSCTL}={userns_clone or ''};"
            f"{MAX_USER_NAMESPACES_SYSCTL}={max_user_ns or ''}",
            passed=userns_ok,
        ),
        PreflightCheck(
            name="containerd_recorded",
            code="containerd_not_observed",
            required="recorded, never a sufficient pin on its own",
            observed=containerd_version,
            passed=bool(containerd_version),
            advisory=True,
        ),
    ]

    failures = tuple(
        check.code for check in checks if not check.passed and not check.advisory
    )
    passed = not failures and not dev_only

    runtime = default_runtime or ("runsc" if "runsc" in runtimes else "")
    if not runtime and runtimes:
        runtime = sorted(runtimes)[0]
    if not runtime:
        # The pin vocabulary for "no container runtime" (config/agents.yaml
        # metrology.sandbox.runtime: none), so a laptop matches its own pin.
        runtime = "none"

    if dev_only:
        tier = "D"
    elif not is_linux or docker_desktop:
        tier = "D"
    elif not passed:
        tier = "0"
    elif declared_class:
        tier = "B"
    elif runtime == "runsc" and runsc_meets_floor(runsc_version):
        # Tier A requires an observed runsc version that meets the floor.
        tier = "A"
    elif runtime == "runc":
        tier = "C"
    else:
        tier = "0"

    if dev_only:
        host_class = HOST_CLASS_DEV_ONLY
    elif tier == "B":
        host_class = declared_class
    elif tier == "A":
        host_class = HOST_CLASS_GVISOR
    elif tier == "C":
        host_class = HOST_CLASS_RUNC
    elif docker_desktop:
        host_class = HOST_CLASS_DOCKER_DESKTOP
    elif not is_linux:
        host_class = HOST_CLASS_MACOS if system.casefold() == "darwin" else (
            HOST_CLASS_UNCLASSIFIED
        )
    else:
        host_class = HOST_CLASS_UNCLASSIFIED

    toolchain = {
        "containerd": containerd_version,
        "docker": _info_text(info, "ServerVersion"),
        "kernel": kernel_release,
        "runc": runc_version,
        "runsc": runsc_version,
    }
    cgroup = (
        f"v{cgroup_version}/{cgroup_driver}"
        if cgroup_version or cgroup_driver
        else ""
    )
    result = PreflightResult(
        isolation_mode=DEV_ONLY if dev_only else "attesting",
        tier=tier,
        host_class=host_class,
        platform=system,
        kernel=kernel_release,
        runtime=runtime,
        cgroup=cgroup,
        userns=bool(userns_ok),
        toolchain=toolchain,
        checks=tuple(checks),
        failures=failures,
        passed=passed,
    )
    return seal_preflight(result)


def preflight_digest(result: PreflightResult) -> str:
    """sha256 over the canonical record with ``preflight_sha256`` blanked."""

    payload = result.model_dump(mode="json")
    payload["preflight_sha256"] = ""
    return sha256_hex(canonical_json(payload))


def seal_preflight(result: PreflightResult) -> PreflightResult:
    """Checksum-bind the observation; any later edit is detectable."""

    return result.model_copy(update={"preflight_sha256": preflight_digest(result)})


# ---------------------------------------------------------------------------
# Mount hygiene: credential-shaped files are COUNTED, never opened
# ---------------------------------------------------------------------------


def _shared_name_rules() -> tuple[tuple[str, re.Pattern[str], bool], ...]:
    """The repository's credential NAME rules, imported once and cached.

    Imported lazily so this module keeps its dependency-light import graph
    (only ``elt_taskgen.models`` at module scope), which is what lets
    ``runtime_matrix`` re-export the surface if the owner takes that hand-off.
    """

    global _NAME_RULES
    if _NAME_RULES is None:
        from elt_taskgen.review.tools.credential_sweep import NAME_RULES

        _NAME_RULES = tuple(NAME_RULES)
    return _NAME_RULES


def is_credential_shaped(name: str) -> bool:
    """Does this FILE NAME look like a credential? (names only, no reads).

    The union of the repository's shared name rules
    (``review.tools.credential_sweep.NAME_RULES``) and this module's
    container-lane additions, so the mount counter can never be more
    forgiving about a NAME than the sweep every other surface uses.
    """

    base = os.path.basename(str(name))
    if any(pattern.search(base) for _rule, pattern, _opaque in _shared_name_rules()):
        return True
    lowered = base.casefold()
    return any(
        fnmatch.fnmatchcase(lowered, pattern)
        for pattern in CREDENTIAL_NAME_PATTERNS
    )


class MountScan(BaseModel):
    """What ONE mount walk actually saw (finding p5-3).

    ``credential_files`` alone is ambiguous: a name-only counter answers 0
    both for "I walked this tree and found nothing" and for "I was handed no
    tree at all", and the sealed record could not tell them apart.
    ``scanned`` and ``entries_seen`` make the difference explicit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scanned: StrictBoolean
    entries_seen: StrictNonNegativeInt
    credential_files: StrictNonNegativeInt


def scan_mount_for_credentials(root: Path | str | None) -> MountScan:
    """Count credential-shaped file names without reading file contents.

    A missing root is not scanned. The bounded walk does not follow directory
    symlinks.
    """

    if root is None:
        return MountScan(scanned=False, entries_seen=0, credential_files=0)
    base = Path(root)
    if not base.is_dir():
        return MountScan(scanned=False, entries_seen=0, credential_files=0)
    seen = 0
    found = 0
    for _directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            seen += 1
            if seen > MAX_MOUNT_ENTRIES:
                raise SandboxAttestationError(
                    f"mount at {base} exceeds {MAX_MOUNT_ENTRIES} entries; "
                    "it was not scanned (fail closed)",
                    code="mount_too_large",
                )
            if is_credential_shaped(name):
                found += 1
    return MountScan(scanned=True, entries_seen=seen, credential_files=found)


def count_credential_files(root: Path | str | None) -> int:
    """Number of credential-shaped files under ``root`` (0 for a missing one).

    The count alone cannot say whether anything was scanned; callers that
    need to know use :func:`scan_mount_for_credentials`, and
    :func:`attest_sandbox` records both.
    """

    return scan_mount_for_credentials(root).credential_files


# ---------------------------------------------------------------------------
# Defaults for the derived attestation fields
# ---------------------------------------------------------------------------


def default_verifier_code_sha256() -> str:
    """One digest over the SOURCE BYTES of the deterministic verifier files."""

    root = _package_root()
    checksums: dict[str, str] = {}
    for relative in VERIFIER_CODE_FILES:
        path = root.joinpath(*relative.split("/"))
        if path.is_symlink() or not path.is_file():
            raise SandboxAttestationError(
                f"verifier source {relative} is missing (fail closed)",
                code="verifier_code_missing",
            )
        checksums[relative] = _file_sha256(path)
    return sha256_hex(canonical_json({"files": checksums}))


def default_package_digest() -> str:
    """One digest over the installed distribution set (name, version)."""

    from importlib import metadata

    installed: list[list[str]] = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"] if dist.metadata else None
        if not name:
            continue
        installed.append([str(name), str(dist.version or "")])
    installed.sort()
    return sha256_hex(canonical_json({"distributions": installed}))


def default_diagnostics_version() -> str:
    """``review.tools.projection.DIAGNOSTICS_VERSION`` (recorded, not hashed)."""

    from elt_taskgen.review.tools.projection import DIAGNOSTICS_VERSION

    return str(DIAGNOSTICS_VERSION)


def default_tool_manifest_sha256() -> str:
    """Digest of the wire tool surface every session-runner role may use."""

    from elt_taskgen.review import providers

    surface = {
        role: providers.wire_tools_for(role)
        for role in sorted(providers.SESSION_RUNNER_ROLES)
    }
    return sha256_hex(canonical_json(surface))


def harness_loop_limits(
    *, agents_config: Path | str | Mapping[str, Any] | None = None
) -> dict[str, int]:
    """The integer loop limits the session runners enforce, flattened.

    Read through ``providers.role_manifest_limits`` — the same object the
    behaviour manifest hashes — so the attestation records the bounds the run
    actually enforced. Empty while every session block ships disabled.
    """

    from elt_taskgen.review import providers

    limits: dict[str, int] = {}
    for role in sorted(providers.SESSION_RUNNER_ROLES):
        declared = providers.role_manifest_limits(role, agents_config=agents_config)
        for key, value in sorted(declared.items()):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            limits[f"{role}.{key}"] = int(value)
    return limits


def default_contamination_mode(
    environ: Mapping[str, str] | None = None,
) -> str:
    """The contamination enforcement mode this process runs under."""

    from elt_taskgen.verification.contamination import enforcement

    return enforcement(dict(environ) if environ is not None else None).value


def pinned_sandbox_declaration(
    *, agents_config: Path | str | None = None
) -> dict[str, str]:
    """The PINNED ``metrology.sandbox`` declaration (R-F), never an observation.

    Delegates to ``review.providers.sandbox_pin``: one reader, so the object
    compared here is byte-identical to the object the behaviour manifest
    hashes.
    """

    from elt_taskgen.review.providers import sandbox_pin

    return dict(sandbox_pin(agents_config=agents_config))


def observed_sandbox(
    preflight: PreflightResult,
    *,
    image_digest: str = "",
    workspace_template_sha256: str = "",
) -> dict[str, str]:
    """The observed counterpart of the pin: the same three keys, measured."""

    return {
        "runtime": preflight.runtime,
        "image_digest": _canonical_text(image_digest, label="image_digest"),
        "workspace_template_sha256": _canonical_text(
            workspace_template_sha256, label="workspace_template_sha256"
        ),
    }


# ---------------------------------------------------------------------------
# Sealing, verification and the secret-free assertion
# ---------------------------------------------------------------------------


def sandbox_attestation_digest(attestation: SandboxAttestation) -> str:
    """sha256 over the canonical record with ``attestation_digest`` blanked."""

    payload = attestation.model_dump(mode="json")
    # Schema 1.1 predates the two config-identity fields.  They have defaults
    # solely so the current closed model can parse historical records; omit
    # them when reproducing that historical seal.  New records are always 1.2.
    if attestation.attestation_schema_version == LEGACY_SANDBOX_ATTESTATION_SCHEMA_VERSION:
        payload.pop("sandbox_pin", None)
        payload.pop("agents_config_sha256", None)
    payload["attestation_digest"] = ""
    return sha256_hex(canonical_json(payload))


def seal_sandbox_attestation(attestation: SandboxAttestation) -> SandboxAttestation:
    """Checksum-bind the record: any later edit is detectable."""

    return attestation.model_copy(
        update={"attestation_digest": sandbox_attestation_digest(attestation)}
    )


def assert_attestation_is_secret_free(attestation: SandboxAttestation) -> None:
    """No secret material anywhere in the record (values and nested keys).

    The closed field roster is the real control; this is the assertion that
    proves it, and it is what ``export/attestation_gate`` calls before a
    record is allowed to back any RLVR label.
    """

    payload = attestation.model_dump(mode="json")

    def _scan(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                _scan(str(key), f"{path}.{key} (key)")
                _scan(value, f"{path}.{key}")
            return
        if isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                _scan(value, f"{path}[{index}]")
            return
        if isinstance(node, bool) or isinstance(node, (int, float)) or node is None:
            return
        if not isinstance(node, str):
            raise SandboxAttestationError(
                f"attestation field {path} is not a scalar the schema allows",
                code="attestation_not_scalar",
            )
        _canonical_text(node, label=f"attestation field {path}")
        lowered = node.casefold()
        for marker in _SECRET_MARKERS:
            if marker in lowered:
                raise SandboxAttestationError(
                    f"attestation field {path} contains secret-shaped material",
                    code="attestation_secret_material",
                )

    for key, value in payload.items():
        # The FIELD NAMES are the closed roster (``credential_files_in_mount``
        # legitimately contains the word); only the values are scanned.
        _scan(value, key)


def verify_sandbox_attestation(
    attestation: SandboxAttestation,
) -> SandboxAttestation:
    """Re-derive the seal and re-assert every closed invariant, fail closed."""

    if not isinstance(attestation, SandboxAttestation):
        raise SandboxAttestationError(
            "sandbox attestation object is invalid",
            code="attestation_invalid",
        )
    try:
        # ``model_copy(update=...)`` deliberately skips validation, so reparse
        # even an in-memory record (the discipline verify_attestation uses).
        reparsed = SandboxAttestation.model_validate(
            attestation.model_dump(mode="python")
        )
    except ValueError as exc:
        raise SandboxAttestationError(
            f"sandbox attestation object is invalid: {exc}",
            code="attestation_invalid",
        ) from exc
    if reparsed.attestation_schema_version not in {
        LEGACY_SANDBOX_ATTESTATION_SCHEMA_VERSION,
        SANDBOX_ATTESTATION_SCHEMA_VERSION,
    }:
        raise SandboxAttestationError(
            "unsupported sandbox attestation schema "
            f"{reparsed.attestation_schema_version!r}",
            code="attestation_schema_unsupported",
        )
    if not reparsed.attestation_digest:
        raise SandboxAttestationError(
            "sandbox attestation was never sealed (no digest)",
            code="attestation_unsealed",
        )
    if _SHA256.fullmatch(reparsed.preflight_sha256) is None:
        raise SandboxAttestationError(
            "sandbox attestation records no preflight digest",
            code="attestation_preflight_missing",
        )
    if reparsed.host_class not in HOST_CLASSES:
        raise SandboxAttestationError(
            f"sandbox attestation host class {reparsed.host_class!r} is unknown",
            code="attestation_host_class_unknown",
        )
    if reparsed.attestation_schema_version == SANDBOX_ATTESTATION_SCHEMA_VERSION:
        expected_pin_keys = {
            "runtime",
            "image_digest",
            "workspace_template_sha256",
        }
        if set(reparsed.sandbox_pin) != expected_pin_keys:
            raise SandboxAttestationError(
                "sandbox attestation does not seal the exact configured pin",
                code="attestation_config_unbound",
            )
        for key, value in reparsed.sandbox_pin.items():
            _canonical_text(value, label=f"sandbox_pin.{key}")
        if not reparsed.sandbox_pin["runtime"]:
            raise SandboxAttestationError(
                "sandbox attestation configured runtime is empty",
                code="attestation_config_unbound",
            )
        image_pin = reparsed.sandbox_pin["image_digest"]
        if image_pin and _IMAGE_DIGEST.fullmatch(image_pin) is None:
            raise SandboxAttestationError(
                "sandbox attestation configured image digest is malformed",
                code="attestation_config_unbound",
            )
        template_pin = reparsed.sandbox_pin["workspace_template_sha256"]
        if template_pin and _SHA256.fullmatch(template_pin) is None:
            raise SandboxAttestationError(
                "sandbox attestation configured workspace-template digest is malformed",
                code="attestation_config_unbound",
            )
        if _SHA256.fullmatch(reparsed.agents_config_sha256) is None:
            raise SandboxAttestationError(
                "sandbox attestation does not seal an agents-config identity",
                code="attestation_config_unbound",
            )
    assert_attestation_is_secret_free(reparsed)
    expected = sandbox_attestation_digest(reparsed)
    if reparsed.attestation_digest != expected:
        raise SandboxAttestationError(
            "sandbox attestation digest mismatch: the record was modified "
            "after sealing",
            code="attestation_digest_mismatch",
        )
    return reparsed


def assert_attestation_matches_agents_config(
    attestation: SandboxAttestation,
    *,
    agents_config: Path | str | None = None,
) -> SandboxAttestation:
    """Require an attestation minted under this exact active configuration.

    This is an active-run check, distinct from historical release
    verification: a frozen release remains verifiable after the operator's
    local default config changes, while a new freeze/certification run cannot
    reuse config A's attestation under config B.
    """

    verified = verify_sandbox_attestation(attestation)
    if verified.attestation_schema_version != SANDBOX_ATTESTATION_SCHEMA_VERSION:
        raise SandboxAttestationError(
            "legacy sandbox attestation does not bind an agents configuration; "
            "re-mint it under the active --agents-config",
            code="attestation_config_unbound",
        )
    from elt_taskgen.review import providers as providers_mod

    # One parsed document supplies every active-config expectation. Reopening
    # the pathname between pin/SHA and loop-limit checks permits a mixed A/B
    # record during an otherwise ordinary atomic config replacement.
    config_snapshot = providers_mod._agents_doc_for(agents_config)
    expected_pin, expected_config = providers_mod.agents_config_identity(
        agents_config=config_snapshot
    )
    if verified.sandbox_pin != expected_pin:
        raise SandboxAttestationError(
            "sandbox attestation pin differs from the active --agents-config",
            code="attestation_config_mismatch",
        )
    if verified.agents_config_sha256 != expected_config:
        raise SandboxAttestationError(
            "sandbox attestation was minted under a different agents configuration",
            code="attestation_config_mismatch",
        )
    if verified.loop_limits != harness_loop_limits(agents_config=config_snapshot):
        raise SandboxAttestationError(
            "sandbox attestation loop limits differ from the active agents "
            "configuration",
            code="attestation_config_mismatch",
        )
    return verified


# ---------------------------------------------------------------------------
# The attestation itself
# ---------------------------------------------------------------------------


def attest_sandbox(
    *,
    pinned: Mapping[str, str],
    preflight: PreflightResult,
    observed: Mapping[str, str] | None = None,
    image_digest: str = "",
    workspace_template_sha256: str = "",
    package_digest: str | None = None,
    runtime_json_sha256: str = "",
    oci_config_sha256: str = "",
    tool_manifest_sha256: str | None = None,
    diagnostics_version: str | None = None,
    verifier_code_sha256: str | None = None,
    loop_limits: Mapping[str, int] | None = None,
    contamination_mode: str | None = None,
    mount_root: Path | str,
    run_id: str = "",
    attested_at: str | None = None,
    agents_config: Path | str | None = None,
) -> SandboxAttestation:
    """Seal a sandbox after validating its pin and execution mount.

    Pin differences and failed preflights raise typed errors. ``dev-only`` may
    record tier D. The mount scan reads names but not file contents.
    """

    if not isinstance(preflight, PreflightResult):
        raise SandboxAttestationError(
            "attest_sandbox requires a PreflightResult", code="preflight_invalid"
        )
    scan = scan_mount_for_credentials(mount_root)
    if not scan.scanned:
        raise SandboxAttestationError(
            f"mount_root {str(mount_root)!r} is not an existing directory; "
            "credential_files_in_mount is a zero-tolerance control and an "
            "unscanned mount is not evidence of a clean one (fail closed)",
            code="mount_root_missing",
        )
    if preflight.preflight_sha256 != preflight_digest(preflight):
        raise SandboxAttestationError(
            "preflight result was modified after sealing",
            code="preflight_digest_mismatch",
        )
    dev_only = preflight.isolation_mode == DEV_ONLY
    if not preflight.passed and not dev_only:
        raise IsolationPreflightError(
            "isolation preflight failed: "
            + ", ".join(preflight.failures or ("unknown",))
            + f" (host class {preflight.host_class}); no attestation was minted"
        )

    if not isinstance(pinned, Mapping):
        raise SandboxAttestationError(
            "pinned sandbox declaration must be a mapping", code="sandbox_pin_invalid"
        )
    declaration = {
        str(key): _canonical_text(value, label=f"pinned {key}")
        for key, value in pinned.items()
    }
    from elt_taskgen.review import providers as providers_mod

    # Capture the complete document once. Every configuration-derived field
    # below is computed from this in-memory snapshot, never by reopening the
    # path later in the attestation.
    config_snapshot = providers_mod._agents_doc_for(agents_config)
    configured_pin, configured_agents_sha256 = providers_mod.agents_config_identity(
        agents_config=config_snapshot
    )
    configured_loop_limits = harness_loop_limits(agents_config=config_snapshot)
    if declaration != configured_pin:
        raise SandboxPinMismatch(
            "the supplied sandbox pin is not the metrology.sandbox declaration "
            "in the active agents configuration"
        )
    if observed is None:
        measured = observed_sandbox(
            preflight,
            image_digest=image_digest,
            workspace_template_sha256=workspace_template_sha256,
        )
    else:
        if not isinstance(observed, Mapping):
            raise SandboxAttestationError(
                "observed sandbox state must be a mapping",
                code="sandbox_observation_invalid",
            )
        measured = {
            str(key): _canonical_text(value, label=f"observed {key}")
            for key, value in observed.items()
        }
        # A caller-supplied observation must still be THIS host's: otherwise a
        # record could pass the pin comparison while stamping a runtime the
        # preflight never saw.
        if measured.get("runtime", preflight.runtime) != preflight.runtime:
            raise SandboxPinMismatch(
                f"observed runtime {measured.get('runtime')!r} contradicts the "
                f"preflight observation {preflight.runtime!r}"
            )
    if set(measured) != set(declaration):
        raise SandboxPinMismatch(
            "observed sandbox keys "
            f"{sorted(measured)} do not match the pinned declaration "
            f"{sorted(declaration)} (config/agents.yaml metrology.sandbox)"
        )
    drift = {
        key: (declaration[key], measured[key])
        for key in sorted(declaration)
        if declaration[key] != measured[key]
    }
    if drift:
        described = ", ".join(
            f"{key}: pinned {pin!r}, observed {seen!r}"
            for key, (pin, seen) in drift.items()
        )
        raise SandboxPinMismatch(
            f"sandbox pin mismatch ({described}). The admission fingerprint "
            "hashes the PINNED declaration (config/agents.yaml "
            "metrology.sandbox), so a run under drifted isolation could not be "
            "told apart from one under the pinned isolation; re-pin and "
            "re-earn, or run on the pinned sandbox."
        )

    resolved_image = measured.get("image_digest", "")
    if resolved_image and _IMAGE_DIGEST.fullmatch(resolved_image) is None:
        raise SandboxAttestationError(
            "observed image_digest is not an exact sha256 reference",
            code="image_digest_unpinned",
        )
    for label, value in (
        ("runtime_json_sha256", runtime_json_sha256),
        ("oci_config_sha256", oci_config_sha256),
    ):
        text = _canonical_text(value, label=label)
        if text and _SHA256.fullmatch(text) is None:
            raise SandboxAttestationError(
                f"{label} is not a sha256", code="digest_malformed"
            )

    resolved_package = (
        default_package_digest() if package_digest is None else str(package_digest)
    )
    resolved_tool_manifest = (
        default_tool_manifest_sha256()
        if tool_manifest_sha256 is None
        else str(tool_manifest_sha256)
    )
    resolved_verifier = (
        default_verifier_code_sha256()
        if verifier_code_sha256 is None
        else str(verifier_code_sha256)
    )
    resolved_diagnostics = (
        default_diagnostics_version()
        if diagnostics_version is None
        else str(diagnostics_version)
    )
    # Shape-check every digest field; keyword filtering cannot catch opaque
    # secrets stored in a digest slot.
    for label, value in (
        ("package_digest", resolved_package),
        ("tool_manifest_sha256", resolved_tool_manifest),
        ("verifier_code_sha256", resolved_verifier),
    ):
        text = _canonical_text(value, label=label)
        if text and _SHA256.fullmatch(text) is None:
            raise SandboxAttestationError(
                f"{label} is not a sha256", code="digest_malformed"
            )
    diagnostics_text = _canonical_text(
        resolved_diagnostics, label="diagnostics_version"
    )
    if diagnostics_text and _VERSION_STRING.fullmatch(diagnostics_text) is None:
        raise SandboxAttestationError(
            "diagnostics_version is not a dotted version string",
            code="diagnostics_version_malformed",
        )

    stamped_at = _canonical_text(
        _utc_now() if attested_at is None else str(attested_at), label="attested_at"
    )
    if stamped_at and _ISO_UTC.fullmatch(stamped_at) is None:
        raise SandboxAttestationError(
            "attested_at is not an ISO-8601 UTC timestamp",
            code="attested_at_malformed",
        )
    run_label = _canonical_text(run_id, label="run_id")
    if run_label and _RUN_ID.fullmatch(run_label) is None:
        raise SandboxAttestationError(
            "run_id must be a short identifier "
            "(letters, digits, '.', '_', '-', at most 128 characters)",
            code="run_id_malformed",
        )

    mode = (
        default_contamination_mode()
        if contamination_mode is None
        else str(contamination_mode)
    )
    if mode not in CONTAMINATION_MODES:
        raise SandboxAttestationError(
            f"contamination mode {mode!r} is not one of {sorted(CONTAMINATION_MODES)}",
            code="contamination_mode_unknown",
        )

    if loop_limits is None:
        resolved_loop_limits = configured_loop_limits
    else:
        if not isinstance(loop_limits, Mapping):
            raise SandboxAttestationError(
                "loop_limits must be a mapping", code="loop_limits_invalid"
            )
        resolved_loop_limits: dict[str, int] = {}
        for key, value in loop_limits.items():
            if (
                not isinstance(key, str)
                or not key
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise SandboxAttestationError(
                    "loop_limits must contain non-empty string keys and "
                    "non-negative integer values",
                    code="loop_limits_invalid",
                )
            resolved_loop_limits[key] = value
        if resolved_loop_limits != configured_loop_limits:
            raise SandboxAttestationError(
                "supplied loop_limits differ from the active agents "
                "configuration",
                code="loop_limits_config_mismatch",
            )

    attestation = SandboxAttestation(
        tier=preflight.tier,
        host_class=preflight.host_class,
        kernel=preflight.kernel,
        runtime=preflight.runtime,
        platform=preflight.platform,
        cgroup=preflight.cgroup,
        userns=preflight.userns,
        image_digest=resolved_image,
        package_digest=resolved_package,
        runtime_json_sha256=str(runtime_json_sha256),
        oci_config_sha256=str(oci_config_sha256),
        tool_manifest_sha256=resolved_tool_manifest,
        diagnostics_version=resolved_diagnostics,
        verifier_code_sha256=resolved_verifier,
        loop_limits=dict(resolved_loop_limits),
        sandbox_pin=declaration,
        agents_config_sha256=configured_agents_sha256,
        toolchain=dict(preflight.toolchain),
        preflight_sha256=preflight.preflight_sha256,
        contamination_mode=mode,
        credential_files_in_mount=scan.credential_files,
        mount_scanned=scan.scanned,
        mount_entries_scanned=scan.entries_seen,
        attested_at=stamped_at,
        run_id=run_label,
    )
    assert_attestation_is_secret_free(attestation)
    return seal_sandbox_attestation(attestation)


__all__ = [
    "assert_attestation_matches_agents_config",
    "assert_attestation_is_secret_free",
    "attest_sandbox",
    "ATTESTING_TIERS",
    "CONTAMINATION_MODES",
    "count_credential_files",
    "CREDENTIAL_NAME_PATTERNS",
    "default_contamination_mode",
    "default_diagnostics_version",
    "default_package_digest",
    "default_tool_manifest_sha256",
    "default_verifier_code_sha256",
    "DEV_ONLY",
    "harness_loop_limits",
    "HOST_CLASSES",
    "is_credential_shaped",
    "ISOLATION_ENV",
    "isolation_preflight",
    "IsolationPreflightError",
    "kernel_meets_floor",
    "KERNEL_MIN_VERSION",
    "LEGACY_SANDBOX_ATTESTATION_SCHEMA_VERSION",
    "MAX_USER_NAMESPACES_SYSCTL",
    "MountScan",
    "observed_sandbox",
    "OPERATOR_HOST_CLASSES",
    "pinned_sandbox_declaration",
    "preflight_digest",
    "PreflightCheck",
    "PreflightResult",
    "REQUIRED_CGROUP_DRIVER",
    "REQUIRED_CGROUP_VERSION",
    "RUNC_ADVISORY",
    "runc_meets_floor",
    "RUNC_MIN_BACKPORT_VERSION",
    "RUNC_MIN_VERSION",
    "RUNSC_ADVISORY",
    "runsc_meets_floor",
    "RUNSC_MIN_RELEASE",
    "RUNSC_PINNED_RELEASE",
    "sandbox_attestation_digest",
    "SANDBOX_ATTESTATION_SCHEMA_VERSION",
    "SANDBOX_HOST_CLASS_ENV",
    "SANDBOX_PREFLIGHT_SCHEMA_VERSION",
    "SANDBOX_TIERS",
    "SandboxAttestation",
    "SandboxAttestationError",
    "SandboxPinMismatch",
    "scan_mount_for_credentials",
    "seal_preflight",
    "seal_sandbox_attestation",
    "UNPRIVILEGED_USERNS_SYSCTL",
    "VERIFIER_CODE_FILES",
    "verify_sandbox_attestation",
]

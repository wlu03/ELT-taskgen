"""Tests for runtime/attestation.py (roadmap Phase 5, Table 9; Output 10 A22).

WHY THIS EXISTS
The sandbox attestation is the only evidence that a label-bearing run was
produced under isolation anyone can name. Three properties have to hold or the
evidence is worthless:

  * it is SECRET-FREE and SEALED — the same discipline export/certification.py
    states in its header ("No secrets ever enter an attestation") and enforces
    with a digest over the canonical record;
  * the preflight FAILS CLOSED — a host with a vulnerable ``runc``/``runsc``,
    a missing floor or an observation that could not be made yields no
    attestation at all, and macOS attests only as the explicitly non-attesting
    ``dev-only`` laptop lane;
  * the PINNED declaration, never the observation, is what the admission
    fingerprint hashes (R-F): ``attest_sandbox`` refuses drift instead of
    recording it, so a run under other isolation cannot be mistaken for one
    under the pin.

NO REAL COMMAND RUNS HERE. Every host fact reaches the preflight through an
injected ``runtime.process.Runner`` double, so no test starts ``docker``,
``runc``, ``runsc``, ``uname`` or ``sysctl``. Credential-shaped files are
counted by NAME with reads disabled, so nothing under a mount is ever opened.
"""

from __future__ import annotations

import atexit
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from elt_taskgen.destinations import Destination, destination_contract
from elt_taskgen.review import metrology as metrology_mod
from elt_taskgen.review import providers as providers_mod
from elt_taskgen.runtime import attestation as att
from elt_taskgen.runtime.process import CommandResult, ProcessFailure

DOCKER_INFO = ("docker", "info", "--format", "{{json .}}")
RUNC = ("runc", "--version")
RUNSC = ("runsc", "--version")
CONTAINERD = ("containerd", "--version")
UNAME = ("uname", "-r")
USERNS = ("sysctl", "-n", att.UNPRIVILEGED_USERNS_SYSCTL)
MAX_USERNS = ("sysctl", "-n", att.MAX_USER_NAMESPACES_SYSCTL)


class FakeRunner:
    """A ``runtime.process.Runner`` double: recorded stdout, never a process.

    An argv with no recorded output raises ``ProcessFailure`` exactly as the
    real runner does for a missing or failing binary, which is how the
    "observation could not be made" path is exercised without a host.
    """

    def __init__(self, outputs):
        self.outputs = {tuple(key): value for key, value in dict(outputs).items()}
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        key = tuple(str(value) for value in argv)
        self.calls.append(key)
        if key not in self.outputs:
            raise ProcessFailure(f"{key[0]} is not installed")
        value = self.outputs[key]
        if isinstance(value, BaseException):
            raise value
        return CommandResult(0, str(value), "")


def docker_info_json(**overrides) -> str:
    info = {
        "Runtimes": {"runc": {"path": "runc"}, "runsc": {"path": "runsc"}},
        "DefaultRuntime": "runsc",
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "KernelVersion": "6.8.0-45-generic",
        "OperatingSystem": "Ubuntu 24.04.1 LTS",
        "ServerVersion": "27.3.1",
    }
    info.update(overrides)
    return json.dumps(info)


def tier_a_outputs(overrides=None) -> dict[tuple[str, ...], str]:
    """A Linux gVisor host that meets every floor."""

    outputs: dict[tuple[str, ...], str] = {
        DOCKER_INFO: docker_info_json(),
        RUNC: "runc version 1.4.3\ncommit: v1.4.3-0-gd44b5a1\nspec: 1.2.0\n",
        RUNSC: f"runsc version {att.RUNSC_PINNED_RELEASE}\nspec: 1.1.0-rc.1\n",
        CONTAINERD: (
            "containerd github.com/containerd/containerd v2.1.4 "
            "0f4b7a2c9c0a1b2d3e4f5061728394a5b6c7d8e9\n"
        ),
        UNAME: "6.8.0-45-generic\n",
        USERNS: "1\n",
        MAX_USERNS: "63594\n",
    }
    outputs.update(dict(overrides or {}))
    return outputs


def tier_c_outputs(overrides=None) -> dict[tuple[str, ...], str]:
    """A Linux host with plain runc and no gVisor at all."""

    outputs = tier_a_outputs()
    outputs[DOCKER_INFO] = docker_info_json(
        Runtimes={"runc": {"path": "runc"}}, DefaultRuntime="runc"
    )
    outputs.pop(RUNSC)
    outputs.update(dict(overrides or {}))
    return outputs


def preflight(outputs, *, system="Linux", environ=None, host_class=None):
    return att.isolation_preflight(
        runner=FakeRunner(outputs),
        environ={} if environ is None else environ,
        platform_system=system,
        operator_host_class=host_class,
    )


#: A pin that matches the tier-A double, so the R-F comparison is exercised
#: against something other than the repository's laptop default.
GVISOR_PIN = {
    "runtime": "runsc",
    "image_digest": "",
    "workspace_template_sha256": "",
}
LAPTOP_PIN = {
    "runtime": "none",
    "image_digest": "",
    "workspace_template_sha256": "",
}

#: Cheap, explicit values for the derived fields so most tests neither hash
#: the verifier sources nor enumerate installed distributions.
CHEAP_DERIVED = dict(
    package_digest="",
    tool_manifest_sha256="",
    diagnostics_version="2",
    verifier_code_sha256="",
    contamination_mode="enforce",
)

#: `attest_sandbox` REQUIRES a mount to scan (finding p5-3): an unscanned
#: mount is not evidence of a clean one, so the default cannot be None any
#: more. This is one empty directory shared by every test that does not care
#: what is in the mount; it is removed when the process exits.
EMPTY_MOUNT = Path(tempfile.mkdtemp(prefix="elt-attest-empty-mount-"))
atexit.register(shutil.rmtree, EMPTY_MOUNT, True)
PIN_CONFIG_ROOT = Path(tempfile.mkdtemp(prefix="elt-attest-pin-config-"))
atexit.register(shutil.rmtree, PIN_CONFIG_ROOT, True)


def agents_config_for_pin(pin: dict[str, str]) -> Path:
    """Minimal synthetic agents config whose sandbox declaration is ``pin``."""

    # Avoid importing another production helper into this fixture: JSON is
    # valid YAML and the canonical spelling gives stable, collision-free test
    # filenames for all pin variants exercised below.
    import hashlib

    canonical = json.dumps(dict(pin), sort_keys=True, separators=(",", ":"))
    path = PIN_CONFIG_ROOT / f"{hashlib.sha256(canonical.encode()).hexdigest()}.json"
    if not path.exists():
        document = yaml.safe_load(
            (Path(__file__).parents[1] / "config" / "agents.yaml").read_text(
                encoding="utf-8"
            )
        )
        document.setdefault("metrology", {})["sandbox"] = dict(pin)
        path.write_text(
            json.dumps(document, sort_keys=True),
            encoding="utf-8",
        )
    return path


def attest(**kwargs):
    """`att.attest_sandbox` with the cheap derived fields and the empty
    mount, unless the caller overrides them."""
    fields = {**CHEAP_DERIVED, "mount_root": EMPTY_MOUNT}
    fields.update(kwargs)
    if "agents_config" not in fields and isinstance(fields.get("pinned"), dict):
        fields["agents_config"] = agents_config_for_pin(fields["pinned"])
    return att.attest_sandbox(**fields)


class TestPreflightFloors(unittest.TestCase):
    """The floors, their advisories and what each one refuses."""

    def test_preflight_asserts_runc_floor(self) -> None:
        """`>= 1.4.3`, or `>= 1.3.6` on the backport line (GHSA-xjvp-4fhw-gc47).

        1.4.2 and 1.3.5 are BELOW the fix on their own lines and must be
        refused; an unparseable or missing version is refused too (fail
        closed), never treated as new enough.
        """
        for version, expected in (
            ("1.4.3", True),
            ("1.4.4", True),
            ("1.5.0", True),
            ("2.0.0", True),
            ("1.3.6", True),
            ("1.3.9", True),
            ("1.4.2", False),
            ("1.3.5", False),
            ("1.2.9", False),
            ("0.9.9", False),
            ("", False),
            ("not-a-version", False),
        ):
            with self.subTest(runc=version):
                self.assertIs(att.runc_meets_floor(version), expected)

        # End to end, through the injected runner: the backport passes and the
        # version just below it fails with the closed code.
        ok = preflight(tier_a_outputs({RUNC: "runc version 1.3.6\n"}))
        self.assertTrue(ok.passed, ok.failures)
        self.assertEqual(ok.toolchain["runc"], "1.3.6")
        bad = preflight(tier_a_outputs({RUNC: "runc version 1.3.5\n"}))
        self.assertFalse(bad.passed)
        self.assertIn("runc_below_floor", bad.failures)
        self.assertEqual(bad.tier, "0")

        check = {c.name: c for c in bad.checks}["runc_floor"]
        self.assertIn(att.RUNC_ADVISORY, check.required)
        self.assertFalse(check.advisory)
        self.assertEqual(check.observed, "1.3.5")

    def test_preflight_asserts_runsc_floor(self) -> None:
        """`>= release-20240325.0` (GHSA-4fj4-9m67-3mj3); the latest tag is a
        RECOMMENDATION recorded as an advisory check, not a refusal, and a host
        with no gVisor at all is tier C rather than an unsafe host."""
        for tag, expected in (
            (att.RUNSC_MIN_RELEASE, True),
            (att.RUNSC_PINNED_RELEASE, True),
            ("release-20240326.0", True),
            ("release-20240325.1", True),
            ("release-20240324.9", False),
            ("release-20230101.0", False),
            ("", False),
            ("release-2024032.0", False),
        ):
            with self.subTest(runsc=tag):
                self.assertIs(att.runsc_meets_floor(tag), expected)

        vulnerable = preflight(
            tier_a_outputs({RUNSC: "runsc version release-20240324.9\n"})
        )
        self.assertFalse(vulnerable.passed)
        self.assertIn("runsc_below_floor", vulnerable.failures)

        at_floor = preflight(
            tier_a_outputs({RUNSC: f"runsc version {att.RUNSC_MIN_RELEASE}\n"})
        )
        self.assertTrue(at_floor.passed, at_floor.failures)
        advisory = {c.name: c for c in at_floor.checks}["runsc_pinned_release"]
        self.assertTrue(advisory.advisory)
        self.assertFalse(advisory.passed)
        self.assertNotIn("runsc_below_pinned_release", at_floor.failures)
        self.assertIn(att.RUNSC_PINNED_RELEASE, advisory.required)

        pinned = preflight(tier_a_outputs())
        self.assertTrue({c.name: c for c in pinned.checks}["runsc_pinned_release"].passed)

        # Absent gVisor: no failure, tier C.
        absent = preflight(tier_c_outputs())
        self.assertTrue(absent.passed, absent.failures)
        self.assertEqual(absent.tier, "C")
        self.assertEqual(absent.host_class, att.HOST_CLASS_RUNC)

    def test_isolation_preflight_fails_closed_on_old_runc(self) -> None:
        """An otherwise perfect gVisor host with a vulnerable runc mints
        nothing: the preflight fails, the tier degrades to unclassified and
        `attest_sandbox` raises instead of recording the host."""
        result = preflight(tier_a_outputs({RUNC: "runc version 1.2.0\n"}))
        self.assertFalse(result.passed)
        self.assertEqual(result.failures, ("runc_below_floor",))
        self.assertEqual(result.tier, "0")
        self.assertEqual(result.isolation_mode, "attesting")
        with self.assertRaises(att.IsolationPreflightError) as raised:
            attest(pinned=GVISOR_PIN, preflight=result)
        self.assertEqual(raised.exception.code, "isolation_preflight_failed")
        self.assertIn("runc_below_floor", str(raised.exception))

    def test_every_required_floor_fails_closed_when_unobservable(self) -> None:
        """A host where nothing can be observed fails every required check.

        This is the direction that matters: an absent binary or an
        unreachable daemon must never read as "floor met".
        """
        result = preflight({})
        self.assertFalse(result.passed)
        for code in (
            "docker_info_unavailable",
            "kernel_below_floor",
            "runc_below_floor",
            "unprivileged_userns_disabled",
            "cgroup_not_v2",
            "cgroup_driver_not_systemd",
        ):
            self.assertIn(code, result.failures)
        self.assertEqual(result.runtime, "none")
        self.assertFalse(result.userns)

    def test_kernel_cgroup_and_userns_floors(self) -> None:
        self.assertTrue(att.kernel_meets_floor("5.6"))
        self.assertTrue(att.kernel_meets_floor("6.8.0-45-generic"))
        self.assertFalse(att.kernel_meets_floor("5.4.0-190-generic"))
        self.assertFalse(att.kernel_meets_floor(""))

        old_kernel = preflight(tier_a_outputs({UNAME: "5.4.0-190-generic\n"}))
        self.assertIn("kernel_below_floor", old_kernel.failures)

        v1 = preflight(
            tier_a_outputs({DOCKER_INFO: docker_info_json(CgroupVersion="1")})
        )
        self.assertIn("cgroup_not_v2", v1.failures)

        cgroupfs = preflight(
            tier_a_outputs({DOCKER_INFO: docker_info_json(CgroupDriver="cgroupfs")})
        )
        self.assertIn("cgroup_driver_not_systemd", cgroupfs.failures)

        # Either sysctl proves the fact; neither readable is a failure.
        outputs = tier_a_outputs()
        outputs.pop(USERNS)
        self.assertTrue(preflight(outputs).userns)
        outputs.pop(MAX_USERNS)
        no_userns = preflight(outputs)
        self.assertFalse(no_userns.userns)
        self.assertIn("unprivileged_userns_disabled", no_userns.failures)
        disabled = preflight(tier_a_outputs({USERNS: "0\n", MAX_USERNS: "0\n"}))
        self.assertIn("unprivileged_userns_disabled", disabled.failures)

    def test_containerd_is_recorded_but_never_a_sufficient_pin(self) -> None:
        result = preflight(tier_a_outputs())
        check = {c.name: c for c in result.checks}["containerd_recorded"]
        self.assertTrue(check.advisory)
        self.assertEqual(result.toolchain["containerd"], "2.1.4")

        without = tier_a_outputs()
        without.pop(CONTAINERD)
        missing = preflight(without)
        self.assertTrue(missing.passed, missing.failures)
        self.assertNotIn("containerd_not_observed", missing.failures)
        self.assertEqual(missing.toolchain["containerd"], "")

    def test_tier_derivation_is_a_closed_table(self) -> None:
        self.assertEqual(preflight(tier_a_outputs()).tier, "A")
        self.assertEqual(preflight(tier_c_outputs()).tier, "C")
        hosted = preflight(tier_c_outputs(), host_class="hosted-microvm")
        self.assertEqual(hosted.tier, "B")
        self.assertEqual(hosted.host_class, "hosted-microvm")
        desktop = preflight(
            tier_a_outputs(
                {DOCKER_INFO: docker_info_json(OperatingSystem="Docker Desktop")}
            )
        )
        self.assertEqual(desktop.tier, "D")
        self.assertEqual(desktop.host_class, att.HOST_CLASS_DOCKER_DESKTOP)
        self.assertEqual(
            preflight(tier_a_outputs(), system="Darwin").host_class,
            att.HOST_CLASS_MACOS,
        )
        with self.assertRaises(att.SandboxAttestationError):
            preflight(tier_a_outputs(), host_class="my-laptop")
        for result in (preflight(tier_a_outputs()), preflight(tier_c_outputs())):
            self.assertIn(result.tier, att.SANDBOX_TIERS)
            self.assertIn(result.host_class, att.HOST_CLASSES)

    def test_preflight_is_sealed_and_tamper_evident(self) -> None:
        result = preflight(tier_a_outputs())
        self.assertRegex(result.preflight_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(result.preflight_sha256, att.preflight_digest(result))
        edited = result.model_copy(update={"kernel": "9.9.9"})
        self.assertNotEqual(edited.preflight_sha256, att.preflight_digest(edited))
        with self.assertRaises(att.SandboxAttestationError) as raised:
            attest(pinned=GVISOR_PIN, preflight=edited)
        self.assertEqual(raised.exception.code, "preflight_digest_mismatch")

    def test_no_test_ever_runs_a_real_command(self) -> None:
        """Every observation is injected: the preflight asks a Runner double
        for exactly the recorded argv and starts no process of its own."""
        runner = FakeRunner(tier_a_outputs())
        att.isolation_preflight(
            runner=runner, environ={}, platform_system="Linux"
        )
        self.assertEqual(
            set(runner.calls),
            {DOCKER_INFO, RUNC, RUNSC, CONTAINERD, UNAME, USERNS, MAX_USERNS},
        )
        for call in runner.calls:
            self.assertIn(call[0], {"docker", "runc", "runsc", "containerd", "uname", "sysctl"})


class TestSandboxAttestation(unittest.TestCase):
    """The sealed record itself."""

    def attest(self, *, outputs=None, pin=None, **kwargs):
        result = preflight(tier_a_outputs() if outputs is None else outputs)
        return attest(
            pinned=GVISOR_PIN if pin is None else pin,
            preflight=result,
            **kwargs,
        )

    def test_sandbox_attestation_is_secret_free_and_sealed(self) -> None:
        """No secret may enter the record, and the seal must detect any edit.

        The closed field roster is the real control (extra="forbid" plus the
        exact roadmap Table 9 fields); the secret scan is the assertion that
        proves it, and the digest is taken over the canonical record with the
        digest blanked, exactly as export/certification.py seals its own
        attestations.
        """
        record = self.attest(
            runtime_json_sha256="a" * 64, oci_config_sha256="b" * 64
        )
        self.assertEqual(
            set(type(record).model_fields),
            {
                "attestation_schema_version",
                "tier",
                "host_class",
                "kernel",
                "runtime",
                "platform",
                "cgroup",
                "userns",
                "image_digest",
                "package_digest",
                "runtime_json_sha256",
                "oci_config_sha256",
                "tool_manifest_sha256",
                "diagnostics_version",
                "verifier_code_sha256",
                "loop_limits",
                "sandbox_pin",
                "agents_config_sha256",
                "toolchain",
                "preflight_sha256",
                "contamination_mode",
                "credential_files_in_mount",
                # Schema 1.1 (the Phase 4/5 repair pass): the mount SCAN is
                # recorded, not just its count (finding p5-3), and the record
                # says when and for which run it was minted (finding p5-10).
                "mount_scanned",
                "mount_entries_scanned",
                "attested_at",
                "run_id",
                "attestation_digest",
            },
        )
        self.assertEqual(record.tier, "A")
        self.assertEqual(record.host_class, att.HOST_CLASS_GVISOR)
        self.assertEqual(record.credential_files_in_mount, 0)
        self.assertIs(record.mount_scanned, True)
        self.assertEqual(record.contamination_mode, "enforce")
        self.assertEqual(record.sandbox_pin, GVISOR_PIN)
        self.assertRegex(record.agents_config_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(record.attested_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        # Sealed, and the seal reproduces.
        self.assertRegex(record.attestation_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            record.attestation_digest, att.sandbox_attestation_digest(record)
        )
        self.assertIs(att.verify_sandbox_attestation(record).__class__, type(record))

        # Tampering is detected.
        edited = record.model_copy(update={"tier": "B"})
        with self.assertRaises(att.SandboxAttestationError) as raised:
            att.verify_sandbox_attestation(edited)
        self.assertEqual(raised.exception.code, "attestation_digest_mismatch")

        # Never sealed at all is a refusal, not a pass.
        unsealed = record.model_copy(update={"attestation_digest": ""})
        with self.assertRaises(att.SandboxAttestationError) as raised:
            att.verify_sandbox_attestation(unsealed)
        self.assertEqual(raised.exception.code, "attestation_unsealed")

        # Secret-free: nothing in the record looks like credential material,
        # and a record that did would be refused before it could be sealed.
        att.assert_attestation_is_secret_free(record)
        payload = json.dumps(record.model_dump(mode="json")).casefold()
        for marker in ("password", "secret", "-----begin", "bearer "):
            self.assertNotIn(marker, payload)
        smuggled = record.model_copy(
            update={"kernel": "6.8.0-45-generic password=hunter2"}
        )
        with self.assertRaises(att.SandboxAttestationError) as raised:
            att.assert_attestation_is_secret_free(smuggled)
        self.assertEqual(raised.exception.code, "attestation_secret_material")

        # Every scalar is a bounded, printable observation.
        for name, value in record.model_dump(mode="json").items():
            if isinstance(value, str):
                self.assertEqual(value, value.strip(), name)
                self.assertLessEqual(len(value), 2048, name)

    def test_attestation_is_bound_to_exact_active_agents_config(self) -> None:
        config_a = agents_config_for_pin(GVISOR_PIN)
        config_b = PIN_CONFIG_ROOT / "same-pin-different-config.json"
        config_b.write_text(
            json.dumps(
                {
                    "metrology": {"sandbox": GVISOR_PIN},
                    "test_identity": "different",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        record = self.attest(agents_config=config_a)
        self.assertEqual(
            att.assert_attestation_matches_agents_config(
                record, agents_config=config_a
            ),
            record,
        )
        with self.assertRaises(att.SandboxAttestationError) as raised:
            att.assert_attestation_matches_agents_config(
                record, agents_config=config_b
            )
        self.assertEqual(raised.exception.code, "attestation_config_mismatch")

        # The pin argument cannot contradict the config whose identity is
        # being sealed, even before an active-run comparison.
        with self.assertRaises(att.SandboxPinMismatch):
            self.attest(pin=LAPTOP_PIN, agents_config=config_a)

    def test_attestation_uses_one_agents_config_snapshot(self) -> None:
        """A path rewrite between identity and limits cannot mint mixed A/B."""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "agents.json"
            document_a = yaml.safe_load(
                (Path(__file__).parents[1] / "config" / "agents.yaml").read_text(
                    encoding="utf-8"
                )
            )
            document_a.setdefault("metrology", {})["sandbox"] = dict(GVISOR_PIN)
            document_a["roles"]["independent_implementer"]["session"][
                "max_turns"
            ] = 4
            document_b = json.loads(json.dumps(document_a))
            document_b["roles"]["independent_implementer"]["session"][
                "max_turns"
            ] = 5
            text_a = json.dumps(document_a, sort_keys=True)
            text_b = json.dumps(document_b, sort_keys=True)
            path.write_text(text_a, encoding="utf-8")

            limits_a = att.harness_loop_limits(agents_config=document_a)
            limits_b = att.harness_loop_limits(agents_config=document_b)
            self.assertEqual(limits_a["independent_implementer.max_turns"], 4)
            self.assertEqual(limits_b["independent_implementer.max_turns"], 5)

            original_identity = providers_mod.agents_config_identity

            def rewrite_path_after_identity(*, agents_config=None):
                identity = original_identity(agents_config=agents_config)
                path.write_text(text_b, encoding="utf-8")
                return identity

            with mock.patch.object(
                providers_mod,
                "agents_config_identity",
                side_effect=rewrite_path_after_identity,
            ):
                record = self.attest(agents_config=path)

            # Restore A, as an atomic A -> B -> A race would. The record must
            # be internally all-A and pass the active A comparison.
            path.write_text(text_a, encoding="utf-8")
            self.assertEqual(record.loop_limits, limits_a)
            self.assertEqual(
                record.agents_config_sha256,
                providers_mod.agents_config_sha256(agents_config=document_a),
            )
            self.assertEqual(
                att.assert_attestation_matches_agents_config(
                    record, agents_config=path
                ),
                record,
            )

            # Even a correctly resealed historical mixed record is refused by
            # the active-config check; matching the SHA alone is insufficient.
            mixed = record.model_copy(
                update={"loop_limits": limits_b, "attestation_digest": ""}
            )
            mixed = att.seal_sandbox_attestation(mixed)
            with self.assertRaises(att.SandboxAttestationError) as raised:
                att.assert_attestation_matches_agents_config(
                    mixed, agents_config=path
                )
            self.assertEqual(raised.exception.code, "attestation_config_mismatch")

    def test_explicit_loop_limits_must_equal_config_snapshot(self) -> None:
        config = agents_config_for_pin(GVISOR_PIN)
        expected = att.harness_loop_limits(agents_config=config)
        self.assertEqual(
            self.attest(agents_config=config, loop_limits=expected).loop_limits,
            expected,
        )
        key = next(iter(sorted(expected)))
        mismatched = {**expected, key: expected[key] + 1}
        with self.assertRaises(att.SandboxAttestationError) as raised:
            self.attest(agents_config=config, loop_limits=mismatched)
        self.assertEqual(raised.exception.code, "loop_limits_config_mismatch")
        with self.assertRaises(att.SandboxAttestationError) as raised:
            self.attest(agents_config=config, loop_limits={key: True})
        self.assertEqual(raised.exception.code, "loop_limits_invalid")

    def test_sandbox_attestation_records_no_credential_in_mount(self) -> None:
        """The mount counter matches NAMES and never opens what it counts.

        A clean mount records 0. A mount holding a credential-shaped file
        records it even when the file cannot be read at all — reads are
        disabled for the duration of the scan — which is exactly the property
        that keeps hard rule 2 (never read a credential) and the attestation's
        secret-freedom compatible.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            clean = root / "clean"
            (clean / "elt" / "models").mkdir(parents=True)
            (clean / "config.yaml").write_text("Airbyte: {}\n", encoding="utf-8")
            (clean / "elt" / "models" / "mart.sql").write_text("select 1\n", encoding="utf-8")
            self.assertEqual(att.count_credential_files(clean), 0)
            record = self.attest(mount_root=clean)
            self.assertEqual(record.credential_files_in_mount, 0)

            dirty = root / "dirty"
            (dirty / "nested").mkdir(parents=True)
            for name in (
                destination_contract(Destination.SNOWFLAKE).credential_filename,
                ".env",
            ):
                (dirty / name).write_text("", encoding="utf-8")
            (dirty / "nested" / "service_account.json").write_text("", encoding="utf-8")

            def _no_reads(*args, **kwargs):  # pragma: no cover - must not run
                raise AssertionError("the mount counter opened a file")

            with mock.patch("builtins.open", _no_reads), mock.patch.object(
                Path, "read_bytes", _no_reads
            ), mock.patch.object(Path, "read_text", _no_reads):
                found = att.count_credential_files(dirty)
            self.assertEqual(found, 3)
            dirty_record = self.attest(mount_root=dirty)
            self.assertEqual(dirty_record.credential_files_in_mount, 3)

            # Missing mount counts zero rather than raising.
            self.assertEqual(att.count_credential_files(root / "absent"), 0)
            self.assertEqual(att.count_credential_files(None), 0)

        # Every shipped destination credential file is credential-shaped, so a
        # forgotten one in a mount can never be counted as ordinary content.
        for destination in Destination:
            with self.subTest(destination=destination.value):
                self.assertTrue(
                    att.is_credential_shaped(
                        destination_contract(destination).credential_filename
                    )
                )
        self.assertFalse(att.is_credential_shaped("config.yaml"))
        self.assertFalse(att.is_credential_shaped("mart.sql"))

    def test_credential_names_extend_the_repository_sweep(self) -> None:
        """One name vocabulary, not two: the counter is never more forgiving.

        `review/tools/credential_sweep.NAME_RULES` is the repository's single
        definition of "credential-shaped BY NAME"; every other surface (the
        `runs/` sweep, the model-facing copy in `runtime/model_copy.py`) reads
        it. A second list here would drift, and the half that drifted would be
        exactly the half a mount could smuggle past the counter — a live dbt
        `profiles.yml` or a `terraform.tfstate` counted as ordinary content
        while `credential_files_in_mount` reported 0 and the release gate
        accepted the batch.

        The counter is name-only (it must never open what it counts), so it
        takes the sweep's NAME half and adds the container-lane names the
        sweep has no reason to carry. It is therefore strictly stricter,
        never more permissive, and this test holds that direction.
        """
        from elt_taskgen.review.tools import credential_sweep

        # Everything the shared rules name is counted, including the four the
        # earlier standalone list omitted entirely.
        for name in (
            "profiles.yml",
            "profiles.yaml",
            "terraform.tfstate",
            "terraform.tfstate.backup",
            "terraform.tfvars",
            "prod.auto.tfvars.json",
            ".aws_credentials",
            ".envrc",
            "id_ecdsa",
            "signing.jks",
            "putty.ppk",
            "snowflake_credential.json",
            ".env",
            ".env.local",
            "prod.env",
            ".netrc",
            ".pgpass",
            "server.pem",
            "client.key",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    credential_sweep._name_rule(name),
                    f"{name} is not in the shared vocabulary any more",
                )
                self.assertTrue(att.is_credential_shaped(name))

        # ...and the container-lane additions this module owns.
        for name in (
            "service_account.json",
            "gcp-credential-file.json",
            "kubeconfig",
            "cluster.kubeconfig",
            "id_rsa.pub",
        ):
            with self.subTest(extra=name):
                self.assertTrue(att.is_credential_shaped(name))

        # A path is judged by its BASENAME, so a directory named like a
        # credential cannot mask a file that is not one, or the reverse.
        self.assertTrue(att.is_credential_shaped("nested/dir/.env"))
        self.assertFalse(att.is_credential_shaped(".env/notes.md"))
        for benign in ("dbt_project.yml", "schema.yml", "main.tf", "README.md"):
            with self.subTest(benign=benign):
                self.assertFalse(att.is_credential_shaped(benign))

    def test_macos_runs_are_dev_only_in_attestation(self) -> None:
        """A laptop attests only as the explicitly non-attesting dev-only lane.

        With ELT_TASKGEN_ISOLATION=dev-only a macOS run still produces a
        record — tier D, host class dev-only, isolation mode recorded — which
        the release gate refuses for RLVR labels. WITHOUT that variable the
        same host mints nothing: the preflight fails closed on the platform.
        """
        dev = preflight(
            {}, system="Darwin", environ={att.ISOLATION_ENV: att.DEV_ONLY}
        )
        self.assertEqual(dev.isolation_mode, att.DEV_ONLY)
        self.assertEqual(dev.tier, "D")
        self.assertEqual(dev.host_class, att.HOST_CLASS_DEV_ONLY)
        self.assertFalse(dev.passed)
        self.assertIn("platform_not_linux", dev.failures)
        self.assertEqual(dev.runtime, "none")

        # The repository's committed laptop pin matches this host exactly, so
        # the dev-only lane needs no re-pin to record what it is.
        record = attest(pinned=LAPTOP_PIN, preflight=dev)
        self.assertEqual(record.tier, "D")
        self.assertEqual(record.host_class, att.HOST_CLASS_DEV_ONLY)
        self.assertEqual(record.platform, "Darwin")
        self.assertEqual(record.runtime, "none")
        self.assertNotIn(record.tier, att.ATTESTING_TIERS)
        att.verify_sandbox_attestation(record)

        # Docker Desktop under dev-only is the same lane.
        desktop = preflight(
            tier_a_outputs(
                {DOCKER_INFO: docker_info_json(OperatingSystem="Docker Desktop")}
            ),
            system="Darwin",
            environ={att.ISOLATION_ENV: att.DEV_ONLY},
        )
        self.assertEqual(desktop.tier, "D")
        self.assertEqual(desktop.isolation_mode, att.DEV_ONLY)

        # Without the marker a macOS host is simply unattestable.
        laptop = preflight({}, system="Darwin")
        self.assertEqual(laptop.isolation_mode, "attesting")
        self.assertFalse(laptop.passed)
        with self.assertRaises(att.IsolationPreflightError):
            attest(pinned=LAPTOP_PIN, preflight=laptop)

    def test_attest_sandbox_refuses_observation_that_differs_from_the_pin(self) -> None:
        """R-F: the fingerprint hashes the PIN, so drift is refused, not stamped.

        A run whose observed runtime, image digest or workspace template
        differs from config/agents.yaml metrology.sandbox raises
        SandboxPinMismatch (the CLI convention: ERROR [code], exit 2, nothing
        measured) rather than recording an attestation the digest could not
        tell apart from a pinned one.
        """
        result = preflight(tier_a_outputs())

        with self.assertRaises(att.SandboxPinMismatch) as raised:
            attest(
                pinned={**GVISOR_PIN, "runtime": "runc"},
                preflight=result,
            )
        self.assertEqual(raised.exception.code, "sandbox_pin_mismatch")
        self.assertIn("runtime", str(raised.exception))
        self.assertIn("re-pin", str(raised.exception))

        image = "runner@sha256:" + "c" * 64
        with self.assertRaises(att.SandboxPinMismatch):
            attest(
                pinned=GVISOR_PIN,
                preflight=result,
                image_digest=image,
            )
        with self.assertRaises(att.SandboxPinMismatch):
            attest(
                pinned=GVISOR_PIN,
                preflight=result,
                workspace_template_sha256="d" * 64,
            )
        # A pin whose key set differs is refused before any comparison.
        with self.assertRaises(att.SandboxPinMismatch):
            attest(pinned={"runtime": "runsc"}, preflight=result)
        # A caller-supplied observation that contradicts the preflight is
        # refused too: a record must describe the host that was measured.
        with self.assertRaises(att.SandboxPinMismatch) as raised:
            attest(
                pinned={**GVISOR_PIN, "runtime": "runc"},
                preflight=result,
                observed={**GVISOR_PIN, "runtime": "runc"},
            )
        self.assertIn("contradicts the preflight", str(raised.exception))

        # The matching pin is what succeeds, and the observed values are
        # stamped on the record.
        matched = attest(
            pinned={**GVISOR_PIN, "image_digest": image},
            preflight=result,
            image_digest=image,
        )
        self.assertEqual(matched.image_digest, image)
        self.assertEqual(matched.runtime, "runsc")
        self.assertEqual(
            att.observed_sandbox(result, image_digest=image),
            {
                "runtime": "runsc",
                "image_digest": image,
                "workspace_template_sha256": "",
            },
        )
        # An unpinned image reference is refused outright.
        with self.assertRaises(att.SandboxAttestationError) as raised:
            attest(
                pinned={**GVISOR_PIN, "image_digest": "runner:latest"},
                preflight=result,
                image_digest="runner:latest",
            )
        self.assertEqual(raised.exception.code, "image_digest_unpinned")

    def test_agents_config_sandbox_block_is_the_pinned_declaration_not_an_observation(
        self,
    ) -> None:
        """config/agents.yaml metrology.sandbox holds the PIN only (R-F).

        The three declared keys are a declaration a human wrote; none of the
        observed attestation fields (tier, kernel, cgroup, userns, host class,
        toolchain, preflight hash) appear there, and minting an attestation on
        a completely different host leaves the hashed declaration untouched —
        which is what keeps PROOF 3's cross-process determinism true.
        """
        pin = att.pinned_sandbox_declaration()
        self.assertEqual(pin, providers_mod.sandbox_pin())
        self.assertEqual(
            set(pin), {"runtime", "image_digest", "workspace_template_sha256"}
        )
        self.assertEqual(pin, LAPTOP_PIN)
        before = metrology_mod.sandbox_digest()
        self.assertEqual(before, pin)

        observed_only = {
            "tier",
            "host_class",
            "kernel",
            "platform",
            "cgroup",
            "userns",
            "toolchain",
            "preflight_sha256",
            "package_digest",
            "credential_files_in_mount",
            "contamination_mode",
        }
        self.assertEqual(observed_only & set(pin), set())
        self.assertTrue(observed_only <= set(att.SandboxAttestation.model_fields))

        record = attest(pinned=GVISOR_PIN, preflight=preflight(tier_a_outputs()))
        self.assertEqual(record.tier, "A")
        # Observing a gVisor host did not move the hashed declaration.
        self.assertEqual(metrology_mod.sandbox_digest(), before)
        self.assertEqual(providers_mod.sandbox_pin(), pin)

        # The repository default is the laptop pin, so an attestation minted
        # from it must be the dev-only one; a Tier A host requires a re-pin
        # (and, by construction, a re-earn) before it can attest.
        with self.assertRaises(att.SandboxPinMismatch):
            attest(pinned=pin, preflight=preflight(tier_a_outputs()))

    def test_derived_defaults_are_deterministic_and_secret_free(self) -> None:
        """The fields the harness derives rather than observes.

        They are computed from source bytes, the installed distribution set,
        the projection version, the wire tool surface and the declared loop
        limits — never from anything under a credential path.
        """
        record = att.attest_sandbox(
            pinned=GVISOR_PIN,
            preflight=preflight(tier_a_outputs()),
            mount_root=EMPTY_MOUNT,
            agents_config=agents_config_for_pin(GVISOR_PIN),
        )
        self.assertRegex(record.verifier_code_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(record.package_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(record.tool_manifest_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(record.diagnostics_version, att.default_diagnostics_version())
        self.assertEqual(
            record.verifier_code_sha256, att.default_verifier_code_sha256()
        )
        self.assertEqual(record.contamination_mode, att.default_contamination_mode())
        # Every session block ships disabled, so no integer limit is enforced.
        self.assertEqual(record.loop_limits, att.harness_loop_limits())
        self.assertTrue(
            all(isinstance(value, int) for value in record.loop_limits.values())
        )
        att.verify_sandbox_attestation(record)

    def test_contamination_mode_is_closed_vocabulary(self) -> None:
        for mode in sorted(att.CONTAMINATION_MODES):
            with self.subTest(mode=mode):
                record = self.attest(contamination_mode=mode)
                self.assertEqual(record.contamination_mode, mode)
        with self.assertRaises(att.SandboxAttestationError) as raised:
            self.attest(contamination_mode="lenient")
        self.assertEqual(raised.exception.code, "contamination_mode_unknown")


class TestRepairPassFindings(unittest.TestCase):
    """The Phase 4/5 repair pass: findings p5-0, p5-1, p5-3 and p5-8."""

    def test_runsc_version_unobserved_is_a_failure_not_a_pass(self) -> None:
        """finding p5-0: gVisor whose BUILD cannot be identified is exactly
        the host the floor exists to reject.

        The floor check is two-valued — it reads an unparseable or missing
        `runsc --version` the same way it reads "gVisor is absent" — while the
        TIER is derived from the engine's runtime name. A source build
        (`runsc version devel`) or a runsc binary off the preflight's PATH
        therefore minted a tier A attestation with an EMPTY toolchain entry.
        """
        for label, outputs in (
            ("source build", tier_a_outputs({RUNSC: "runsc version devel\nspec: 1.1.0\n"})),
            ("binary absent", {k: v for k, v in tier_a_outputs().items() if k != RUNSC}),
        ):
            with self.subTest(runsc=label):
                result = preflight(outputs)
                self.assertFalse(result.passed, result.failures)
                self.assertIn("runsc_version_unobserved", result.failures)
                self.assertEqual(result.tier, "0")
                self.assertEqual(result.toolchain["runsc"], "")
                with self.assertRaises(att.IsolationPreflightError):
                    attest(pinned=GVISOR_PIN, preflight=result)
        # A parseable version at the floor still reaches tier A.
        good = preflight(tier_a_outputs())
        self.assertTrue(good.passed, good.failures)
        self.assertEqual(good.tier, "A")
        # ...and a host with NO gVisor at all is untouched: tier C, no failure.
        plain = preflight(tier_c_outputs())
        self.assertTrue(plain.passed, plain.failures)
        self.assertEqual(plain.tier, "C")
        self.assertNotIn("runsc_version_unobserved", plain.failures)

    def test_declared_host_class_cannot_demote_observed_isolation_evidence(
        self,
    ) -> None:
        """finding p5-1: an env string is not a substitute for an observation.

        `ELT_TASKGEN_SANDBOX_HOST_CLASS` demoted the docker-info, cgroup-v2 and
        cgroup-driver checks to advisory and jumped the tier ladder to B before
        the runtime was ever looked at, so a cgroup v1 host — and even a host
        with no container runtime at all — passed the preflight at tier B and
        minted a label-bearing attestation.
        """
        environ = {att.SANDBOX_HOST_CLASS_ENV: "hosted-sandbox"}
        # (a) The engine ANSWERED and reported cgroup v1: an observation.
        v1 = preflight(
            tier_c_outputs(
                {
                    DOCKER_INFO: docker_info_json(
                        Runtimes={"runc": {"path": "runc"}},
                        DefaultRuntime="runc",
                        CgroupVersion="1",
                        CgroupDriver="cgroupfs",
                    )
                }
            ),
            environ=environ,
        )
        self.assertFalse(v1.passed, v1.failures)
        self.assertIn("cgroup_not_v2", v1.failures)
        self.assertIn("cgroup_driver_not_systemd", v1.failures)
        self.assertEqual(v1.tier, "0")
        # (b) No engine at all: the declaration is uncorroborated.
        bare = preflight(
            {
                RUNC: "runc version 1.4.3\n",
                UNAME: "6.8.0-45-generic\n",
                USERNS: "1\n",
                MAX_USERNS: "63594\n",
            },
            environ=environ,
        )
        self.assertFalse(bare.passed, bare.failures)
        self.assertIn("declared_host_class_uncorroborated", bare.failures)
        self.assertEqual(bare.runtime, "none")
        # (c) The legitimate hosted lane still works: a declared class beside a
        #     real engine reporting cgroup v2 + systemd is tier B.
        hosted = preflight(tier_c_outputs(), environ=environ)
        self.assertTrue(hosted.passed, hosted.failures)
        self.assertEqual(hosted.tier, "B")
        self.assertEqual(hosted.host_class, "hosted-sandbox")

    def test_attest_sandbox_requires_a_mount_to_scan(self) -> None:
        """finding p5-3: `credential_files_in_mount == 0` must mean a scan ran.

        The counter answered 0 for a missing argument, a typo'd path and a
        non-directory alike, so A22 control 5 read as proven without anything
        ever being walked — indistinguishable in the sealed record from a scan
        that found nothing.
        """
        result = preflight(tier_a_outputs())
        with self.assertRaises(TypeError):
            att.attest_sandbox(pinned=GVISOR_PIN, preflight=result, **CHEAP_DERIVED)
        for missing in (None, "/no/such/mount/at/all", __file__):
            with self.subTest(mount_root=missing):
                with self.assertRaises(att.SandboxAttestationError) as raised:
                    attest(pinned=GVISOR_PIN, preflight=result, mount_root=missing)
                self.assertEqual(raised.exception.code, "mount_root_missing")
        # A real (empty) directory is scanned, and the record says so.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("public\n", encoding="utf-8")
            record = attest(pinned=GVISOR_PIN, preflight=result, mount_root=root)
        self.assertIs(record.mount_scanned, True)
        self.assertEqual(record.mount_entries_scanned, 1)
        self.assertEqual(record.credential_files_in_mount, 0)
        # The scan helper itself distinguishes the three cases.
        self.assertIs(att.scan_mount_for_credentials(None).scanned, False)
        self.assertIs(att.scan_mount_for_credentials("/no/such/tree").scanned, False)

    def test_digest_fields_refuse_free_text(self) -> None:
        """finding p5-8: the closed roster is only a control if the fields in
        it have closed SHAPES.

        `assert_attestation_is_secret_free` is a keyword denylist, and an
        opaque high-entropy token carries none of those keywords, so four
        fields of the roster were an open channel for secret material into a
        sealed, published record.
        """
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        for field, value, code in (
            ("package_digest", secret, "digest_malformed"),
            ("tool_manifest_sha256", secret, "digest_malformed"),
            ("verifier_code_sha256", "eyJhbGciOiJIUzI1NiJ9.payload.sig", "digest_malformed"),
            ("diagnostics_version", "xoxb-1234-abcdefg", "diagnostics_version_malformed"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(att.SandboxAttestationError) as raised:
                    self._attest(**{field: value})
                self.assertEqual(raised.exception.code, code)
        # Empty stays legal (a pin legitimately leaves these blank), and a real
        # digest is accepted.
        record = self._attest(package_digest="", tool_manifest_sha256="a" * 64)
        self.assertEqual(record.tool_manifest_sha256, "a" * 64)

    def test_attestation_records_when_and_for_which_run(self) -> None:
        """finding p5-10: an isolation record is evidence about a moment."""
        record = self._attest(
            attested_at="2026-09-04T12:00:00Z", run_id="release-2026-09-04"
        )
        self.assertEqual(record.attested_at, "2026-09-04T12:00:00Z")
        self.assertEqual(record.run_id, "release-2026-09-04")
        att.verify_sandbox_attestation(record)
        for field, value, code in (
            ("attested_at", "yesterday", "attested_at_malformed"),
            ("run_id", "a" * 200, "run_id_malformed"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(att.SandboxAttestationError) as raised:
                    self._attest(**{field: value})
                self.assertEqual(raised.exception.code, code)

    def _attest(self, **kwargs):
        return attest(
            pinned=GVISOR_PIN, preflight=preflight(tier_a_outputs()), **kwargs
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

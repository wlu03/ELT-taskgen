"""Tests for export/attestation_gate.py (roadmap Phase 5, Table 9).

WHY THIS EXISTS
A batch of RLVR labels is only as trustworthy as the isolation it was produced
under. The gate is the single place that says so:

  * a batch claiming RLVR labels with NO attestation is refused, and
    ``release.require_attestation: false`` cannot exempt it — the setting is
    scoped to batches that claim no labels, which is the whole point of the
    scoping;
  * tier C (plain runc) and tier D (macOS, Docker Desktop, the ``dev-only``
    laptop lane) are refused for label-claiming batches;
  * contamination modes ``observe`` and ``off`` are refused (they record an
    overlap without refusing any), and the running mode must equal the attested
    one;
  * a mount that carried a credential-shaped file is refused
    (``credential_files_in_mount`` must be 0);
  * a record that is unsealed, tampered with or not an attestation at all is
    refused rather than trusted.

Every refusal is typed and carries a code from the closed
``REFUSAL_CODES`` vocabulary, so a caller can report ``ERROR [code]`` and exit
2 the way the rest of the harness reports a fail-closed refusal.

The attestations here are built by the offline doubles in
tests/test_sandbox_attestation.py (imported as a module the way
test_certification imports test_export, so its own tests are not re-collected):
no command runs, no Docker is invoked and no cloud is touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    import test_export as export_fixture
except ModuleNotFoundError:  # package-style invocation
    from tests import test_export as export_fixture

try:
    import test_sandbox_attestation as sandbox_fixture
except ModuleNotFoundError:  # package-style invocation
    from tests import test_sandbox_attestation as sandbox_fixture

from elt_taskgen.export import release as release_mod
from elt_taskgen.export.attestation_gate import (
    REFUSAL_CODES,
    RELEASE_REQUIRE_ATTESTATION_DEFAULT,
    REQUIRED_CONTAMINATION_MODE,
    AttestationRefusal,
    batch_claims_rlvr_labels,
    recompute_attestation_digest,
    require_attestation_for_labels,
    require_attestation_setting,
)
from elt_taskgen.provenance import INGEST_PROVENANCE_DIRNAME
from elt_taskgen.models import (
    DifficultyMeasurement,
    EmpiricalDifficulty,
    RLVR_TASK_VARIANTS,
    SolverTierResult,
    TaskVariant,
    VariantCalibration,
    canonical_json,
    solver_roster_fingerprint,
)
from elt_taskgen.runtime import attestation as att
from elt_taskgen.verification.contamination import Enforcement


def attestation(
    *,
    tier: str = "A",
    mode: str = "enforce",
    mount_root=None,
    host_class: str | None = None,
    **extra,
) -> att.SandboxAttestation:
    """One sealed attestation at the requested tier, built from a host double."""

    if tier == "A":
        result = sandbox_fixture.preflight(sandbox_fixture.tier_a_outputs())
        pin = sandbox_fixture.GVISOR_PIN
    elif tier == "B":
        result = sandbox_fixture.preflight(
            sandbox_fixture.tier_c_outputs(), host_class="hosted-microvm"
        )
        pin = {**sandbox_fixture.GVISOR_PIN, "runtime": "runc"}
    elif tier == "C":
        result = sandbox_fixture.preflight(sandbox_fixture.tier_c_outputs())
        pin = {**sandbox_fixture.GVISOR_PIN, "runtime": "runc"}
    elif tier == "D":
        result = sandbox_fixture.preflight(
            {},
            system="Darwin",
            environ={att.ISOLATION_ENV: att.DEV_ONLY},
        )
        pin = sandbox_fixture.LAPTOP_PIN
    else:  # pragma: no cover - the table above is the closed set used here
        raise AssertionError(f"no host double for tier {tier!r}")
    derived = dict(sandbox_fixture.CHEAP_DERIVED)
    derived["contamination_mode"] = mode
    # `attest_sandbox` REQUIRES a mount to scan (finding p5-3); an empty
    # shared directory stands in for the ones the caller does not care about.
    derived.update(extra)
    agents_config = derived.pop(
        "agents_config", sandbox_fixture.agents_config_for_pin(pin)
    )
    record = att.attest_sandbox(
        pinned=pin,
        preflight=result,
        mount_root=sandbox_fixture.EMPTY_MOUNT if mount_root is None else mount_root,
        agents_config=agents_config,
        **derived,
    )
    assert record.tier == tier, (record.tier, tier)
    if host_class is not None:
        assert record.host_class == host_class
    return record


def manifest(**overrides) -> release_mod.ReleaseManifest:
    """A minimal, in-memory ReleaseManifest (never a frozen tree on disk)."""

    fields = dict(
        release_id="rel-0001",
        tasks={"t1": "a" * 64},
        splits={"t1": "train"},
        families={"t1": "fam"},
        licenses={"t1": "CC0-1.0"},
        checksums={"public/t1/config.yaml": "b" * 64},
        scorer_version="1.0.0",
        generator_version="1.0.0",
    )
    fields.update(overrides)
    return release_mod.ReleaseManifest(**fields)


def labelled_manifest(**overrides) -> release_mod.ReleaseManifest:
    return manifest(
        corpus_profile=release_mod.COMBINED_CORPUS_PROFILE,
        public_layout=release_mod.COMBINED_PUBLIC_LAYOUT,
        variants={"t1": tuple(v.value for v in RLVR_TASK_VARIANTS)},
        **overrides,
    )


class TestBatchClaims(unittest.TestCase):
    """What counts as a batch claiming RLVR labels."""

    def test_batch_claims_rlvr_labels_reads_manifests_and_fails_closed(self) -> None:
        self.assertTrue(batch_claims_rlvr_labels(labelled_manifest()))
        self.assertFalse(batch_claims_rlvr_labels(manifest()))
        # The shipped EL/T variants alone are enough, whatever the profile.
        self.assertTrue(
            batch_claims_rlvr_labels(
                manifest(variants={"t1": tuple(v.value for v in RLVR_TASK_VARIANTS)})
            )
        )
        # Mappings read the same way as models.
        self.assertTrue(
            batch_claims_rlvr_labels(
                {"corpus_profile": release_mod.COMBINED_CORPUS_PROFILE}
            )
        )
        self.assertFalse(batch_claims_rlvr_labels({"corpus_profile": "legacy_full"}))
        # An explicit declaration may only ever make the answer STRICTER
        # (finding p5-2): `true` claims labels a profile alone would not...
        self.assertTrue(
            batch_claims_rlvr_labels(
                {"corpus_profile": "legacy_full", "claims_rlvr_labels": True}
            )
        )
        # ...`false` is honoured when nothing in the same manifest
        # contradicts it...
        self.assertFalse(
            batch_claims_rlvr_labels(
                {"corpus_profile": "legacy_full", "claims_rlvr_labels": False}
            )
        )
        # ...and CANNOT override the batch's own RLVR evidence. Anyone who can
        # write the release directory can add one key to manifest.json; that
        # must not carry a batch of RLVR variants past the tier, contamination
        # and credential refusals.
        for contradicting in (
            {
                "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
                "claims_rlvr_labels": False,
            },
            {
                "claims_rlvr_labels": False,
                "variants": {"t1": tuple(v.value for v in RLVR_TASK_VARIANTS)},
            },
            {"claims_rlvr_labels": False, "variant_acceptance": {"el": [1, 2, 3]}},
        ):
            with self.subTest(manifest=sorted(contradicting)):
                with self.assertRaises(AttestationRefusal) as raised:
                    batch_claims_rlvr_labels(contradicting)
                self.assertEqual(raised.exception.code, "batch_manifest_invalid")
        # An uninterpretable shape claims labels (fail closed), and a
        # non-boolean declaration is a typed refusal, never a truthy read.
        self.assertTrue(batch_claims_rlvr_labels({}))
        self.assertTrue(batch_claims_rlvr_labels({"corpus_profile": "something_new"}))
        with self.assertRaises(AttestationRefusal) as raised:
            batch_claims_rlvr_labels({"claims_rlvr_labels": "yes"})
        self.assertEqual(raised.exception.code, "batch_manifest_invalid")
        with self.assertRaises(AttestationRefusal) as raised:
            batch_claims_rlvr_labels(None)
        self.assertEqual(raised.exception.code, "batch_manifest_invalid")

    def test_corpus_profile_constants_match_export_release(self) -> None:
        """The gate keeps its own literals (so export/release.py can call it
        without an import cycle); this pins them to the real constants."""
        from elt_taskgen.export import attestation_gate

        self.assertEqual(
            attestation_gate._RLVR_CORPUS_PROFILE,
            release_mod.COMBINED_CORPUS_PROFILE,
        )
        self.assertEqual(
            attestation_gate._LEGACY_CORPUS_PROFILE,
            release_mod.ReleaseManifest.model_fields["corpus_profile"].default,
        )
        self.assertEqual(
            attestation_gate._RLVR_VARIANT_VALUES,
            frozenset(v.value for v in RLVR_TASK_VARIANTS),
        )


class TestRequireAttestationSetting(unittest.TestCase):
    def test_default_is_true_and_config_is_read(self) -> None:
        self.assertTrue(RELEASE_REQUIRE_ATTESTATION_DEFAULT)
        # The repository config states nothing today, so the default applies.
        self.assertIs(require_attestation_setting(), True)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            off = root / "off.yaml"
            off.write_text("release:\n  require_attestation: false\n", encoding="utf-8")
            self.assertIs(require_attestation_setting(config=off), False)
            on = root / "on.yaml"
            on.write_text("release:\n  require_attestation: true\n", encoding="utf-8")
            self.assertIs(require_attestation_setting(config=on), True)
            silent = root / "silent.yaml"
            silent.write_text("metrology: {}\n", encoding="utf-8")
            self.assertIs(require_attestation_setting(config=silent), True)
            bad = root / "bad.yaml"
            bad.write_text("release:\n  require_attestation: maybe\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                require_attestation_setting(config=bad)
            with self.assertRaises(FileNotFoundError):
                require_attestation_setting(config=root / "absent.yaml")


class TestAttestationGate(unittest.TestCase):
    """The refusals themselves."""

    def test_release_refuses_tier_c_and_d_labels(self) -> None:
        """Only tiers A and B may back RLVR labels.

        Tier C is plain runc and tier D is the laptop lane; both are honest
        records of real isolation, and both are refused the moment a batch
        claims labels. The same records are fine for a batch that claims none.
        """
        batch = labelled_manifest()
        for tier in ("C", "D"):
            with self.subTest(tier=tier):
                record = attestation(tier=tier)
                with self.assertRaises(AttestationRefusal) as raised:
                    require_attestation_for_labels(
                        batch, record, contamination_mode="enforce"
                    )
                self.assertEqual(raised.exception.code, "attestation_tier_refused")
                self.assertIn(tier, str(raised.exception))
                self.assertNotIn(record.tier, att.ATTESTING_TIERS)

        # Tiers A and B pass, and the decision names what it accepted.
        for tier in ("A", "B"):
            with self.subTest(tier=tier):
                decision = require_attestation_for_labels(
                    batch, attestation(tier=tier), contamination_mode="enforce"
                )
                self.assertTrue(decision.claims_rlvr_labels)
                self.assertTrue(decision.required)
                self.assertEqual(decision.tier, tier)
                self.assertRegex(decision.attestation_digest, r"^[0-9a-f]{64}$")
                self.assertIn(tier, decision.reason)

        # The tier-D dev-only record is exactly what a laptop batch that
        # claims no labels may still ship with.
        unlabelled = require_attestation_for_labels(
            manifest(), attestation(tier="D"), contamination_mode="enforce"
        )
        self.assertFalse(unlabelled.claims_rlvr_labels)
        self.assertEqual(unlabelled.tier, "D")

    def test_release_refuses_observe_mode_attestation(self) -> None:
        """`observe` and `off` record overlaps without refusing any, so neither
        may stand behind a label; the running mode must also equal the attested
        one, or the record describes a different run."""
        batch = labelled_manifest()
        for mode in ("observe", "off"):
            with self.subTest(mode=mode):
                with self.assertRaises(AttestationRefusal) as raised:
                    require_attestation_for_labels(
                        batch, attestation(mode=mode), contamination_mode=mode
                    )
                self.assertEqual(raised.exception.code, "contamination_mode_refused")
                self.assertIn(mode, str(raised.exception))

        # A record that says enforce while the run is in observe (or the other
        # way around) is a mismatch, not a pass.
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, attestation(mode="enforce"), contamination_mode="observe"
            )
        self.assertEqual(raised.exception.code, "contamination_mode_mismatch")
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, attestation(mode="observe"), contamination_mode="enforce"
            )
        self.assertEqual(raised.exception.code, "contamination_mode_mismatch")

        # An unknown mode is refused before anything else is judged.
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, attestation(), contamination_mode="lenient"
            )
        self.assertEqual(raised.exception.code, "contamination_mode_unknown")

        # The enforcement enum is accepted directly, as the caller holds it.
        decision = require_attestation_for_labels(
            batch, attestation(), contamination_mode=Enforcement.ENFORCE
        )
        self.assertEqual(decision.contamination_mode, REQUIRED_CONTAMINATION_MODE)

    def test_labels_can_never_skip_the_gate_however_the_setting_is_set(self) -> None:
        """`release.require_attestation` is scoped: it can only relax a batch
        that claims no labels."""
        with tempfile.TemporaryDirectory() as raw:
            off = Path(raw) / "off.yaml"
            off.write_text("release:\n  require_attestation: false\n", encoding="utf-8")

            with self.assertRaises(AttestationRefusal) as raised:
                require_attestation_for_labels(
                    labelled_manifest(),
                    None,
                    contamination_mode="enforce",
                    config=off,
                )
            self.assertEqual(raised.exception.code, "attestation_missing")
            self.assertIn("cannot exempt", str(raised.exception))

            # The same setting DOES relax a batch that claims no labels.
            decision = require_attestation_for_labels(
                manifest(), None, contamination_mode="enforce", config=off
            )
            self.assertFalse(decision.required)
            self.assertFalse(decision.claims_rlvr_labels)

            # With the default (true) even an unlabelled batch needs one.
            with self.assertRaises(AttestationRefusal) as raised:
                require_attestation_for_labels(
                    manifest(), None, contamination_mode="enforce"
                )
            self.assertEqual(raised.exception.code, "attestation_missing")

    def test_unsealed_tampered_and_alien_records_are_refused(self) -> None:
        batch = labelled_manifest()
        record = attestation()

        tampered = record.model_copy(update={"tier": "A", "kernel": "9.9.9"})
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, tampered, contamination_mode="enforce"
            )
        self.assertEqual(raised.exception.code, "attestation_digest_mismatch")

        unsealed = record.model_copy(update={"attestation_digest": ""})
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, unsealed, contamination_mode="enforce"
            )
        self.assertEqual(raised.exception.code, "attestation_unsealed")

        without_preflight = att.seal_sandbox_attestation(
            record.model_copy(update={"preflight_sha256": ""})
        )
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, without_preflight, contamination_mode="enforce"
            )
        self.assertEqual(raised.exception.code, "attestation_preflight_missing")

        smuggled = att.seal_sandbox_attestation(
            record.model_copy(update={"kernel": "6.8.0 password=hunter2"})
        )
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(batch, smuggled, contamination_mode="enforce")
        self.assertEqual(raised.exception.code, "attestation_secret_material")

        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                batch, {"tier": "A"}, contamination_mode="enforce"
            )
        self.assertEqual(raised.exception.code, "attestation_invalid")

    def test_credential_in_the_mount_refuses_labels(self) -> None:
        """A22 control 5: the attested mount must have held no credential."""
        with tempfile.TemporaryDirectory() as raw:
            mount = Path(raw) / "mount"
            mount.mkdir()
            (mount / "snowflake_credential.json").write_text("", encoding="utf-8")
            record = attestation(mount_root=mount)
            self.assertEqual(record.credential_files_in_mount, 1)
            with self.assertRaises(AttestationRefusal) as raised:
                require_attestation_for_labels(
                    labelled_manifest(), record, contamination_mode="enforce"
                )
            self.assertEqual(raised.exception.code, "credential_files_in_mount")

            clean = Path(raw) / "clean"
            clean.mkdir()
            (clean / "config.yaml").write_text("Airbyte: {}\n", encoding="utf-8")
            ok = require_attestation_for_labels(
                labelled_manifest(),
                attestation(mount_root=clean),
                contamination_mode="enforce",
            )
            self.assertEqual(ok.tier, "A")

    def test_recompute_attestation_digest_matches_the_seal(self) -> None:
        """`verify_release`'s half: re-derive the digest from the record alone."""
        record = attestation()
        self.assertEqual(recompute_attestation_digest(record), record.attestation_digest)
        edited = record.model_copy(update={"host_class": att.HOST_CLASS_RUNC})
        self.assertNotEqual(
            recompute_attestation_digest(edited), edited.attestation_digest
        )
        with self.assertRaises(AttestationRefusal) as raised:
            recompute_attestation_digest(None)
        self.assertEqual(raised.exception.code, "attestation_invalid")

    def test_legacy_config_unbound_record_is_dev_readable_but_cannot_label(self) -> None:
        current = attestation()
        payload = current.model_dump(mode="python")
        payload.pop("sandbox_pin")
        payload.pop("agents_config_sha256")
        payload["attestation_schema_version"] = (
            att.LEGACY_SANDBOX_ATTESTATION_SCHEMA_VERSION
        )
        payload["attestation_digest"] = ""
        legacy = att.seal_sandbox_attestation(
            att.SandboxAttestation.model_validate(payload)
        )

        # Generic verification and an unlabelled reader retain compatibility.
        att.verify_sandbox_attestation(legacy)
        decision = require_attestation_for_labels(
            manifest(), legacy, contamination_mode="enforce"
        )
        self.assertFalse(decision.claims_rlvr_labels)

        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(), legacy, contamination_mode="enforce"
            )
        self.assertEqual(raised.exception.code, "attestation_config_unbound")

    def test_every_refusal_code_is_in_the_closed_vocabulary(self) -> None:
        with self.assertRaises(ValueError):
            AttestationRefusal("nope", code="invented_code")
        self.assertIn("attestation_missing", REFUSAL_CODES)
        # Every code the gate can raise is declared.
        for code in (
            "attestation_tier_refused",
            "contamination_mode_refused",
            "contamination_mode_mismatch",
            "credential_files_in_mount",
            "attestation_digest_mismatch",
            "attestation_unsealed",
            "attestation_invalid",
            "attestation_preflight_missing",
            "attestation_secret_material",
            "batch_manifest_invalid",
            "contamination_mode_unknown",
            "mount_not_scanned",
            "attestation_tier_uncorroborated",
            "attestation_stale",
            "attestation_run_mismatch",
            "attestation_config_mismatch",
            "attestation_config_unbound",
        ):
            self.assertIn(code, REFUSAL_CODES)


class TestRepairPassGateFindings(unittest.TestCase):
    """The Phase 4/5 repair pass: findings p5-1, p5-3 and p5-10 on the gate."""

    def test_gate_refuses_an_attestation_that_scanned_no_mount(self) -> None:
        """finding p5-3, read side: a record with no scan behind its
        `credential_files_in_mount == 0` is not evidence of a clean mount."""
        record = attestation()
        unscanned = att.seal_sandbox_attestation(
            record.model_copy(
                update={"mount_scanned": False, "attestation_digest": ""}
            )
        )
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(),
                unscanned,
                contamination_mode=Enforcement.ENFORCE,
            )
        self.assertEqual(raised.exception.code, "mount_not_scanned")

    def test_gate_refuses_an_uncorroborated_tier_b_record(self) -> None:
        """finding p5-1, read side: tier B rests on an operator DECLARATION, so
        it may back labels only beside real isolation evidence."""
        record = attestation(tier="B", host_class="hosted-microvm")
        bare = att.seal_sandbox_attestation(
            record.model_copy(
                update={"runtime": "none", "cgroup": "", "attestation_digest": ""}
            )
        )
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(), bare, contamination_mode=Enforcement.ENFORCE
            )
        self.assertEqual(raised.exception.code, "attestation_tier_uncorroborated")
        # The corroborated tier B record still admits.
        self.assertTrue(
            require_attestation_for_labels(
                labelled_manifest(), record, contamination_mode=Enforcement.ENFORCE
            ).claims_rlvr_labels
        )

    def test_gate_binds_the_attestation_to_its_run_and_refuses_a_stale_one(
        self,
    ) -> None:
        """finding p5-10: one tier A record must not replay indefinitely."""
        stamped = attestation(attested_at="2026-01-01T00:00:00Z", run_id="run-a")
        # (a) Bound to its own run.
        result = require_attestation_for_labels(
            labelled_manifest(),
            stamped,
            contamination_mode=Enforcement.ENFORCE,
            run_id="run-a",
            now="2026-01-02T00:00:00Z",
        )
        self.assertTrue(result.claims_rlvr_labels)
        # (b) Not to another run.
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(),
                stamped,
                contamination_mode=Enforcement.ENFORCE,
                run_id="run-b",
                now="2026-01-02T00:00:00Z",
            )
        self.assertEqual(raised.exception.code, "attestation_run_mismatch")
        # (c) A record naming no run at all cannot be bound to one.
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(),
                attestation(),
                contamination_mode=Enforcement.ENFORCE,
                run_id="run-a",
            )
        self.assertEqual(raised.exception.code, "attestation_run_mismatch")
        # (d) A year later the same sealed record is stale, not silently valid.
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(),
                stamped,
                contamination_mode=Enforcement.ENFORCE,
                now="2027-01-01T00:00:00Z",
            )
        self.assertEqual(raised.exception.code, "attestation_stale")
        # (e) A record with no timestamp is a typed refusal, never a pass.
        undated = att.seal_sandbox_attestation(
            attestation().model_copy(
                update={"attested_at": "", "attestation_digest": ""}
            )
        )
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(), undated, contamination_mode=Enforcement.ENFORCE
            )
        self.assertEqual(raised.exception.code, "attestation_stale")
        # (f) A future timestamp cannot extend the freshness window forward.
        future = attestation(attested_at="2027-01-01T00:00:00Z")
        with self.assertRaises(AttestationRefusal) as raised:
            require_attestation_for_labels(
                labelled_manifest(),
                future,
                contamination_mode=Enforcement.ENFORCE,
                now="2026-01-01T00:00:00Z",
            )
        self.assertEqual(raised.exception.code, "attestation_stale")


class TestCertifiedReleaseIntegration(unittest.TestCase):
    """The release freezer is the production caller of the attestation gate."""

    def setUp(self) -> None:
        self.harness = export_fixture.TestFreezeRelease("freeze")
        self.harness.setUp()
        self.harness.publish_source_provenance()
        self.addCleanup(self.harness.doCleanups)

    def write_empirical_difficulty(self) -> DifficultyMeasurement:
        task = self.harness.task
        content_hash = task.content_hash()
        model_key = "fixture:solver"
        calibrations = {
            TaskVariant.EXTRACT_LOAD: VariantCalibration(
                variant=TaskVariant.EXTRACT_LOAD,
                tiers=(
                    SolverTierResult(
                        model_key=model_key,
                        k=1,
                        successes=0,
                        stage1_failures=1,
                    ),
                ),
            ),
            TaskVariant.TRANSFORM: VariantCalibration(
                variant=TaskVariant.TRANSFORM,
                tiers=(
                    SolverTierResult(
                        model_key=model_key,
                        k=1,
                        successes=0,
                        stage2_failures=1,
                    ),
                ),
            ),
        }
        empirical = EmpiricalDifficulty(
            solver_config="fixture roster",
            n_attempts=2,
            success_rate=0.0,
            stage1_failure_rate=0.5,
            stage2_failure_rate=0.5,
            variants=calibrations,
            roster_fingerprint=solver_roster_fingerprint((model_key,)),
            campaign_fingerprint="1" * 64,
            measured_at_content_hash=content_hash,
        )
        measurement = DifficultyMeasurement(
            task_id=task.task_id,
            task_content_hash=content_hash,
            load_score=0.5,
            transform_score=0.5,
            empirical=empirical,
        )
        path = self.harness.task_root / release_mod.DIFFICULTY_REPORT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            canonical_json(measurement.model_dump(mode="json")),
            encoding="utf-8",
        )
        return measurement

    def freeze_certified(self):
        measurement = self.write_empirical_difficulty()
        assert measurement.empirical is not None
        agents_config = sandbox_fixture.agents_config_for_pin(
            sandbox_fixture.GVISOR_PIN
        )
        return release_mod.freeze_release(
            self.harness.engine,
            self.harness.selection,
            self.harness.out_dir,
            release_mode=release_mod.CERTIFIED_RELEASE_MODE,
            sandbox_attestation=attestation(agents_config=agents_config),
            agents_config=agents_config,
            expected_campaign_fingerprint=(
                measurement.empirical.campaign_fingerprint
            ),
        )

    def test_development_release_is_explicit_and_backwards_compatible(self) -> None:
        # This class normally provisions source evidence for certified tests;
        # remove it to exercise the explicit legacy/local development path.
        store = self.harness.task_root / INGEST_PROVENANCE_DIRNAME
        for path in store.iterdir():
            path.chmod(0o644)
            path.unlink()
        store.rmdir()
        manifest = self.harness.freeze()
        self.assertEqual(
            manifest.release_mode, release_mod.DEVELOPMENT_RELEASE_MODE
        )
        self.assertEqual(manifest.sandbox_attestation_digest, "")
        self.assertEqual(manifest.difficulty_measurements, {})
        self.assertEqual(manifest.source_provenance, {})
        self.assertEqual(manifest.source_provenance_digests, {})
        self.assertNotIn(
            release_mod.SANDBOX_ATTESTATION_FILENAME, manifest.checksums
        )
        self.assertTrue(release_mod.verify_release(self.harness.out_dir).ok)

    def test_development_release_can_bind_structural_only_difficulty(self) -> None:
        task = self.harness.task
        measurement = DifficultyMeasurement(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            load_score=0.4,
            transform_score=0.6,
        )
        path = self.harness.task_root / release_mod.DIFFICULTY_REPORT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            canonical_json(measurement.model_dump(mode="json")),
            encoding="utf-8",
        )
        manifest = self.harness.freeze()
        self.assertIn(task.task_id, manifest.difficulty_measurements)
        verification = release_mod.verify_release(self.harness.out_dir)
        self.assertTrue(verification.ok, verification.failures)

    def test_certified_freeze_fails_closed_without_attestation(self) -> None:
        with self.assertRaises(AttestationRefusal) as raised:
            release_mod.freeze_release(
                self.harness.engine,
                self.harness.selection,
                self.harness.out_dir,
                release_mode=release_mod.CERTIFIED_RELEASE_MODE,
            )
        self.assertEqual(raised.exception.code, "attestation_missing")
        self.assertFalse(self.harness.out_dir.exists())

    def test_certified_freeze_fails_closed_without_source_provenance(self) -> None:
        store = self.harness.task_root / INGEST_PROVENANCE_DIRNAME
        for path in store.iterdir():
            path.chmod(0o644)
            path.unlink()
        store.rmdir()
        with self.assertRaisesRegex(
            ValueError,
            "re-ingest it through the typed five-source manifest coordinator",
        ):
            self.freeze_certified()
        self.assertFalse(self.harness.out_dir.exists())
        self.assertFalse(self.harness.out_dir.parent.exists())

    def test_certified_freeze_rejects_attestation_from_another_agents_config(
        self,
    ) -> None:
        root = self.harness.out_dir.parent
        root.mkdir(parents=True, exist_ok=True)
        config_a = root / "agents-a.json"
        config_b = root / "agents-b.json"
        base = {"metrology": {"sandbox": sandbox_fixture.GVISOR_PIN}}
        config_a.write_text(
            json.dumps({**base, "test_identity": "A"}), encoding="utf-8"
        )
        config_b.write_text(
            json.dumps({**base, "test_identity": "B"}), encoding="utf-8"
        )
        record = attestation(agents_config=config_a)

        with self.assertRaises(AttestationRefusal) as raised:
            release_mod.freeze_release(
                self.harness.engine,
                self.harness.selection,
                self.harness.out_dir,
                release_mode=release_mod.CERTIFIED_RELEASE_MODE,
                sandbox_attestation=record,
                agents_config=config_b,
            )
        self.assertEqual(raised.exception.code, "attestation_config_mismatch")
        self.assertFalse(self.harness.out_dir.exists())

    def test_certified_freeze_requires_empirical_difficulty_for_every_task(
        self,
    ) -> None:
        agents_config = sandbox_fixture.agents_config_for_pin(
            sandbox_fixture.GVISOR_PIN
        )
        with self.assertRaisesRegex(ValueError, "no empirical difficulty evidence"):
            release_mod.freeze_release(
                self.harness.engine,
                self.harness.selection,
                self.harness.out_dir,
                release_mode=release_mod.CERTIFIED_RELEASE_MODE,
                sandbox_attestation=attestation(agents_config=agents_config),
                agents_config=agents_config,
                expected_campaign_fingerprint="1" * 64,
            )
        self.assertFalse(self.harness.out_dir.exists())

    def test_certified_freeze_binds_attestation_and_difficulty(self) -> None:
        manifest = self.freeze_certified()
        task_id = self.harness.task.task_id
        attestation_path = (
            self.harness.out_dir / release_mod.SANDBOX_ATTESTATION_FILENAME
        )
        difficulty_rel = (
            Path("private") / task_id / release_mod.DIFFICULTY_REPORT_REL
        ).as_posix()
        frozen = att.SandboxAttestation.model_validate_json(
            attestation_path.read_text(encoding="utf-8")
        )

        self.assertEqual(manifest.release_mode, release_mod.CERTIFIED_RELEASE_MODE)
        self.assertEqual(frozen.run_id, manifest.release_id)
        self.assertEqual(
            manifest.sandbox_attestation_digest, frozen.attestation_digest
        )
        self.assertIn(release_mod.SANDBOX_ATTESTATION_FILENAME, manifest.checksums)
        self.assertIn(task_id, manifest.difficulty_measurements)
        self.assertIn(difficulty_rel, manifest.checksums)
        self.assertNotIn(
            difficulty_rel,
            release_mod._private_semantic_checksums(manifest.checksums, (task_id,)),
        )
        verification = release_mod.verify_release(self.harness.out_dir)
        self.assertTrue(verification.ok, verification.failures)

    def test_certified_freeze_requires_the_active_campaign_identity(self) -> None:
        measurement = self.write_empirical_difficulty()
        assert measurement.empirical is not None
        agents_config = sandbox_fixture.agents_config_for_pin(
            sandbox_fixture.GVISOR_PIN
        )
        with self.assertRaisesRegex(ValueError, "active calibration campaign"):
            release_mod.freeze_release(
                self.harness.engine,
                self.harness.selection,
                self.harness.out_dir,
                release_mode=release_mod.CERTIFIED_RELEASE_MODE,
                sandbox_attestation=attestation(agents_config=agents_config),
                agents_config=agents_config,
            )
        with self.assertRaisesRegex(ValueError, "stale for the active campaign"):
            release_mod.freeze_release(
                self.harness.engine,
                self.harness.selection,
                self.harness.out_dir,
                release_mode=release_mod.CERTIFIED_RELEASE_MODE,
                sandbox_attestation=attestation(agents_config=agents_config),
                agents_config=agents_config,
                expected_campaign_fingerprint="2" * 64,
            )
    def test_verify_release_rejects_missing_or_semantically_invalid_attestation(
        self,
    ) -> None:
        self.freeze_certified()
        path = self.harness.out_dir / release_mod.SANDBOX_ATTESTATION_FILENAME
        path.unlink()
        missing = release_mod.verify_release(self.harness.out_dir)
        self.assertFalse(missing.ok)
        self.assertTrue(
            any(
                release_mod.SANDBOX_ATTESTATION_FILENAME in failure
                and "missing" in failure
                for failure in missing.failures
            ),
            missing.failures,
        )

        # Restore a self-sealed record that is byte-different and tier C. The
        # semantic gate must report the tier refusal in addition to checksums.
        original = attestation(run_id="irrelevant")
        tampered = att.seal_sandbox_attestation(
            original.model_copy(
                update={
                    "tier": "C",
                    "run_id": release_mod.ReleaseManifest.model_validate_json(
                        (self.harness.out_dir / "release_manifest.json").read_text(
                            encoding="utf-8"
                        )
                    ).release_id,
                    "attestation_digest": "",
                }
            )
        )
        path.write_text(
            json.dumps(tampered.model_dump(mode="json"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        invalid = release_mod.verify_release(self.harness.out_dir)
        self.assertFalse(invalid.ok)
        self.assertTrue(
            any("attestation_tier_refused" in failure for failure in invalid.failures),
            invalid.failures,
        )

    def test_verify_release_rejects_manifest_attestation_digest_mismatch(
        self,
    ) -> None:
        self.freeze_certified()
        manifest_path = self.harness.out_dir / "release_manifest.json"
        os.chmod(manifest_path, 0o644)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["sandbox_attestation_digest"] = "f" * 64
        manifest_path.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        verification = release_mod.verify_release(self.harness.out_dir)
        self.assertFalse(verification.ok)
        self.assertTrue(
            any(
                "attestation content digest disagrees" in failure
                for failure in verification.failures
            ),
            verification.failures,
        )

    def test_verify_release_rejects_stale_difficulty_content(self) -> None:
        self.freeze_certified()
        task_id = self.harness.task.task_id
        path = (
            self.harness.out_dir
            / "private"
            / task_id
            / release_mod.DIFFICULTY_REPORT_REL
        )
        os.chmod(path, 0o644)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["task_content_hash"] = "f" * 64
        payload["empirical"]["measured_at_content_hash"] = "f" * 64
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        verification = release_mod.verify_release(self.harness.out_dir)
        self.assertFalse(verification.ok)
        self.assertTrue(
            any(
                "difficulty" in failure and "stale" in failure
                for failure in verification.failures
            ),
            verification.failures,
        )

    def test_verify_release_rejects_manifest_difficulty_digest_mismatch(
        self,
    ) -> None:
        self.freeze_certified()
        task_id = self.harness.task.task_id
        manifest_path = self.harness.out_dir / "release_manifest.json"
        os.chmod(manifest_path, 0o644)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["difficulty_measurements"][task_id] = "f" * 64
        manifest_path.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        verification = release_mod.verify_release(self.harness.out_dir)
        self.assertFalse(verification.ok)
        self.assertTrue(
            any(
                "canonical difficulty digest disagrees" in failure
                for failure in verification.failures
            ),
            verification.failures,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

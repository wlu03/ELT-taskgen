"""Fail-closed invariants for promoted runtime difficulty evidence."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.cli import CliUsageError, cmd_runtime_certify_difficulty
from elt_taskgen.corpus.certified_difficulty import (
    CertifiedDifficultyError,
    RuntimeCertifiedDifficulty,
    build_runtime_certified_difficulty,
    certified_difficulty_digest,
    verify_certified_difficulty,
)
from elt_taskgen.corpus.difficulty import structural_difficulty, with_empirical
from elt_taskgen.export import certification as certification_mod
from elt_taskgen.export import release as release_mod
from elt_taskgen.models import (
    EmpiricalDifficulty,
    SolverTierResult,
    TaskVariant,
    VariantCalibration,
    solver_roster_fingerprint,
)


def _report() -> RuntimeCertifiedDifficulty:
    task = demo_fixture.demo_task()
    model_key = "test:solver"
    el_tier = SolverTierResult(
        model_key=model_key, k=2, successes=1, stage1_failures=1
    )
    transform_tier = SolverTierResult(
        model_key=model_key, k=2, successes=1, stage2_failures=1
    )
    empirical = EmpiricalDifficulty(
        solver_config="test-roster",
        n_attempts=4,
        success_rate=0.5,
        stage1_failure_rate=0.5,
        stage2_failure_rate=0.5,
        variants={
            TaskVariant.EXTRACT_LOAD: VariantCalibration(
                variant=TaskVariant.EXTRACT_LOAD, tiers=(el_tier,)
            ),
            TaskVariant.TRANSFORM: VariantCalibration(
                variant=TaskVariant.TRANSFORM, tiers=(transform_tier,)
            ),
        },
        roster_fingerprint=solver_roster_fingerprint((model_key,)),
        campaign_fingerprint="1" * 64,
        measured_at_content_hash=task.content_hash(),
    )
    difficulty = with_empirical(structural_difficulty(task), empirical)
    report = RuntimeCertifiedDifficulty(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        release_id="release-test",
        semantic_release_id="semantic-test",
        runtime_bundle_id="runtime-test",
        certification_id="certification-test",
        destination="snowflake",
        difficulty=difficulty,
        empirical_band="medium",
        variant_pass_rates={
            TaskVariant.EXTRACT_LOAD.value: {model_key: 0.5},
            TaskVariant.TRANSFORM.value: {model_key: 0.5},
        },
        empirical_roster_fingerprint=empirical.roster_fingerprint,
        empirical_campaign_fingerprint=empirical.campaign_fingerprint,
        runtime_attestation_digest="0" * 64,
        runtime_lifecycle_digest="2" * 64,
    )
    return report.model_copy(
        update={"evidence_digest": certified_difficulty_digest(report)}
    )


def _reseal(
    report: RuntimeCertifiedDifficulty, **updates
) -> RuntimeCertifiedDifficulty:
    changed = report.model_copy(update={**updates, "evidence_digest": ""})
    return changed.model_copy(
        update={"evidence_digest": certified_difficulty_digest(changed)}
    )


class CertifiedDifficultyInvariantTests(unittest.TestCase):
    def test_valid_derived_summary_verifies(self) -> None:
        report = _report()
        self.assertEqual(verify_certified_difficulty(report), report)

    def test_current_report_binds_verified_lifecycle_digest(self) -> None:
        report = _report()
        self.assertEqual(report.schema_version, "1.2")
        self.assertEqual(report.runtime_lifecycle_digest, "2" * 64)
        changed = report.model_copy(
            update={"runtime_lifecycle_digest": "3" * 64, "evidence_digest": ""}
        )
        self.assertNotEqual(
            certified_difficulty_digest(changed), report.evidence_digest
        )

    def test_current_report_without_lifecycle_digest_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "lifecycle digest"):
            verify_certified_difficulty(
                _reseal(_report(), runtime_lifecycle_digest="")
            )

    def test_legacy_report_without_lifecycle_digest_remains_diagnosable(self) -> None:
        payload = _report().model_dump(mode="json")
        payload["schema_version"] = "1.1"
        payload.pop("runtime_lifecycle_digest")
        payload["evidence_digest"] = ""
        legacy = RuntimeCertifiedDifficulty.model_validate(payload)
        sealed = legacy.model_copy(
            update={"evidence_digest": certified_difficulty_digest(legacy)}
        )
        with self.assertRaisesRegex(
            CertifiedDifficultyError, "legacy.*cleanup-lifecycle"
        ):
            verify_certified_difficulty(sealed)

    def test_builder_copies_digest_from_verified_lifecycle_receipt(self) -> None:
        source = _report()
        manifest = SimpleNamespace(
            tasks={source.task_id: source.task_content_hash},
            release_mode=release_mod.CERTIFIED_RELEASE_MODE,
            sandbox_attestation_digest="4" * 64,
            difficulty_measurements={},
            certification_ids={source.task_id: source.certification_id},
            release_id=source.release_id,
            runtime_bundle_ids={source.task_id: source.runtime_bundle_id},
            semantic_release_id=source.semantic_release_id,
            destinations={source.task_id: source.destination},
        )
        status = certification_mod.CertificationStatus(
            certification_id=source.certification_id,
            state=certification_mod.CertificationState.CERTIFIED,
            populations=("primary",),
        )
        attestation = SimpleNamespace(
            task_id=source.task_id,
            release_id=source.release_id,
            runtime_bundle_id=source.runtime_bundle_id,
            populations=("primary",),
            attestation_digest=source.runtime_attestation_digest,
        )
        lifecycle_digest = "7" * 64
        lifecycle = SimpleNamespace(
            certification_attestation_digest=source.runtime_attestation_digest,
            lifecycle_digest=lifecycle_digest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            release_dir = Path(tmp) / "release"
            release_dir.mkdir()
            (release_dir / "release_manifest.json").write_text(
                "{}", encoding="utf-8"
            )
            with (
                mock.patch.object(
                    release_mod,
                    "verify_release",
                    return_value=SimpleNamespace(ok=True, failures=()),
                ),
                mock.patch.object(
                    release_mod.ReleaseManifest,
                    "model_validate_json",
                    return_value=manifest,
                ),
                mock.patch(
                    "elt_taskgen.corpus.certified_difficulty._load_release_difficulty",
                    return_value=source.difficulty,
                ),
                mock.patch.object(
                    certification_mod,
                    "certification_status",
                    return_value=status,
                ),
                mock.patch.object(
                    certification_mod,
                    "verify_attestation",
                    return_value=attestation,
                ),
                mock.patch(
                    "elt_taskgen.runtime.certification_lifecycle."
                    "verify_completed_lifecycle",
                    return_value=lifecycle,
                ),
            ):
                built = build_runtime_certified_difficulty(
                    release_dir=release_dir,
                    certification_store=Path(tmp) / "certifications",
                    task_id=source.task_id,
                )

        self.assertEqual(built.runtime_lifecycle_digest, lifecycle_digest)
        self.assertEqual(verify_certified_difficulty(built), built)

    def test_resealed_contradictory_derived_fields_are_refused(self) -> None:
        report = _report()
        contradictions = {
            "variant_pass_rates": {
                "extract_load": {"fabricated": 0.99},
                "transform": {"fabricated": 0.99},
            },
            "empirical_band": "easy",
            "empirical_roster_fingerprint": "fabricated",
        }
        for field, value in contradictions.items():
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(CertifiedDifficultyError, field),
            ):
                verify_certified_difficulty(_reseal(report, **{field: value}))

    def test_resealed_contradictory_empirical_aggregates_are_refused(self) -> None:
        report = _report()
        assert report.difficulty.empirical is not None
        contradictions = {
            "n_attempts": 99,
            "success_rate": 0.25,
            "stage1_failure_rate": 0.25,
            "stage2_failure_rate": 0.25,
        }
        for field, value in contradictions.items():
            changed_empirical = report.difficulty.empirical.model_copy(
                update={field: value}
            )
            changed_difficulty = report.difficulty.model_copy(
                update={"empirical": changed_empirical}
            )
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(CertifiedDifficultyError, field),
            ):
                verify_certified_difficulty(
                    _reseal(report, difficulty=changed_difficulty)
                )


class CertifiedDifficultyOutputTests(unittest.TestCase):
    def test_output_creation_is_exclusive_even_if_exists_check_lies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "certified.json"
            out.write_text("first writer\n", encoding="utf-8")
            args = argparse.Namespace(
                release=Path(tmp) / "release",
                certification_store=Path(tmp) / "certifications",
                task_id=_report().task_id,
                workspace=None,
                out=out,
            )
            with (
                mock.patch(
                    "elt_taskgen.corpus.certified_difficulty."
                    "build_runtime_certified_difficulty",
                    return_value=_report(),
                ),
                # The old exists()+write_text sequence overwrote under this
                # interleaving; exclusive create still asks the filesystem.
                mock.patch.object(Path, "exists", return_value=False),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(CliUsageError, "already exists"),
            ):
                cmd_runtime_certify_difficulty(args)
            self.assertEqual(out.read_text(encoding="utf-8"), "first writer\n")

    def test_interrupted_output_is_not_published_and_retry_succeeds(self) -> None:
        from elt_taskgen.runtime import certification_lifecycle as lifecycle

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "certified.json"
            args = argparse.Namespace(
                release=Path(tmp) / "release",
                certification_store=Path(tmp) / "certifications",
                task_id=_report().task_id,
                workspace=None,
                out=out,
            )
            with (
                mock.patch(
                    "elt_taskgen.corpus.certified_difficulty."
                    "build_runtime_certified_difficulty",
                    return_value=_report(),
                ),
                mock.patch.object(
                    lifecycle.os, "write", side_effect=OSError("interrupted write")
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(CliUsageError, "could not create"),
            ):
                cmd_runtime_certify_difficulty(args)

            self.assertFalse(out.exists())
            self.assertEqual(list(out.parent.glob(f".{out.name}.stage-*")), [])

            with (
                mock.patch(
                    "elt_taskgen.corpus.certified_difficulty."
                    "build_runtime_certified_difficulty",
                    return_value=_report(),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_runtime_certify_difficulty(args), 0)

            self.assertEqual(
                json.loads(out.read_text(encoding="utf-8")),
                _report().model_dump(mode="json"),
            )
            self.assertEqual(out.stat().st_mode & 0o222, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

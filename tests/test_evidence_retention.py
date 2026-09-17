from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from elt_taskgen.review import evidence_retention as retention
from elt_taskgen.review import providers


class EvidencePermissionTest(unittest.TestCase):
    def test_new_transcript_parents_and_file_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            store = providers.TranscriptStore(workspace / "transcripts")
            key = "a" * 64
            path = store.record("critic", key, {"prompt_sha256": key})

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE((workspace / "transcripts").stat().st_mode), 0o700
            )
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_migration_is_idempotent_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "transcripts"
            nested = root / "role"
            nested.mkdir(parents=True)
            record = nested / "entry.json"
            record.write_text("{}", encoding="utf-8")
            root.chmod(0o755)
            nested.chmod(0o755)
            record.chmod(0o644)

            first = retention.harden_permissions(root)
            second = retention.harden_permissions(root)
            self.assertEqual(first.changed, 3)
            self.assertEqual(second.changed, 0)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)

            outside = Path(tmp) / "outside"
            outside.write_text("private", encoding="utf-8")
            os.symlink(outside, nested / "escape")
            with self.assertRaises(retention.EvidenceMaintenanceError):
                retention.harden_permissions(root)

    def test_provider_refuses_symlinked_evidence_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            outside = Path(tmp) / "outside"
            workspace.mkdir()
            outside.mkdir()
            (outside / "critic").mkdir()
            os.symlink(outside, workspace / "transcripts")
            store = providers.TranscriptStore(workspace / "transcripts")
            key = "b" * 64
            with self.assertRaisesRegex(RuntimeError, "not a real directory"):
                store.record("critic", key, {"prompt_sha256": key})
            self.assertEqual(list((outside / "critic").iterdir()), [])

    def test_provider_raw_output_is_private_and_refuses_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            raw = workspace / "tool_raw" / ("c" * 64) / "0.bin"
            payload = b"synthetic raw evidence"
            digest = hashlib.sha256(payload).hexdigest()
            providers._publish_raw_output(raw, payload, digest)
            self.assertEqual(stat.S_IMODE(raw.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(raw.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(raw.parent.parent.stat().st_mode), 0o700)

            outside = Path(tmp) / "outside.bin"
            outside.write_bytes(payload)
            planted = raw.parent / "1.bin"
            os.symlink(outside, planted)
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                providers._publish_raw_output(planted, payload, digest)
            self.assertEqual(outside.read_bytes(), payload)

    def test_transcript_lookup_refuses_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "transcripts"
            role = root / "critic"
            role.mkdir(parents=True)
            key = "d" * 64
            outside = Path(tmp) / "outside.json"
            outside.write_text(json.dumps({"prompt_sha256": key}), encoding="utf-8")
            os.symlink(outside, role / f"{key}.json")
            store = providers.TranscriptStore(root)
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                store.lookup("critic", key)

    def test_transcript_lookup_refuses_symlinked_role_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "transcripts"
            outside = Path(tmp) / "outside-role"
            root.mkdir()
            outside.mkdir()
            key = "e" * 64
            (outside / f"{key}.json").write_text(
                json.dumps({"prompt_sha256": key}), encoding="utf-8"
            )
            os.symlink(outside, root / "critic")
            store = providers.TranscriptStore(root)
            with self.assertRaisesRegex(RuntimeError, "not a real directory"):
                store.lookup("critic", key)

    def test_dangling_root_symlink_and_special_file_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            dangling = workspace / "missing"
            os.symlink(dangling, workspace / "transcripts")
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "must not be a symlink"
            ):
                retention.harden_permissions(workspace / "transcripts")

        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tool_raw"
            root.mkdir()
            os.mkfifo(root / "stream")
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "not a regular file"
            ):
                retention.retention_candidates(Path(tmp))

    def test_workspace_hardening_includes_durable_state_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for name in retention.PERMISSION_STORES:
                root = workspace / name
                root.mkdir()
                (root / "record.json").write_text("{}", encoding="utf-8")
                root.chmod(0o755)
                (root / "record.json").chmod(0o644)

            reports = retention.harden_workspace(workspace)
            self.assertEqual(
                [report.root.name for report in reports],
                list(retention.PERMISSION_STORES),
            )
            self.assertTrue(all(report.changed == 2 for report in reports))
            for name in retention.PERMISSION_STORES:
                self.assertEqual(
                    stat.S_IMODE((workspace / name).stat().st_mode), 0o700
                )
                self.assertEqual(
                    stat.S_IMODE((workspace / name / "record.json").stat().st_mode),
                    0o600,
                )


class EvidenceRetentionTest(unittest.TestCase):
    OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
    NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @staticmethod
    def _old_store(workspace: Path, kind: str = "transcripts") -> Path:
        root = workspace / kind / "role"
        root.mkdir(parents=True)
        record = root / "entry.json"
        record.write_text("old evidence", encoding="utf-8")
        old = EvidenceRetentionTest.OLD.timestamp()
        os.utime(record, (old, old))
        return workspace / kind

    def test_whole_store_policy_uses_newest_file_and_stable_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            transcripts = workspace / "transcripts" / "role"
            raw = workspace / "tool_raw" / "digest"
            transcripts.mkdir(parents=True)
            raw.mkdir(parents=True)
            transcript = transcripts / "entry.json"
            raw_file = raw / "0.bin"
            transcript.write_text("old transcript", encoding="utf-8")
            raw_file.write_bytes(b"old raw")
            old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
            os.utime(transcript, (old, old))
            os.utime(raw_file, (old, old))

            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            first = retention.retention_candidates(workspace, now=now)
            second = retention.retention_candidates(workspace, now=now)
            self.assertEqual(first, second)
            self.assertEqual([item.kind for item in first], ["transcripts", "tool_raw"])
            self.assertTrue(all(len(item.tree_sha256) == 64 for item in first))

            recent = now.timestamp()
            os.utime(transcript, (recent, recent))
            remaining = retention.retention_candidates(workspace, now=now)
            self.assertEqual([item.kind for item in remaining], ["tool_raw"])

    def test_naive_clock_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                retention.retention_candidates(
                    Path(tmp), now=datetime(2026, 1, 1)
                )

    def test_plan_digest_is_bound_to_the_workspace_and_exact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left"
            right = Path(tmp) / "right"
            left.mkdir()
            right.mkdir()
            self._old_store(left)
            self._old_store(right)
            left_candidate = retention.retention_candidates(left, now=self.NOW)[0]
            right_candidate = retention.retention_candidates(right, now=self.NOW)[0]
            self.assertEqual(left_candidate.tree_sha256, right_candidate.tree_sha256)
            self.assertNotEqual(
                left_candidate.plan_sha256, right_candidate.plan_sha256
            )

            record = left / "transcripts" / "role" / "other.json"
            record.write_text("also old", encoding="utf-8")
            old = self.OLD.timestamp()
            os.utime(record, (old, old))
            changed = retention.retention_candidates(left, now=self.NOW)[0]
            self.assertNotEqual(left_candidate.plan_sha256, changed.plan_sha256)

    def test_apply_purges_one_whole_store_and_finishes_private_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            receipt = workspace / "state" / "retention" / "receipt.json"

            result = retention.apply_retention(
                workspace,
                kind="transcripts",
                plan_sha256=candidate.plan_sha256,
                receipt=receipt,
                now=self.NOW,
            )

            self.assertEqual(result.state, "complete")
            self.assertFalse(root.exists())
            self.assertTrue(receipt.is_file())
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "complete")
            self.assertTrue(payload["purged"])
            self.assertEqual(payload["plan_sha256"], candidate.plan_sha256)
            self.assertEqual(payload["candidate"]["tree_sha256"], candidate.tree_sha256)
            self.assertNotIn("old evidence", receipt.read_text(encoding="utf-8"))

    def test_apply_refuses_stale_digest_and_recent_store_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            extra = root / "role" / "extra.json"
            extra.write_text("changed", encoding="utf-8")
            old = self.OLD.timestamp()
            os.utime(extra, (old, old))
            receipt = workspace / "receipt.json"
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "plan changed"
            ):
                retention.apply_retention(
                    workspace,
                    kind="transcripts",
                    plan_sha256=candidate.plan_sha256,
                    receipt=receipt,
                    now=self.NOW,
                )
            self.assertTrue(root.is_dir())
            self.assertFalse(receipt.exists())

            fresh = self.NOW.timestamp()
            os.utime(extra, (fresh, fresh))
            current = retention.retention_candidates(workspace, now=self.NOW)
            self.assertEqual(current, ())
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "retention window"
            ):
                retention.apply_retention(
                    workspace,
                    kind="transcripts",
                    plan_sha256="0" * 64,
                    receipt=receipt,
                    now=self.NOW,
                )
            self.assertTrue(root.is_dir())
            self.assertFalse(receipt.exists())

    def test_apply_requires_the_exact_lowercase_plan_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "lowercase hex"
            ):
                retention.apply_retention(
                    workspace,
                    kind="transcripts",
                    plan_sha256=candidate.plan_sha256.upper(),
                    receipt=workspace / "receipt.json",
                    now=self.NOW,
                )
            self.assertTrue(root.is_dir())

    def test_active_admission_blocks_transcript_plan_and_apply_with_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            marker = workspace / retention.ADMISSION_RECORD_RELATIVE
            marker.parent.mkdir()
            marker.write_text("synthetic admission", encoding="utf-8")

            plan = retention.retention_plan(workspace, now=self.NOW)
            self.assertEqual(plan["candidates"], [])
            self.assertEqual(len(plan["refusals"]), 1)
            self.assertEqual(
                plan["refusals"][0]["code"], "admission_record_present"
            )
            self.assertEqual(
                plan["refusals"][0]["candidate"]["plan_sha256"],
                candidate.plan_sha256,
            )

            receipt = workspace / "retention-refusal.json"
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "council admission"
            ):
                retention.apply_retention(
                    workspace,
                    kind="transcripts",
                    plan_sha256=candidate.plan_sha256,
                    receipt=receipt,
                    now=self.NOW,
                )
            self.assertTrue(root.is_dir())
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "refused")
            self.assertEqual(payload["failure"], "admission_record_present")
            self.assertFalse(payload["purged"])
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_raw_store_waits_for_digest_linked_transcript_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            self._old_store(workspace, "transcripts")
            raw_root = self._old_store(workspace, "tool_raw")
            candidates = {
                candidate.kind: candidate
                for candidate in retention.retention_candidates(
                    workspace, now=self.NOW
                )
            }
            plan = retention.retention_plan(workspace, now=self.NOW)
            self.assertEqual(
                [candidate["kind"] for candidate in plan["candidates"]],
                ["transcripts"],
            )
            self.assertEqual(
                [(item["kind"], item["code"]) for item in plan["refusals"]],
                [("tool_raw", "transcript_store_present")],
            )

            refusal_receipt = workspace / "raw-refused.json"
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "validator outputs"
            ):
                retention.apply_retention(
                    workspace,
                    kind="tool_raw",
                    plan_sha256=candidates["tool_raw"].plan_sha256,
                    receipt=refusal_receipt,
                    now=self.NOW,
                )
            self.assertTrue(raw_root.is_dir())
            refused = json.loads(refusal_receipt.read_text(encoding="utf-8"))
            self.assertEqual(refused["state"], "refused")
            self.assertEqual(refused["failure"], "transcript_store_present")

            retention.apply_retention(
                workspace,
                kind="transcripts",
                plan_sha256=candidates["transcripts"].plan_sha256,
                receipt=workspace / "transcripts-purged.json",
                now=self.NOW,
            )
            raw_plan = retention.retention_plan(workspace, now=self.NOW)
            self.assertEqual(
                [candidate["kind"] for candidate in raw_plan["candidates"]],
                ["tool_raw"],
            )
            retention.apply_retention(
                workspace,
                kind="tool_raw",
                plan_sha256=raw_plan["candidates"][0]["plan_sha256"],
                receipt=workspace / "raw-purged.json",
                now=self.NOW,
            )
            self.assertFalse(raw_root.exists())

    def test_receipt_is_durable_before_the_quarantine_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            receipt = workspace / "receipt.json"
            observed: list[str] = []

            def refuse_move(_source, _target):
                observed.append(
                    json.loads(receipt.read_text(encoding="utf-8"))["state"]
                )
                raise OSError("synthetic rename failure")

            with mock.patch.object(retention.os, "rename", side_effect=refuse_move):
                with self.assertRaisesRegex(
                    retention.EvidenceMaintenanceError, "nothing was purged"
                ):
                    retention.apply_retention(
                        workspace,
                        kind="transcripts",
                        plan_sha256=candidate.plan_sha256,
                        receipt=receipt,
                        now=self.NOW,
                    )
            self.assertEqual(observed, ["prepared"])
            self.assertTrue(root.is_dir())
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "aborted_before_quarantine")
            self.assertFalse(payload["purged"])

    def test_failed_removal_leaves_named_quarantine_and_incomplete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            receipt = workspace / "receipt.json"

            with mock.patch.object(
                retention.shutil, "rmtree", side_effect=OSError("synthetic failure")
            ):
                with self.assertRaisesRegex(
                    retention.EvidenceMaintenanceError, "quarantined evidence remains"
                ):
                    retention.apply_retention(
                        workspace,
                        kind="transcripts",
                        plan_sha256=candidate.plan_sha256,
                        receipt=receipt,
                        now=self.NOW,
                    )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "quarantined")
            self.assertFalse(payload["purged"])
            self.assertFalse(root.exists())
            self.assertTrue(Path(payload["quarantine"]).is_dir())

    def test_receipt_cannot_be_written_inside_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            candidate = retention.retention_candidates(workspace, now=self.NOW)[0]
            receipt = root / "receipt.json"
            with self.assertRaisesRegex(
                retention.EvidenceMaintenanceError, "must be outside"
            ):
                retention.apply_retention(
                    workspace,
                    kind="transcripts",
                    plan_sha256=candidate.plan_sha256,
                    receipt=receipt,
                    now=self.NOW,
                )
            self.assertTrue(root.is_dir())
            self.assertFalse(receipt.exists())

    def test_cli_defaults_to_read_only_plan_and_requires_apply_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = self._old_store(workspace)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = retention.main(["--workspace", str(workspace)])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["mode"], "read_only_plan")
            self.assertEqual(len(payload["candidates"]), 1)
            self.assertEqual(
                sorted(path.relative_to(root) for path in root.rglob("*")), before
            )

            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = retention.main(
                    ["--workspace", str(workspace), "--apply"]
                )
            self.assertEqual(code, 2)
            self.assertIn("--apply also requires", error.getvalue())
            self.assertTrue(root.is_dir())


if __name__ == "__main__":
    unittest.main()

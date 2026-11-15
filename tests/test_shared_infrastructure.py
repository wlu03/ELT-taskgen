"""Failure, durability, and evidence-identity contracts for shared services."""

import concurrent.futures
from concurrent.futures.process import BrokenProcessPool
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elt_taskgen import file_io, pipeline_readiness
from elt_taskgen.engine import InfrastructureFailure, StageName
from elt_taskgen.pipeline.workers import run_workers


class DurableWriteTests(unittest.TestCase):
    def test_failed_replacement_preserves_previous_record_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_bytes(b"old")
            with patch.object(file_io.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    file_io.atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_private_directory_and_unicode_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "record.json"
            file_io.atomic_write(path, "λ\n", directory_mode=0o700)
            self.assertEqual(path.read_text(), "λ\n")
            if os.name != "nt":
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            file_io.atomic_write(path, b"replacement", directory_mode=0o777)
            self.assertEqual(path.read_bytes(), b"replacement")
            if os.name != "nt":
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


class WorkerExecutorTests(unittest.TestCase):
    payloads = [{"task_id": "first"}, {"task_id": "second"}]

    def test_shared_failure_pauses_sequential_siblings_and_interrupts_attempt(self):
        calls = []
        def crash(payload):
            calls.append(payload["task_id"])
            raise RuntimeError("worker failed")
        with tempfile.TemporaryDirectory() as directory:
            attempts = Path(directory)
            path = attempts / "first" / "attempt.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"state": "RUNNING"}))
            rows = run_workers(self.payloads, worker=crash, max_workers=1,
                               attempts_dir=attempts, echo=lambda _: None)
            self.assertEqual(calls, ["first"])
            self.assertEqual([r["state"] for r in rows], ["infrastructure", "blocked"])
            self.assertEqual(json.loads(path.read_text())["state"], "INTERRUPTED")

    def test_rejections_and_task_local_failures_leave_siblings_runnable(self):
        for outcome in ({"state": "rejected"}, {"state": "infrastructure", "infrastructure_scope": "task"}):
            calls = []
            def worker(payload):
                calls.append(payload["task_id"])
                return {**payload, **outcome}
            rows = run_workers(self.payloads, worker=worker, max_workers=1, echo=lambda _: None)
            self.assertEqual(calls, ["first", "second"])
            self.assertEqual(len(rows), 2)

    def test_parallel_exception_policy_is_explicit(self):
        def crash(payload):
            raise ValueError(payload["task_id"])
        with patch("concurrent.futures.ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor):
            rows = run_workers(self.payloads, worker=crash, max_workers=2, echo=lambda _: None)
            self.assertEqual([r["state"] for r in rows], ["infrastructure"] * 2)
            with self.assertRaises(ValueError):
                run_workers(self.payloads, worker=crash, max_workers=2,
                            failure_policy="raise_parallel", echo=lambda _: None)

    def test_broken_pool_accounts_for_every_candidate_or_raises(self):
        with patch("concurrent.futures.ProcessPoolExecutor", side_effect=BrokenProcessPool("crash")):
            rows = run_workers(self.payloads, worker=lambda p: p, max_workers=2, echo=lambda _: None)
            self.assertEqual([r["task_id"] for r in rows], ["first", "second"])
            self.assertTrue(all(r["state"] == "infrastructure" for r in rows))
            with self.assertRaises(InfrastructureFailure):
                run_workers(self.payloads, worker=lambda p: p, max_workers=2,
                            failure_policy="raise_parallel", echo=lambda _: None)


class ImplementationSealTests(unittest.TestCase):
    def test_moved_implementations_remain_part_of_evidence_identity(self):
        spec = pipeline_readiness.GenerationRunSpec(candidate_count=1, profile="draft")
        modules = {
            StageName.REFERENCE: (
                "elt_taskgen.generation.plan_ops", "elt_taskgen.generation.plan_validation",
                "elt_taskgen.generation.shapes.rollup", "elt_taskgen.generation.recipe_admission",
                "elt_taskgen.pipeline.workers", "elt_taskgen.pipeline.corpus",
                "elt_taskgen.pipeline.migration", "elt_taskgen.file_io",
            ),
            StageName.AUTHOR: (
                "elt_taskgen.review.provider_config", "elt_taskgen.review.provider_protocol",
                "elt_taskgen.review.provider_backends",
            ),
        }
        digest = pipeline_readiness._module_source_sha256
        for stage, names in modules.items():
            original = pipeline_readiness._configured_inputs(spec, stage)
            for name in names:
                with self.subTest(stage=stage, module=name):
                    self.assertIn(name, original["module_source_sha256"])
                    with patch.object(pipeline_readiness, "_module_source_sha256",
                                      side_effect=lambda n: "0" * 64 if n == name else digest(n)):
                        changed = pipeline_readiness._configured_inputs(spec, stage)
                    self.assertNotEqual(original["implementation_sha256"], changed["implementation_sha256"])

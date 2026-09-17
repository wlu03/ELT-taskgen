"""Roadmap Phase 2 item 2.b: the declarative, single-step, Docker-free env.

``DeclarativeEltEnv`` installs, writes ``elt/``, seals and scores through the
PUBLIC ``score_workspace`` (the orchestration doubles of
``tests/test_training_scorer.py`` are wired through the same entry point, so
no dbt runtime is needed here) and projects with ``training_signal``. The
outer supervisor deadline around the grader lives in the environment: a
deadline is label ``None``, never ``0.0`` (Output 8 §8.5; OQ-29).
"""

from __future__ import annotations

import errno
import functools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen.review import session as S
from elt_taskgen.training.contract import WorkspaceErrorCode
from elt_taskgen.training.dbt_runner import DbtRuntimeConfig
from elt_taskgen.training.env import (
    DeclarativeEltEnv,
    Observation,
    StepResult,
    drop_group_if_unlabelled,
    supervised_score,
)
from elt_taskgen.training.models import (
    WorkspaceArtifactFile,
    workspace_artifact_digest,
)
from elt_taskgen.training.scorer import _ScorerDependencies, score_workspace
from elt_taskgen.training.terraform_intent import TerraformIntentEvaluation
from elt_taskgen.training.workspace import load_sealed_workspace, replay_workspace
from elt_taskgen.review.tools.registry import ToolRegistry


FIXTURE_RELEASE = (
    Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"
)
TASK_ID = "gate__five_backend_probe"

ARTIFACT = {
    "elt/main.tf": b'terraform {\n  required_providers {}\n}\n',
    "elt/dbt_project.yml": (
        b"name: gate_task\nversion: '1.0'\nprofile: elt_taskgen\n"
        b"model-paths: ['models']\n"
    ),
    "elt/models/sources.yml": (
        b"version: 2\nsources:\n  - name: raw\n    tables:\n"
        b"      - name: customers\n"
    ),
    # A key relative to elt/ is accepted too.
    "models/customer_rollup.sql": (
        b"{{ config(materialized='table') }}\n"
        b"select * from {{ source('raw', 'customers') }}\n"
    ),
}


def _runtime_config(root: Path) -> DbtRuntimeConfig:
    return DbtRuntimeConfig(python=root / "unused-python", manifest=root / "unused-runtime.json")


def _dbt_result(*, mart_reward: float = 1.0, raw_immutable: bool = True, errors=()):
    return SimpleNamespace(
        dbt_project=1.0,
        mart_reward=mart_reward,
        raw_immutable=raw_immutable,
        error_codes=tuple(errors),
    )


def _doubles(events: list[tuple[str, str]], *, mart_reward: float = 1.0) -> _ScorerDependencies:
    def replay(package, sealed, path, **kwargs):
        events.append(("replay", path.name))
        return replay_workspace(package, sealed, path, **kwargs)

    def terraform(package, path):
        events.append(("terraform", path.parent.name))
        return TerraformIntentEvaluation(reward=1.0, graph=SimpleNamespace())

    def sync(package, population, intent, database_path):
        events.append(("sync", population.value))
        database_path.parent.mkdir(parents=True)
        database_path.write_bytes(population.value.encode("ascii"))
        return SimpleNamespace(
            sync_lifecycle=True,
            upstream_stage1=True,
            strict_raw_tables=1.0,
            error_codes=(),
            database_path=database_path,
        )

    def dbt(package, population, *, attempt_dir, sync_execution, **kwargs):
        events.append(("dbt", population.value))
        return _dbt_result(mart_reward=mart_reward)

    return _ScorerDependencies(replay=replay, terraform=terraform, sync=sync, dbt=dbt)


# Grader-child entry doubles, loaded by `module:function` instead of the public
# scorer. The parent passes this module's importable name to the child.
_ENTRY_MODULE = __name__ if __name__ != "__main__" else "tests.test_training_env"


def _stall_with_detached_grandchild(package, sealed, *, attempts_root, **kwargs):
    """A grader that spawns one long-lived grandchild in the child's own
    process group and one DETACHED into a new session (the way `dbt_runner`
    spawns dbt), records every pid, then stalls past the deadline."""
    root = Path(attempts_root)
    root.mkdir(parents=True, exist_ok=True)
    same_group = subprocess.Popen(["sleep", "300"])
    detached = subprocess.Popen(["sleep", "300"], start_new_session=True)
    (root / "pids.json").write_text(
        json.dumps({"child": os.getpid(), "same_group": same_group.pid, "detached": detached.pid})
    )
    same_group.wait()
    detached.wait()
    raise AssertionError("a late answer is never rewritten into a label")


def _raising_grader(package, sealed, **kwargs):
    raise RuntimeError("/private/answer_key/gold.csv expected 7 rows")


def _non_result_grader(package, sealed, **kwargs):
    return {"reward": 0.0}


def _pid_reporting_grader(package, sealed, *, attempts_root, **kwargs):
    """The REAL public grader, after recording the pid it runs in."""
    root = Path(attempts_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pid.txt").write_text(str(os.getpid()))
    return score_workspace(package, sealed, attempts_root=attempts_root, **kwargs)


def _entry(function) -> str:
    return f"{_ENTRY_MODULE}:{function.__name__}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _grader_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.endswith("-grader")]


class DeclarativeEnvTests(unittest.TestCase):
    def _env(self, root: Path, **kwargs) -> DeclarativeEltEnv:
        return DeclarativeEltEnv(
            FIXTURE_RELEASE,
            attempts_root=root / "episodes",
            runtime_config=_runtime_config(root),
            **kwargs,
        )

    def test_declarative_env_step_seals_and_scores_without_tools(self):
        events: list[tuple[str, str]] = []
        scorer = functools.partial(score_workspace, _dependencies=_doubles(events))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, scorer=scorer)
            observation = env.reset(TASK_ID)
            self.assertIsInstance(observation, Observation)
            self.assertEqual(observation.task_id, TASK_ID)
            self.assertTrue(observation.elt_dir.is_dir())
            self.assertEqual(observation.elt_dir, observation.task_dir / "elt")
            # The policy sees the installed PUBLIC task only: no release root,
            # no private package, no hidden population, no gold.
            self.assertFalse(observation.attempt_root.is_relative_to(FIXTURE_RELEASE))
            for rel in observation.public_files:
                for forbidden in ("private", "answer_key", "populations", "sources", ".duckdb"):
                    self.assertNotIn(forbidden, rel)
            self.assertIn("documentation/README.md", observation.public_files)
            self.assertTrue(observation.documentation)
            for forbidden in ("answer_key", "private/", "reference.sql"):
                self.assertNotIn(forbidden, observation.documentation)
            # No tool surface is injected into the declarative env itself.
            # The independent implementer is agentic by default in the task
            # generation pipeline, but that separate registry is not exposed
            # through this policy environment.
            self.assertFalse(hasattr(env, "tools"))
            self.assertEqual(
                ToolRegistry.for_role("independent_implementer").names,
                (
                    "abort", "check_submission", "dev_query", "dry_run_sql",
                    "list_schemas", "run_mart_sql_dev", "submit_sql_by_mart",
                ),
            )

            step = env.step(ARTIFACT)
            self.assertIsInstance(step, StepResult)
            self.assertTrue(step.done)
            self.assertEqual(step.harness_fault, "")
            self.assertIsNotNone(step.result)
            self.assertTrue(step.result.valid_submission)
            self.assertEqual(step.result.reward, 1.0)
            self.assertTrue(step.signal.label_valid)
            self.assertTrue(step.signal.el_pass)
            self.assertEqual(step.signal.reward, 1.0)
            self.assertEqual(step.signal.first_failed_phase, "none")
            self.assertEqual(sorted(step.signal.populations), sorted(step.result.graded_populations))
            # Scoring went through the public scorer on the complete graded
            # set: replay -> terraform -> sync -> dbt, four times.
            self.assertEqual([event[0] for event in events], ["replay", "terraform", "sync", "dbt"] * 4)
            # What was sealed is exactly what the policy emitted, keyed by the
            # public manifest rule (a bare `models/...` key lands under elt/).
            sealed = load_sealed_workspace(step.sealed_dir, expected_seal_sha256=step.seal_sha256)
            expected = tuple(
                WorkspaceArtifactFile(
                    path=path if path.startswith("elt/") else f"elt/{path}",
                    size_bytes=len(data),
                    sha256=__import__("hashlib").sha256(data).hexdigest(),
                )
                for path, data in sorted(
                    ((k if k.startswith("elt/") else f"elt/{k}", v) for k, v in ARTIFACT.items())
                )
            )
            self.assertEqual(sealed.submission.files, expected)
            self.assertEqual(step.result.artifact_sha256, workspace_artifact_digest(expected))
            self.assertEqual(step.signal.artifact_sha256, step.result.artifact_sha256)
            # Single-step: a second step is refused until the next reset.
            with self.assertRaises(RuntimeError):
                env.step(ARTIFACT)
            # A fresh reset installs a fresh attempt; the first episode's seal
            # is retained (sealed artifacts are evidence).
            again = env.reset(TASK_ID)
            self.assertNotEqual(again.attempt_root, observation.attempt_root)
            self.assertTrue(step.sealed_dir.is_dir())
            env.close()
            self.assertFalse(again.attempt_root.exists())
            self.assertTrue(step.sealed_dir.is_dir())

    def test_refused_artifact_is_a_measured_zero_before_any_grader_runs(self):
        calls = 0

        def never(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("the grader must not run")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, scorer=never)
            env.reset(TASK_ID)
            escaped = dict(ARTIFACT)
            escaped["../task_ir.json"] = b"{}"
            step = env.step(escaped)
            self.assertEqual(calls, 0)
            self.assertTrue(step.done)
            self.assertFalse(step.result.valid_submission)
            self.assertEqual(step.result.failure.error_code, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE)
            self.assertTrue(step.signal.label_valid)
            self.assertTrue(step.signal.policy_violation)
            self.assertEqual(step.signal.reward, 0.0)
            self.assertEqual(step.signal.first_failed_phase, "workspace")
            # An empty artifact fails the seal as a policy FAILURE (0.0).
            env.reset(TASK_ID)
            empty = env.step({})
            self.assertEqual(calls, 0)
            self.assertEqual(empty.signal.reward, 0.0)
            self.assertFalse(empty.signal.policy_violation)
            self.assertEqual(empty.result.failure.error_code, WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT)

    def test_grader_deadline_yields_none_never_zero(self):
        def stalling(*args, **kwargs):
            time.sleep(0.6)
            raise AssertionError("a late answer is never rewritten into a label")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, scorer=stalling, grader_deadline_s=0.05)
            env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assertTrue(step.done)
            self.assertIsNone(step.result)
            self.assertEqual(step.harness_fault, "ToolDeadlineExceeded")
            self.assertEqual(step.harness_fault_code, "deadline_exceeded")
            self.assertIn(step.harness_fault, S.INFRASTRUCTURE_FAULT_NAMES)
            self.assertFalse(step.signal.label_valid)
            self.assertIsNone(step.signal.reward)
            self.assertNotEqual(step.signal.reward, 0.0)
            self.assertEqual(step.signal.first_failed_phase, "none")
            # The seal was taken before the grader ran, so the trajectory is
            # still identified, and the adapter drops its whole group.
            self.assertEqual(step.seal_sha256, load_sealed_workspace(step.sealed_dir).seal_sha256)
            self.assertTrue(drop_group_if_unlabelled([step.signal]))
        # The supervisor itself: a deadline and a raising grader are typed
        # faults (class name only), a non-result is a harness fault too.
        result, fault = supervised_score(lambda: time.sleep(0.5), deadline_s=0.05)
        self.assertIsNone(result)
        self.assertIsInstance(fault, S.ToolDeadlineExceeded)

        def raising():
            raise RuntimeError("/private/answer_key/gold.csv expected 7 rows")

        result, fault = supervised_score(raising, deadline_s=5.0)
        self.assertIsNone(result)
        self.assertIsInstance(fault, S.ToolHarnessFault)
        self.assertEqual(fault.cause_type, "RuntimeError")
        self.assertNotIn("answer_key", str(fault))
        self.assertNotIn("7 rows", str(fault))
        result, fault = supervised_score(lambda: {"reward": 0.0}, deadline_s=5.0)
        self.assertIsNone(result)
        self.assertEqual(fault.code, "non_result")
        with self.assertRaises(ValueError):
            supervised_score(lambda: None, deadline_s=0.0)

    def test_policy_shaped_artifacts_are_classified_never_crash_the_env(self):
        """Finding 2-2: an artifact whose shape used to crash the writer
        (`IsADirectoryError`, `FileExistsError`, `TypeError`) is a classified
        `StepResult`, and the grader never runs."""
        calls = 0

        def never(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("the grader must not run for a refused artifact")

        cases = [
            ({"elt": b"x"}, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE),
            ({"elt/models": b"x", "elt/models/a.sql": b"y"}, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE),
            ({"elt/models/a.sql": b"y", "elt/models": b"x"}, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE),
            ({"elt/main.tf": 5}, WorkspaceErrorCode.SUBMISSION_INVALID),
            ({"elt/Main.tf": b"1", "elt/main.tf": b"2"}, WorkspaceErrorCode.WORKSPACE_CASE_COLLISION),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, scorer=never)
            for artifact, code in cases:
                env.reset(TASK_ID)
                step = env.step(artifact)
                self.assertTrue(step.done, msg=str(artifact))
                self.assertIsNotNone(step.result, msg=str(artifact))
                self.assertFalse(step.result.valid_submission, msg=str(artifact))
                self.assertEqual(step.result.failure.error_code, code, msg=str(artifact))
                # Each of these is a measured label (policy failure/violation),
                # never None, and the grader never ran.
                self.assertTrue(step.signal.label_valid, msg=str(artifact))
                self.assertEqual(step.signal.reward, 0.0, msg=str(artifact))
            self.assertEqual(calls, 0)

    def test_transient_io_fault_at_seal_is_a_harness_fault_not_a_zero(self):
        """Finding 2-0: an EMFILE/EIO hiccup re-hashing the PUBLIC task tree at
        seal time is the harness's fault (the policy never touches it), so the
        label is None, never a 0.0 policy violation."""
        import errno
        import os as _os

        real_open = _os.open

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root)  # the real score_workspace is never reached
            env.reset(TASK_ID)

            def emfile_once(path, *a, **k):
                emfile_once.calls += 1
                if emfile_once.calls == 1:
                    raise OSError(errno.EMFILE, "too many open files")
                return real_open(path, *a, **k)

            emfile_once.calls = 0
            with mock.patch.object(_os, "open", emfile_once):
                step = env.step(ARTIFACT)
            self.assertTrue(step.done)
            self.assertIsNone(step.result)
            self.assertIn(step.harness_fault, S.INFRASTRUCTURE_FAULT_NAMES)
            self.assertFalse(step.signal.label_valid)
            self.assertIsNone(step.signal.reward)
            self.assertNotEqual(step.signal.reward, 0.0)

    def test_grader_deadline_is_sized_above_worst_case_candidate_caps(self):
        """Finding 2-3: the default outer deadline is sized so a candidate that
        hits every one of its own pre-declared dbt caps still lands a measured
        `dbt_timeout` 0.0 INSIDE the window — a trip means the trusted phases
        overran, never that a candidate pushed a certain zero into a dropped
        group ('None farming')."""
        from elt_taskgen.training.dbt_runner import DbtRunnerLimits
        from elt_taskgen.training.env import (
            DEFAULT_GRADER_DEADLINE_S,
            default_grader_deadline_s,
        )
        from elt_taskgen.verification.gates import GRADED_POPULATIONS

        limits = DbtRunnerLimits(command_timeout_seconds=90.0)
        deadline = default_grader_deadline_s(limits)
        worst_case_candidate = len(GRADED_POPULATIONS) * 3 * 90.0
        # The deadline strictly exceeds the worst-case candidate dbt time, and
        # is never below the historical floor.
        self.assertGreater(deadline, worst_case_candidate)
        self.assertGreaterEqual(deadline, DEFAULT_GRADER_DEADLINE_S)
        # A tighter per-command cap yields a smaller (but still floored) budget.
        self.assertGreaterEqual(
            default_grader_deadline_s(DbtRunnerLimits(command_timeout_seconds=10.0)),
            DEFAULT_GRADER_DEADLINE_S,
        )
        # The env installs the sized default when the caller pins nothing.
        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory), dbt_limits=limits)
            self.assertEqual(env.grader_deadline_s, deadline)

    def test_grader_returned_public_mutation_is_a_harness_fault_not_a_zero(self):
        """Finding 2-0 (replay path): a structural workspace failure the grader
        returns (a public-tree mutation re-detected at replay) is impossible
        for the policy in the declarative env, so it is unlabelled, never 0.0."""
        from elt_taskgen.training.models import WorkspaceFailure, WorkspaceScoreResult

        def mutated(package, sealed, **kwargs):
            return WorkspaceScoreResult(
                release_id=package.manifest.release_id,
                task_id=package.task_id,
                task_content_hash=package.task.content_hash(),
                artifact_sha256=sealed.submission.artifact_sha256,
                valid_submission=False,
                reward=0.0,
                failure=WorkspaceFailure(
                    classification=__import__(
                        "elt_taskgen.training.contract", fromlist=["WorkspaceFailureClass"]
                    ).WorkspaceFailureClass.POLICY_VIOLATION,
                    error_code=WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, scorer=mutated)
            env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assertIsNone(step.result)
            self.assertIn(step.harness_fault, S.INFRASTRUCTURE_FAULT_NAMES)
            self.assertFalse(step.signal.label_valid)
            self.assertIsNone(step.signal.reward)

    # -- finding 2-2: policy-shaped NAMES are measured, host faults are not --

    def _never(self):
        calls = [0]

        def never(*args, **kwargs):
            calls[0] += 1
            raise AssertionError("the grader must not run for a refused artifact")

        return never, calls

    def assert_measured(self, step, code):
        self.assertTrue(step.done)
        self.assertEqual(step.harness_fault, "")
        self.assertIsNotNone(step.result)
        self.assertFalse(step.result.valid_submission)
        self.assertEqual(step.result.failure.error_code, code)
        self.assertTrue(step.signal.label_valid)
        self.assertEqual(step.signal.reward, 0.0)

    def assert_unlabelled(self, step, *, fault: str, code: str):
        self.assertTrue(step.done)
        self.assertIsNone(step.result)
        self.assertEqual(step.harness_fault, fault)
        self.assertEqual(step.harness_fault_code, code)
        self.assertIn(step.harness_fault, S.INFRASTRUCTURE_FAULT_NAMES)
        self.assertFalse(step.signal.label_valid)
        self.assertIsNone(step.signal.reward)
        self.assertNotEqual(step.signal.reward, 0.0)

    def test_nul_byte_key_is_a_measured_path_escape_never_a_crash(self):
        """Finding 2-2: a NUL byte (or any C0 control character) in a key
        used to escape `step` as `ValueError: embedded null byte` from
        `write_bytes`, with the episode already marked stepped. It is a
        measured `WORKSPACE_PATH_ESCAPE`; the grader never runs."""
        never, calls = self._never()
        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory), scorer=never)
            for key in ("elt/a\x00b", "elt/models/a\x01b.sql", "elt/main.tf\x7f", "\x00"):
                env.reset(TASK_ID)
                step = env.step({key: b"x", "elt/main.tf": b"terraform {}\n"})
                self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE)
                self.assertTrue(step.signal.policy_violation, msg=repr(key))
            self.assertEqual(calls[0], 0)

    def test_non_utf8_text_value_is_a_measured_submission_invalid_never_a_crash(self):
        """Finding 2-2 (residual): a text value that is not UTF-8 encodable
        (a lone surrogate) used to escape `step` as `UnicodeEncodeError`.
        It is the policy's own bytes: a measured `SUBMISSION_INVALID`; the
        grader never runs and the group is not dropped."""
        never, calls = self._never()
        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory), scorer=never)
            for value in ("\udcff", "select 1 -- \ud800", "\udc00" * 3):
                env.reset(TASK_ID)
                step = env.step({"elt/models/a.sql": value, "elt/main.tf": b"terraform {}\n"})
                self.assert_measured(step, WorkspaceErrorCode.SUBMISSION_INVALID)
                self.assertTrue(step.signal.label_valid, msg=repr(value))
            self.assertEqual(calls[0], 0)

    def test_over_long_path_component_is_a_measured_label_not_a_harness_fault(self):
        """Finding 2-2 ("None farming"): a >255-byte file name used to reach
        `write_bytes` as `ENAMETOOLONG`, caught as the harness-fault
        `artifact_write_io` and DROPPING the group. It is the policy's own
        name: a measured `WORKSPACE_PATH_ESCAPE` before any write — and even
        a write that fails `ENAMETOOLONG`/`EINVAL` is measured, not dropped."""
        never, calls = self._never()
        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory), scorer=never)
            env.reset(TASK_ID)
            step = env.step({"elt/" + "a" * 300: b"x"})
            self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE)
            # 255 single-byte characters fit; 128 three-byte characters do not.
            env.reset(TASK_ID)
            step = env.step({"elt/models/" + "€" * 128 + ".sql": b"x"})
            self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE)
            # The write-time narrowing: a policy-attributable errno is measured.
            for code in (errno.ENAMETOOLONG, errno.EINVAL):
                env.reset(TASK_ID)
                with mock.patch.object(Path, "write_bytes", side_effect=OSError(code, "name")):
                    step = env.step(ARTIFACT)
                self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE)
            # A `ValueError` from the writer (an embedded NUL the normalizer
            # did not see) is measured too, never an escaping exception.
            env.reset(TASK_ID)
            with mock.patch.object(Path, "write_bytes", side_effect=ValueError("embedded null byte")):
                step = env.step(ARTIFACT)
            self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE)
            self.assertEqual(calls[0], 0)

    def test_normalization_collision_is_a_case_collision_on_every_host(self):
        """Finding 2-2 (host divergence): an NFC and an NFD `é` collapse into
        ONE file on APFS and two on ext4. Both keys together are a
        `WORKSPACE_CASE_COLLISION` everywhere, and a lone NFD key is written
        in NFC so the sealed manifest is host-independent."""
        never, calls = self._never()
        nfc = "elt/models/café.sql"
        nfd = "elt/models/café.sql"
        self.assertNotEqual(nfc, nfd)
        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory), scorer=never)
            env.reset(TASK_ID)
            step = env.step({nfc: b"select 1", nfd: b"select 2"})
            self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_CASE_COLLISION)
            # Case-folding and normalization compose: `CAFÉ` (NFD) vs `café`.
            env.reset(TASK_ID)
            step = env.step({nfc: b"select 1", "elt/models/CAFÉ.sql": b"select 2"})
            self.assert_measured(step, WorkspaceErrorCode.WORKSPACE_CASE_COLLISION)
            self.assertEqual(calls[0], 0)
            # A lone NFD key seals under its NFC name.
            env.reset(TASK_ID)
            step = env.step({**ARTIFACT, nfd: b"select 3"})
            self.assertIsNone(step.result)  # the never-scorer is not reached in-process
            self.assertEqual(step.harness_fault, "ToolHarnessFault")
            sealed = load_sealed_workspace(step.sealed_dir, expected_seal_sha256=step.seal_sha256)
            self.assertIn(nfc, [f.path for f in sealed.submission.files])
            self.assertNotIn(nfd, [f.path for f in sealed.submission.files])

    def test_artifact_write_io_fault_is_a_harness_fault_not_a_zero(self):
        """Finding 2-2: an `OSError` the HOST caused while writing the
        artifact (`ENOSPC`, `EACCES`, `EIO`, `EMFILE`) is `SandboxFault` /
        `artifact_write_io`: result None, label None — never a 0.0."""
        from elt_taskgen.training import env as E

        never, calls = self._never()
        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory), scorer=never)
            env.reset(TASK_ID)
            with mock.patch.object(E, "_write_artifact", side_effect=OSError(errno.ENOSPC, "no space")):
                step = env.step(ARTIFACT)
            self.assert_unlabelled(step, fault="SandboxFault", code="artifact_write_io")
            self.assertEqual(step.seal_sha256, "")
            for code in (errno.EACCES, errno.EIO, errno.EMFILE):
                env.reset(TASK_ID)
                with mock.patch.object(Path, "write_bytes", side_effect=OSError(code, "host")):
                    step = env.step(ARTIFACT)
                self.assert_unlabelled(step, fault="SandboxFault", code="artifact_write_io")
            self.assertEqual(calls[0], 0)
            self.assertTrue(drop_group_if_unlabelled([step.signal]))

    # -- finding 2-3: the REAL grader is an env-owned, killable child --------

    def test_default_grader_runs_the_public_scorer_in_an_env_owned_child(self):
        """Finding 2-3: with no scorer injected, the public `score_workspace`
        runs in a child process the env owns (its own session), reloading
        the package through the public loader and answering with the same
        classified result the in-process grader would."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root)
            observation = env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assertEqual(step.harness_fault, "")
            self.assertIsNotNone(step.result)
            self.assertTrue(step.result.valid_submission)
            self.assertTrue(step.signal.label_valid)
            self.assertEqual(step.signal.reward, 0.0)
            self.assertEqual(step.signal.first_failed_phase, "terraform")
            self.assertEqual(sorted(step.result.populations), sorted(step.result.graded_populations))
            supervisor = observation.attempt_root.parent / "supervisor"
            self.assertTrue((supervisor / "grader_result.json").is_file())
            self.assertFalse((supervisor / "grader_fault.json").exists())
            self.assertEqual(_grader_threads(), [])
            # The same public grader through a pid-recording entry: another pid.
            env = self._env(root, grader_entry=_entry(_pid_reporting_grader))
            observation = env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assertEqual(step.harness_fault, "")
            self.assertEqual(step.signal.reward, 0.0)
            pid = int((observation.attempt_root.parent / "grader" / "pid.txt").read_text())
            self.assertNotEqual(pid, os.getpid())
            self.assertFalse(_alive(pid))

    def test_grader_child_faults_are_reported_by_class_never_by_text(self):
        """Finding 2-3: a grader child that RAISES is `ToolHarnessFault`
        naming the exception class only; one that answers a non-result is
        `non_result`; the step carries no text from either."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, grader_entry=_entry(_raising_grader))
            observation = env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assert_unlabelled(step, fault="ToolHarnessFault", code="harness_exception")
            fault = json.loads((observation.attempt_root.parent / "supervisor" / "grader_fault.json").read_text())
            self.assertEqual(fault, {"code": "harness_exception", "cause_type": "RuntimeError"})
            env = self._env(root, grader_entry=_entry(_non_result_grader))
            env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assert_unlabelled(step, fault="ToolHarnessFault", code="non_result")
            # An entry that cannot even be imported is a harness fault too.
            env = self._env(root, grader_entry=f"{_ENTRY_MODULE}:_no_such_grader")
            env.reset(TASK_ID)
            step = env.step(ARTIFACT)
            self.assert_unlabelled(step, fault="ToolHarnessFault", code="harness_exception")

    def test_grader_deadline_kills_the_env_owned_child_process_group(self):
        """Finding 2-3 (the cascade half): at the deadline the supervisor
        KILLS the grader child's process group — and the session the grader
        detached the way `dbt_runner` spawns dbt — and waits, so no grader
        thread and no grader subprocess survives to contend with the group's
        re-sample. The label is None, never 0.0."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._env(root, grader_deadline_s=4.0, grader_entry=_entry(_stall_with_detached_grandchild))
            observation = env.reset(TASK_ID)
            started = time.monotonic()
            step = env.step(ARTIFACT)
            elapsed = time.monotonic() - started
            self.assert_unlabelled(step, fault="ToolDeadlineExceeded", code="deadline_exceeded")
            self.assertEqual(step.signal.first_failed_phase, "none")
            self.assertTrue(drop_group_if_unlabelled([step.signal]))
            self.assertGreaterEqual(elapsed, 4.0)
            pids_path = observation.attempt_root.parent / "grader" / "pids.json"
            self.assertTrue(pids_path.is_file(), "the child never reached the grader entry")
            pids = json.loads(pids_path.read_text())
            self.assertNotEqual(pids["child"], os.getpid())
            for name, pid in pids.items():
                self.assertTrue(_wait_dead(int(pid)), f"{name} pid {pid} survived the deadline")
            self.assertEqual(_grader_threads(), [])
            # The seal was taken before the grader ran: the trajectory is
            # identified, and a fresh episode still answers.
            self.assertEqual(step.seal_sha256, load_sealed_workspace(step.sealed_dir).seal_sha256)
            env = self._env(root)
            env.reset(TASK_ID)
            self.assertEqual(env.step(ARTIFACT).signal.reward, 0.0)

    # -- finding 2-0: a seal- or replay-time I/O fault is never a 0.0 -------

    def test_seal_time_resolve_io_fault_is_a_harness_fault_not_a_zero(self):
        """Finding 2-0 (probe C): an `EIO` from `Path.resolve(strict=True)`
        inside `_verify_attempt` at SEAL time is swallowed by `workspace.py`
        and reported as a deliberate `_fail(PATH_ESCAPE)` with NO `OSError`
        cause. The code set — not the `__cause__` — identifies it: the policy
        only writes regular files beneath elt/, so a seal-time path escape is
        the harness's, and the label is None."""
        real_resolve = Path.resolve
        tripped = [0]

        def eio_in_verify_attempt(self_, strict=False):
            if strict and sys._getframe(1).f_code.co_name == "_verify_attempt" and not tripped[0]:
                tripped[0] += 1
                raise OSError(errno.EIO, "input/output error")
            return real_resolve(self_, strict=strict)

        with tempfile.TemporaryDirectory() as directory:
            env = self._env(Path(directory))  # the grader is never reached
            env.reset(TASK_ID)
            with mock.patch.object(Path, "resolve", eio_in_verify_attempt):
                step = env.step(ARTIFACT)
            self.assertEqual(tripped[0], 1)
            self.assert_unlabelled(step, fault="SandboxFault", code="seal_io")
            self.assertEqual(step.seal_sha256, "")

    def test_replay_time_io_fault_inside_the_real_grader_is_a_harness_fault(self):
        """Finding 2-0 (probes B, B2, D, E): a real `OSError` raised INSIDE
        `replay_workspace` through the real `score_workspace` — re-hashing a
        public file, reading a candidate file, `resolve(strict=True)` in
        `_verify_attempt`, and `os.scandir` in `load_sealed_workspace` (which
        `workspace.py` maps to the label-eligible `SUBMISSION_INVALID`) — is
        unlabelled: the artifact was already sealed and accepted, the policy
        is gone, so the grader's workspace failure is the harness's."""
        real_open, real_scandir, real_resolve = os.open, os.scandir, Path.resolve

        def on_stack(name: str) -> bool:
            return any(frame.name == name for frame in traceback.extract_stack())

        def caller_is(name: str) -> bool:
            return sys._getframe(2).f_code.co_name == name

        def open_fault(caller: str, needle: str):
            state = {"tripped": 0}

            def fake_open(path, *a, **k):
                if not state["tripped"] and caller_is(caller) and needle in str(path) and on_stack("replay_workspace"):
                    state["tripped"] += 1
                    raise OSError(errno.EMFILE, "too many open files")
                return real_open(path, *a, **k)

            return mock.patch.object(os, "open", fake_open), state

        def resolve_fault():
            state = {"tripped": 0}

            def fake_resolve(self_, strict=False):
                if not state["tripped"] and strict and sys._getframe(1).f_code.co_name == "_verify_attempt" and on_stack("replay_workspace"):
                    state["tripped"] += 1
                    raise OSError(errno.EIO, "input/output error")
                return real_resolve(self_, strict=strict)

            return mock.patch.object(Path, "resolve", fake_resolve), state

        def scandir_fault():
            state = {"tripped": 0}

            def fake_scandir(path):
                if not state["tripped"] and sys._getframe(1).f_code.co_name == "load_sealed_workspace" and on_stack("replay_workspace"):
                    state["tripped"] += 1
                    raise OSError(errno.EMFILE, "too many open files")
                return real_scandir(path)

            return mock.patch.object(os, "scandir", fake_scandir), state

        cases = {
            "public file re-hash (_bounded_file_digest)": open_fault("_bounded_file_digest", "documentation"),
            "candidate file read (_read_bounded_regular_file)": open_fault("_read_bounded_regular_file", "elt/"),
            "resolve(strict=True) in _verify_attempt": resolve_fault(),
            "os.scandir in load_sealed_workspace": scandir_fault(),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, (patch, state) in cases.items():
                with self.subTest(site=label):
                    events: list[tuple[str, str]] = []
                    env = self._env(root, scorer=functools.partial(score_workspace, _dependencies=_doubles(events)))
                    env.reset(TASK_ID)
                    with patch:
                        step = env.step(ARTIFACT)
                    self.assertEqual(state["tripped"], 1, msg=label)
                    self.assert_unlabelled(step, fault="SandboxFault", code="grader_workspace_io")
                    self.assertNotEqual(step.seal_sha256, "")
            # Control: the same doubles with no fault injected score 1.0.
            events = []
            env = self._env(root, scorer=functools.partial(score_workspace, _dependencies=_doubles(events)))
            env.reset(TASK_ID)
            self.assertEqual(env.step(ARTIFACT).signal.reward, 1.0)


if __name__ == "__main__":
    unittest.main()

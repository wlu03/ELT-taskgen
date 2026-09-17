"""C2 pinned as a test: ordinary RLVR rollouts perform zero cloud queries.

``docs/EXECUTION_MODEL.md`` and ``docs/plans/cloud_free_elt_agent_rlvr.md``
state it; roadmap §8 makes it Phase 5's Definition-of-done item (3):
``test_ordinary_rollout_path_makes_zero_cloud_calls``. Real warehouse
execution is SPARSE CERTIFICATION on its own lane — never part of a rollout —
so a complete ordinary rollout (``DeclarativeEltEnv.reset`` -> ``step`` ->
the public ``score_workspace`` over the committed fixture release) must run
end to end with the network amputated.

The amputation is real, not simulated:

* the PARENT's ``socket`` AND ``_socket`` constructors are patched to raise —
  around BOTH legs of the rollout — which covers the environment, the seal,
  ``training_signal``, the ``score_in_child`` plumbing and the in-process
  grader leg;
* the env's ordinary grader runs in a CHILD PROCESS (``score_in_child``),
  which a parent-side patch cannot reach, so the child is armed through a
  ``sitecustomize`` guard handed down on ``PYTHONPATH``. The guard writes a
  marker so the test can prove it was actually loaded in that child, and a
  separate probe process proves the guard refuses a socket.

If any step of the rollout reached the network it would raise, and the env
would report a harness fault instead of a label: ``harness_fault == ""`` with
a real label IS the pin. The pin covers every Python-level socket in the
rollout process tree — ``_socket``, the C accelerator, by its own name as
well as the ``socket`` wrappers that subclass it (finding p5-9) — while a
C-level client linked into a native extension is out of its reach, so the
companion test also proves no warehouse client module is even imported by the
rollout path.
"""

from __future__ import annotations

import _socket
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen.training.dbt_runner import DbtRuntimeConfig
from elt_taskgen.training.env import DeclarativeEltEnv
from elt_taskgen.training.models import WorkspaceScoreResult
from elt_taskgen.training.scorer import score_workspace


FIXTURE_RELEASE = (
    Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"
)
TASK_ID = "gate__five_backend_probe"

#: The same declarative artifact the Phase 2 env tests emit: a policy's
#: ``elt/`` tree, scored by the real grader with no dbt runtime configured.
ARTIFACT = {
    "elt/main.tf": b"terraform {\n  required_providers {}\n}\n",
    "elt/dbt_project.yml": (
        b"name: gate_task\nversion: '1.0'\nprofile: elt_taskgen\n"
        b"model-paths: ['models']\n"
    ),
    "elt/models/sources.yml": (
        b"version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n"
    ),
    "models/customer_rollup.sql": (
        b"{{ config(materialized='table') }}\n"
        b"select * from {{ source('raw', 'customers') }}\n"
    ),
}

#: Distribution roots that only exist to talk to a cloud service. None of them
#: may be imported by the ordinary rollout path.
CLOUD_CLIENT_ROOTS = (
    "airbyte",
    "boto3",
    "botocore",
    "databricks",
    "google",
    "redshift_connector",
    "requests",
    "snowflake",
    "urllib3",
)

#: The repository's own cloud modules: the sparse certification lane's, never
#: a rollout's.
CLOUD_RUNTIME_MODULES = (
    "elt_taskgen.runtime.airbyte",
    "elt_taskgen.runtime.databricks",
    "elt_taskgen.runtime.redshift",
    "elt_taskgen.runtime.snowflake",
)

_GUARD = '''\
"""Refuse every socket in this process: the C2 pin's amputated network.

Installed as ``sitecustomize`` on the grader child's ``PYTHONPATH``. A
``sitecustomize`` further along the path (Homebrew ships one that shuffles
``sys.path`` and the prefixes) is executed FIRST, so the child this test
measures is the ordinary child in every respect except its network.
"""

import _socket
import importlib.util
import os
import socket
import sys

_here = os.path.dirname(os.path.abspath(__file__))
for _entry in list(sys.path):
    if not _entry or os.path.abspath(_entry) == _here:
        continue
    _candidate = os.path.join(_entry, "sitecustomize.py")
    if os.path.isfile(_candidate):
        try:
            _spec = importlib.util.spec_from_file_location(
                "_shadowed_sitecustomize", _candidate
            )
            _module = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_module)
        except BaseException:  # a delegate that fails must not arm nothing
            pass
        break


class ZeroCloudViolation(RuntimeError):
    """A rollout process attempted a network call."""


def _refuse(*args, **kwargs):
    raise ZeroCloudViolation("zero-cloud rollout: a network call was attempted")


# `_socket` FIRST: it is the C accelerator `socket.socket` subclasses, it is a
# plain Python-level import, and rebinding only the `socket` wrappers left it
# fully usable — `import _socket; _socket.socket(...)` opened a real socket
# under a guard that reported itself armed (finding p5-9).
_socket.socket = _refuse
_socket.getaddrinfo = _refuse
_socket.socketpair = _refuse
socket.socket = _refuse
socket.create_connection = _refuse
socket.create_server = _refuse
socket.getaddrinfo = _refuse
socket.socketpair = _refuse

_marker = os.environ.get("ELT_ZERO_CLOUD_MARKER")
if _marker:
    with open(_marker, "a", encoding="utf-8") as handle:
        handle.write("armed\\n")
'''

_PROBE = textwrap.dedent(
    """
    import _socket, socket, sys

    def refused(call):
        try:
            call()
        except Exception as exc:
            return type(exc).__name__
        return ""

    names = [
        refused(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
        # The C accelerator, by its own name: the bypass finding p5-9 found.
        refused(lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)),
        refused(lambda: _socket.getaddrinfo("127.0.0.1", 80)),
        refused(lambda: socket.getaddrinfo("127.0.0.1", 80)),
        refused(lambda: socket.socketpair()),
    ]
    if all(name == "ZeroCloudViolation" for name in names):
        sys.stdout.write("ZeroCloudViolation")
        raise SystemExit(0)
    sys.stdout.write(",".join(name or "OPEN" for name in names))
    raise SystemExit(1)
    """
)

_IMPORT_PROBE = textwrap.dedent(
    """
    import json, sys
    import elt_taskgen.training.env  # the ordinary rollout entry point
    import elt_taskgen.training.scorer
    import elt_taskgen.training.signal
    roots = {name.split(".")[0] for name in sys.modules}
    sys.stdout.write(json.dumps(sorted(roots | set(sys.modules))))
    """
)


class ZeroCloudViolation(RuntimeError):
    """A rollout step in THIS process attempted a network call."""


def _refuse(*args, **kwargs):
    raise ZeroCloudViolation("zero-cloud rollout: a network call was attempted")


@contextlib.contextmanager
def _amputated_parent_sockets():
    """Every Python-level socket constructor in THIS process refuses.

    `_socket` included (finding p5-9): rebinding only the `socket` wrappers
    left the C accelerator reachable by its own name, so the amputation the
    module docstring claims was not the amputation the test performed.
    """
    with mock.patch.object(socket, "socket", _refuse), mock.patch.object(
        socket, "create_connection", _refuse
    ), mock.patch.object(socket, "getaddrinfo", _refuse), mock.patch.object(
        socket, "socketpair", _refuse
    ), mock.patch.object(
        socket, "create_server", _refuse
    ), mock.patch.object(
        _socket, "socket", _refuse
    ), mock.patch.object(
        _socket, "getaddrinfo", _refuse
    ):
        yield


def _public_grader(*args, **kwargs) -> WorkspaceScoreResult:
    """The PUBLIC ``score_workspace``, wired through the env's injection seam
    so it runs IN THIS PROCESS under the parent's amputated socket module
    (the env sends the un-wrapped function to a child instead)."""
    return score_workspace(*args, **kwargs)


class ZeroCloudRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.guard_dir = self.root / "netguard"
        self.guard_dir.mkdir()
        (self.guard_dir / "sitecustomize.py").write_text(_GUARD, encoding="utf-8")
        self.marker = self.root / "guard-marker.txt"
        # ``env.score_in_child`` hands the PARENT's ``sys.path`` down to the
        # grader child as its PYTHONPATH, so the guard must lead that path to
        # win over the interpreter's own ``sitecustomize``. Nothing but
        # ``sitecustomize`` lives in the guard directory, so the parent's own
        # imports are unaffected.
        self._saved_path = list(sys.path)
        sys.path.insert(0, str(self.guard_dir))

    def tearDown(self) -> None:
        sys.path[:] = self._saved_path
        self._temporary.cleanup()

    def _env(self, root: Path, **kwargs) -> DeclarativeEltEnv:
        return DeclarativeEltEnv(
            FIXTURE_RELEASE,
            attempts_root=root / "episodes",
            runtime_config=DbtRuntimeConfig(
                python=root / "unused-python", manifest=root / "unused-runtime.json"
            ),
            **kwargs,
        )

    def _child_environment(self) -> dict[str, str]:
        return {
            "PYTHONPATH": os.pathsep.join(
                [str(self.guard_dir), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            "ELT_ZERO_CLOUD_MARKER": str(self.marker),
        }

    def test_ordinary_rollout_path_makes_zero_cloud_calls(self) -> None:
        overrides = self._child_environment()
        with mock.patch.dict(os.environ, overrides):
            # The guard really refuses: a probe process started with exactly
            # the environment the grader child inherits cannot open a socket.
            probe = subprocess.run(
                [sys.executable, "-c", _PROBE],
                capture_output=True,
                text=True,
                timeout=120,
                # Its own marker path: `self.marker` must be written by the
                # GRADER CHILD and by nothing else.
                env={**os.environ, "ELT_ZERO_CLOUD_MARKER": str(self.root / "probe")},
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "ZeroCloudViolation")
            self.assertTrue((self.root / "probe").is_file())
            self.assertFalse(self.marker.exists())

            # Ordinary reset/step grading with network disabled in both child
            # and parent, covering the former parent-socket gap.
            episode_root = self.root / "ordinary"
            with _amputated_parent_sockets():
                env = self._env(episode_root)
                observation = env.reset(TASK_ID)
                self.assertEqual(observation.task_id, TASK_ID)
                step = env.step(ARTIFACT)
            self.assertEqual(step.harness_fault, "")
            self.assertEqual(step.harness_fault_code, "")
            self.assertIsNotNone(step.result)
            self.assertTrue(step.signal.label_valid)
            self.assertIsNotNone(step.signal.reward)
            self.assertTrue(step.result.valid_submission)
            supervisor = observation.attempt_root.parent / "supervisor"
            self.assertTrue((supervisor / "grader_result.json").is_file())
            self.assertFalse((supervisor / "grader_fault.json").exists())
            # The guard was loaded in the grader child, so its clean answer is
            # evidence that the grader made no network call.
            self.assertTrue(self.marker.is_file())
            self.assertIn("armed", self.marker.read_text(encoding="utf-8"))

            # 2. THE SAME ROLLOUT IN PROCESS: the public score_workspace under
            #    the parent's amputated socket module.
            with _amputated_parent_sockets():
                with self.assertRaises(ZeroCloudViolation):
                    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                with self.assertRaises(ZeroCloudViolation):
                    _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                in_process = self._env(
                    self.root / "in-process", scorer=_public_grader
                )
                in_process.reset(TASK_ID)
                local_step = in_process.step(ARTIFACT)
            self.assertEqual(local_step.harness_fault, "")
            self.assertEqual(local_step.harness_fault_code, "")
            self.assertIsNotNone(local_step.result)
            self.assertTrue(local_step.signal.label_valid)
            self.assertIsNotNone(local_step.signal.reward)
            # Both legs of the same rollout agree: the grader's judgement does
            # not depend on where it ran, only on the sealed artifact.
            self.assertEqual(
                local_step.result.artifact_sha256, step.result.artifact_sha256
            )
            self.assertEqual(local_step.signal.reward, step.signal.reward)
            self.assertEqual(
                local_step.signal.first_failed_phase, step.signal.first_failed_phase
            )
            env.close()
            in_process.close()

    def test_rollout_path_imports_no_warehouse_client(self) -> None:
        """The socket pin cannot see a client linked into a C extension, so
        pin the import graph too: nothing a rollout imports can reach a
        warehouse. The cloud connectors are the certification lane's."""
        probe = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROBE],
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        loaded = set(__import__("json").loads(probe.stdout))
        for root in CLOUD_CLIENT_ROOTS:
            self.assertNotIn(root, loaded)
        for module in CLOUD_RUNTIME_MODULES:
            self.assertNotIn(module, loaded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for runtime/process.py: bounded subprocess capture.

WHY THIS EXISTS
SubprocessRunner is shared runtime infrastructure (bootstrap, execution,
source environments, DockerRunner's host side).  Its capture must retain only
the last ``max_output_bytes`` of each stream while the child runs, so a
chatty or adversarial child cannot drive the parent to arbitrary RSS before
truncation, and every public behavior — tail semantics, timeout message,
nonzero-exit message, success result — must match the historical
``subprocess.run`` implementation exactly.
"""

from __future__ import annotations

import inspect
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from elt_taskgen.runtime import bootstrap, execution, source_environment
from elt_taskgen.runtime.process import (
    CLOUD_EGRESS_NETWORK,
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    DOCKER_LANE_CLOUD_EGRESS,
    DOCKER_LANE_NONE,
    DOCKER_LANE_PROXY_BRIDGE,
    PROXY_BRIDGE_NETWORK,
    CommandResult,
    DockerRunner,
    ProcessFailure,
    ProcessTimeout,
    SandboxArgvError,
    SubprocessRunner,
    lint_sandbox_argv,
)


def _rusage_self_bytes() -> int:
    """RUSAGE_SELF high-water mark, normalized to bytes."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


class TestBoundedSubprocessCapture(unittest.TestCase):
    def test_chatty_subprocess_capture_is_bounded(self) -> None:
        cap = 4096
        marker = "END-OF-CHATTY-STREAM"
        emitted = 64 * 1024 * 1024
        script = (
            "import sys\n"
            "chunk = 'x' * 65536\n"
            f"for _ in range({emitted // 65536}):\n"
            "    sys.stdout.write(chunk)\n"
            f"sys.stdout.write({marker!r})\n"
        )
        before = _rusage_self_bytes()
        result = SubprocessRunner(max_output_bytes=cap).run(
            [sys.executable, "-c", script]
        )
        after = _rusage_self_bytes()
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), cap)
        # Tail semantics identical to the legacy [-cap:] slice.
        self.assertTrue(result.stdout.endswith(marker))
        self.assertEqual(result.stdout, ("x" * (cap - len(marker))) + marker)
        # Bounded rolling capture: the parent's high-water mark must grow by
        # far less than the 64 MiB the child emitted.
        self.assertLess(after - before, emitted // 2)

    def test_timeout_kills_chatty_child_with_legacy_message(self) -> None:
        script = (
            "import sys\n"
            "while True:\n"
            "    sys.stdout.write('y' * 8192)\n"
        )
        runner = SubprocessRunner(timeout=1.0, max_output_bytes=4096)
        start = time.monotonic()
        with self.assertRaisesRegex(ProcessFailure, "exceeded its runtime limit"):
            runner.run([sys.executable, "-c", script])
        # The child is killed and reaped and the reader threads are joined:
        # the call returns promptly instead of hanging on a full pipe.
        self.assertLess(time.monotonic() - start, 15.0)

    def test_nonzero_exit_and_success_semantics_preserved(self) -> None:
        runner = SubprocessRunner(max_output_bytes=65536)
        try:
            runner.run(
                [
                    sys.executable,
                    "-c",
                    "print('sekret-output'); raise SystemExit(3)",
                ]
            )
        except ProcessFailure as exc:
            self.assertIn("failed with exit code 3", str(exc))
            self.assertNotIn("sekret-output", str(exc))
        else:
            self.fail("expected ProcessFailure for a nonzero exit code")
        result = runner.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('out-text'); "
                "sys.stderr.write('err-text')",
            ]
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "out-text")
        self.assertEqual(result.stderr, "err-text")


class TestExecutionBounds(unittest.TestCase):
    """Roadmap Phase 0.A: a required positive runner timeout, process-group
    kill on deadline, the Docker lane parameter, and the sandbox argv lint."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_process_group_kill_on_timeout(self) -> None:
        """A grandchild that outlives the direct child must die with the
        deadline: the runner kills the child's whole session, not one pid."""
        pid_file = self.root / "grandchild.pid"
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(120)\n"
        )
        runner = SubprocessRunner(timeout=1.5, max_output_bytes=4096)
        start = time.monotonic()
        with self.assertRaisesRegex(ProcessTimeout, "exceeded its runtime limit"):
            runner.run([sys.executable, "-c", script])
        self.assertLess(time.monotonic() - start, 15.0)
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            # A killed orphan may linger as a zombie until init reaps it;
            # a zombie is dead for every purpose that matters here.
            state = subprocess.run(
                ["/bin/ps", "-o", "stat=", "-p", str(grandchild)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.1)
        else:
            os.kill(grandchild, 9)
            self.fail("grandchild survived the runner's deadline")

    def test_execution_never_defaults_to_bare_subprocess_runner(self) -> None:
        """``SubprocessRunner()`` is bounded by construction and refuses an
        unbounded or non-positive timeout, so every ``runner or
        SubprocessRunner()`` default site in the runtime package is bounded."""
        default = inspect.signature(SubprocessRunner.__init__).parameters["timeout"].default
        self.assertIsInstance(default, float)
        self.assertGreater(default, 0.0)
        self.assertEqual(default, DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
        self.assertEqual(SubprocessRunner().timeout, DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
        for bad in (None, 0, -1.0, float("nan"), float("inf") * 0, "60", True):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                SubprocessRunner(timeout=bad)  # type: ignore[arg-type]
        workspace = self.root / "attempt"
        workspace.mkdir()
        docker = DockerRunner(workspace, "runner@sha256:" + "a" * 64)
        self.assertIsInstance(docker.host_runner, SubprocessRunner)
        self.assertGreater(docker.host_runner.timeout, 0.0)
        with self.assertRaises(ValueError):
            DockerRunner(workspace, "runner@sha256:" + "a" * 64, timeout=0)
        for module in (execution, bootstrap, source_environment):
            source = inspect.getsource(module)
            self.assertNotIn("timeout=None", source, module.__name__)
            # Every default site is `SubprocessRunner()` (bounded by
            # construction) or, for the abctl bootstrap whose image pulls can
            # outlast the one-hour default, `SubprocessRunner(timeout=timeout)`
            # with a validated positive bound.
            self.assertRegex(source, r"SubprocessRunner\((?:timeout=timeout)?\)", module.__name__)
        self.assertGreater(bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS, DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)

    def test_no_runner_construction_in_src_passes_an_unbounded_timeout(self) -> None:
        """Roadmap 0.A: every ``SubprocessRunner(...)`` / ``DockerRunner(...)``
        construction under ``src/`` is bounded. No caller passes
        ``timeout=None``, ``0`` or a negative literal (the constructor would
        refuse it, so this pins that no caller even tries), and the one path
        that legitimately outlasts the one-hour default — the abctl
        bootstrap's image pulls — carries an explicit, documented three-hour
        constant rather than an unbounded wait."""
        import ast

        src_root = Path(inspect.getsourcefile(bootstrap)).resolve().parents[2]
        self.assertEqual(src_root.name, "src")
        runner_names = {"SubprocessRunner", "DockerRunner"}
        constructions: list[str] = []
        for module_path in sorted(src_root.rglob("*.py")):
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                else:
                    continue
                if name not in runner_names:
                    continue
                site = f"{module_path.relative_to(src_root)}:{node.lineno}"
                constructions.append(site)
                for keyword in node.keywords:
                    if keyword.arg != "timeout":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Constant):
                        literal = value.value
                        self.assertIsNotNone(literal, f"{site} passes timeout=None")
                        self.assertNotIsInstance(literal, bool, site)
                        self.assertIsInstance(literal, (int, float), site)
                        self.assertGreater(literal, 0, f"{site} passes a non-positive timeout")
                    elif isinstance(value, ast.UnaryOp):
                        self.fail(f"{site} passes a negative timeout literal")
        # The known default sites are all seen (the scan is not vacuous).
        for expected in (
            "elt_taskgen/runtime/bootstrap.py",
            "elt_taskgen/runtime/execution.py",
            "elt_taskgen/runtime/source_environment.py",
            "elt_taskgen/runtime/process.py",
            "elt_taskgen/cli.py",
        ):
            self.assertTrue(
                any(site.startswith(expected) for site in constructions), expected
            )
        self.assertGreaterEqual(len(constructions), 8)
        self.assertEqual(bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS, 3 * 3600.0)
        self.assertEqual(
            inspect.signature(bootstrap.install_airbyte).parameters["timeout"].default,
            bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS,
        )
        # The bootstrap bound goes through the same validation as every other.
        self.assertEqual(
            SubprocessRunner(timeout=bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS).timeout,
            bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS,
        )

    def test_docker_runner_network_none_for_local_tools(self) -> None:
        workspace = self.root / "attempt"
        (workspace / "elt").mkdir(parents=True)
        image = "runner@sha256:" + "b" * 64

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def run(self, argv, *, cwd=None, env=None, stdin_path=None):
                self.calls.append(tuple(str(v) for v in argv))
                return CommandResult(0)

        host = Recorder()
        DockerRunner(workspace, image, host_runner=host).run(
            ("terraform", "validate"), cwd=workspace / "elt"
        )
        command = host.calls[0]
        self.assertEqual(command[:3], ("docker", "run", "--rm"))
        self.assertEqual(command[-3:], (image, "terraform", "validate"))
        options = command[: command.index(image)]
        network = options.index("--network")
        self.assertEqual(options[network + 1], "none")
        self.assertNotIn("--add-host", options)
        self.assertNotIn("host.docker.internal:host-gateway", options)
        user = options.index("--user")
        self.assertRegex(options[user + 1], r"^[0-9]+:[0-9]+$")
        self.assertFalse(options[user + 1].startswith("0:"))
        self.assertIn("--cidfile", options)
        self.assertIn("--read-only", options)
        self.assertIn("--cap-drop=ALL", options)
        self.assertIn("--security-opt=no-new-privileges", options)

        # The proxy-bridge lane is the ONLY lane that can reach the host
        # gateway, and it is never the default.
        bridged = Recorder()
        DockerRunner(
            workspace, image, host_runner=bridged, lane=DOCKER_LANE_PROXY_BRIDGE
        ).run(("terraform", "validate"))
        options = bridged.calls[0][: bridged.calls[0].index(image)]
        self.assertEqual(options[options.index("--network") + 1], PROXY_BRIDGE_NETWORK)
        self.assertIn("host.docker.internal:host-gateway", options)

        # Warehouse egress is a separate, explicitly named network and never
        # inherits the Airbyte lane's host-gateway mapping.
        cloud = Recorder()
        DockerRunner(
            workspace,
            image,
            host_runner=cloud,
            lane=DOCKER_LANE_CLOUD_EGRESS,
        ).run(("dbt", "run"))
        options = cloud.calls[0][: cloud.calls[0].index(image)]
        self.assertEqual(
            options[options.index("--network") + 1], CLOUD_EGRESS_NETWORK
        )
        self.assertNotIn("--add-host", options)
        self.assertNotIn("host.docker.internal:host-gateway", options)
        self.assertEqual(
            inspect.signature(DockerRunner.__init__).parameters["lane"].default,
            DOCKER_LANE_NONE,
        )
        with self.assertRaisesRegex(ValueError, "unknown sandbox lane"):
            DockerRunner(workspace, image, host_runner=Recorder(), lane="host")
        with self.assertRaisesRegex(ValueError, "must not be root"):
            DockerRunner(workspace, image, host_runner=Recorder(), user="0:0")

        # Environment values are supplied to the docker client out-of-band;
        # only allowlisted variable names can appear in its argv.
        class EnvironmentRecorder(Recorder):
            def __init__(self) -> None:
                super().__init__()
                self.environments: list[dict[str, str] | None] = []

            def run(self, argv, *, cwd=None, env=None, stdin_path=None):
                self.calls.append(tuple(str(v) for v in argv))
                self.environments.append(dict(env) if env else None)
                return CommandResult(0)

        secret = "not-in-docker-argv"
        environment_host = EnvironmentRecorder()
        DockerRunner(
            workspace,
            image,
            host_runner=environment_host,
            lane=DOCKER_LANE_CLOUD_EGRESS,
        ).run(
            ("dbt", "run"),
            env={"ELT_TASKGEN_SNOWFLAKE_PASSWORD": secret},
        )
        environment_command = environment_host.calls[0]
        self.assertNotIn(secret, " ".join(environment_command))
        self.assertIn(
            ("--env", "ELT_TASKGEN_SNOWFLAKE_PASSWORD"),
            list(zip(environment_command, environment_command[1:])),
        )
        self.assertEqual(
            environment_host.environments,
            [{"ELT_TASKGEN_SNOWFLAKE_PASSWORD": secret}],
        )
        with self.assertRaisesRegex(ValueError, "invalid solver environment key"):
            DockerRunner(workspace, image, host_runner=EnvironmentRecorder()).run(
                ("dbt", "run"), env={"BAD-NAME": secret}
            )

        # On a client deadline the container is killed BY ID: a SIGKILL of the
        # docker client alone leaves the container running.
        class TimingOut(Recorder):
            def run(self, argv, *, cwd=None, env=None, stdin_path=None):
                argv = tuple(str(v) for v in argv)
                self.calls.append(argv)
                if argv[:2] == ("docker", "run"):
                    cidfile = Path(argv[argv.index("--cidfile") + 1])
                    cidfile.write_text("c" * 64, encoding="ascii")
                    raise ProcessTimeout("docker exceeded its runtime limit")
                return CommandResult(0)

        killer = TimingOut()
        with self.assertRaises(ProcessTimeout):
            DockerRunner(workspace, image, host_runner=killer).run(("dbt", "run"))
        self.assertEqual(killer.calls[-1], ("docker", "kill", "c" * 64))

    def test_sandbox_argv_lint_refuses_privileged_socket_hostnet(self) -> None:
        refused = (
            ("docker", "run", "--privileged", "img"),
            ("docker", "run", "--network=host", "img"),
            ("docker", "run", "--network", "host", "img"),
            ("docker", "run", "--net=host", "img"),
            ("docker", "run", "--pid=host", "img"),
            ("docker", "run", "--userns=host", "img"),
            ("docker", "run", "--cap-add=SYS_ADMIN", "img"),
            ("docker", "run", "--cap-add", "NET_ADMIN", "img"),
            ("docker", "run", "--device=/dev/mem", "img"),
            ("docker", "run", "--security-opt=seccomp=unconfined", "img"),
            ("docker", "run", "--security-opt", "apparmor=unconfined", "img"),
            ("docker", "run", "-v", "/var/run/docker.sock:/var/run/docker.sock", "img"),
            ("docker", "run", "--volume=/run/docker.sock:/sock", "img"),
            (
                "docker",
                "run",
                "--mount",
                "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                "img",
            ),
        )
        for argv in refused:
            with self.subTest(argv=argv), self.assertRaises(SandboxArgvError):
                lint_sandbox_argv(argv)
        # The runner's own default argv passes its own lint, and a solver
        # command smuggling a switch is refused BEFORE dispatch.
        workspace = self.root / "attempt"
        workspace.mkdir()
        image = "runner@sha256:" + "d" * 64

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def run(self, argv, *, cwd=None, env=None, stdin_path=None):
                self.calls.append(tuple(str(v) for v in argv))
                return CommandResult(0)

        host = Recorder()
        runner = DockerRunner(workspace, image, host_runner=host)
        runner.run(("dbt", "run"))
        lint_sandbox_argv(host.calls[0])
        with self.assertRaises(SandboxArgvError):
            runner.run(("dbt", "run", "--network=host"))
        self.assertEqual(len(host.calls), 1)


if __name__ == "__main__":
    unittest.main()

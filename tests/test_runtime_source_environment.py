from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from elt_taskgen.runtime.process import CommandResult, ProcessFailure
from elt_taskgen.runtime.source_environment import (
    SourceEnvironmentError,
    prepare_source_environment,
    wait_for_airbyte_control_plane,
)
from elt_taskgen.runtime.source_images import SOURCE_SERVICE_IMAGES


class FakeRunner:
    def __init__(self, *, attached_networks: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None, Path | None]] = []
        self.stdin_bytes: list[bytes | None] = []
        self.attached_networks = attached_networks or {}

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        self.calls.append(
            (
                tuple(str(value) for value in argv),
                Path(cwd) if cwd is not None else None,
                Path(stdin_path) if stdin_path is not None else None,
            )
        )
        self.stdin_bytes.append(
            Path(stdin_path).read_bytes() if stdin_path is not None else None
        )
        if tuple(argv[:3]) == ("docker", "inspect", "--format"):
            return CommandResult(0, json.dumps(self.attached_networks))
        return CommandResult(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ReadinessRunner:
    """Return one set of failed component labels per complete probe sample."""

    _COMPONENT_BY_PORT = (
        (":80/", "airbyte-server"),
        (":6443/", "kube-apiserver"),
        (":10259/", "kube-scheduler"),
        (":10257/", "kube-controller-manager"),
    )

    def __init__(
        self, samples: list[set[str]], *, failure_detail: str = "probe failed"
    ) -> None:
        self.samples = samples
        self.failure_detail = failure_detail
        self.calls: list[tuple[str, ...]] = []
        self.sample_index = -1

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        command = tuple(str(value) for value in argv)
        self.calls.append(command)
        endpoint = command[-1]
        component = next(
            label for port, label in self._COMPONENT_BY_PORT if port in endpoint
        )
        if component == "airbyte-server":
            self.sample_index += 1
        failures = self.samples[min(self.sample_index, len(self.samples) - 1)]
        if component in failures:
            raise ProcessFailure(self.failure_detail)
        return CommandResult(0)


class SlowReadinessRunner(ReadinessRunner):
    def __init__(
        self, clock: FakeClock, samples: list[set[str]], *, seconds_per_probe: float
    ) -> None:
        super().__init__(samples)
        self.clock = clock
        self.seconds_per_probe = seconds_per_probe

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        self.clock.now += self.seconds_per_probe
        return super().run(argv, cwd=cwd, env=env, stdin_path=stdin_path)


class InterruptingStartRunner(FakeRunner):
    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        result = super().run(
            argv,
            cwd=cwd,
            env=env,
            stdin_path=stdin_path,
        )
        if tuple(argv[:3]) == ("docker", "network", "connect"):
            raise KeyboardInterrupt
        return result


def _release(tmp_path: Path) -> tuple[Path, str]:
    release = tmp_path / "release"
    task_id = "demo_task"
    answer = release / "private" / task_id / "answer_key"
    rendered = release / "private" / task_id / "populations" / "primary" / "rendered"
    answer.mkdir(parents=True)
    for directory in (
        rendered / "postgres",
        rendered / "mongodb",
        rendered / "s3" / "clicks",
        rendered / "rest" / "events",
        rendered / "files",
    ):
        directory.mkdir(parents=True)
    (rendered / "postgres" / "users.sql").write_text(
        'CREATE TABLE "users" ("id" BIGINT);\n', encoding="utf-8"
    )
    (rendered / "mongodb" / "logs.jsonl").write_text(
        '{"id":1}\n', encoding="utf-8"
    )
    (rendered / "s3" / "clicks" / "part-00000.jsonl").write_text(
        '{"id":1}\n', encoding="utf-8"
    )
    (rendered / "s3" / "clicks" / "part-00001.jsonl").write_text(
        '{"id":2}\n', encoding="utf-8"
    )
    rest = rendered / "rest" / "events"
    (rest / "index.json").write_text(
        json.dumps({"pages": ["page_0001.json"], "row_count": 1}),
        encoding="utf-8",
    )
    (rest / "page_0001.json").write_text(
        json.dumps({"data": [{"id": 1}]}), encoding="utf-8"
    )
    (rendered / "files" / "order_items.csv").write_text(
        "order_id,quantity\n1,2\n", encoding="utf-8"
    )
    serving = {
        "database": "demo_task",
        "tables": {
            "users": {
                "backend": "postgres",
                "rendered_file": "postgres/users.sql",
            },
            "logs": {
                "backend": "mongodb",
                "collection": "logs",
                "rendered_file": "mongodb/logs.jsonl",
            },
            "clicks": {
                "backend": "s3",
                "bucket": "demo-task-bucket",
                "object_key": "clicks.jsonl",
                "rendered_parts_glob": "part-*.jsonl",
                "rendered_dir": "s3/clicks/",
            },
            "events": {
                "backend": "rest",
                "route": "/demo_task/events",
                "rendered_dir": "rest/events/",
            },
            "order_items": {
                "backend": "files",
                "url": "http://elt-files:8080/demo_task/order_items.csv",
                "rendered_file": "files/order_items.csv",
            },
        },
    }
    (answer / "sources_serving.json").write_text(
        json.dumps(serving), encoding="utf-8"
    )
    manifest = {
        "schema_version": "3.0",
        "corpus_profile": "eltbench_end_to_end",
        "public_layout": "combined",
        "release_id": "release-test",
        "tasks": {task_id: "hash"},
        "splits": {task_id: "train"},
        "families": {task_id: "demo"},
        "licenses": {task_id: "test"},
        "checksums": {},
        "el_sources": {
            task_id: {
                "primary": f"private/{task_id}/populations/primary/rendered"
            }
        },
        "scorer_version": "test",
        "generator_version": "test",
    }
    (release / "release_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return release, task_id


class RuntimeSourceEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_writes_isolated_compose_without_copying_private_data(self) -> None:
        release, task_id = _release(self.root)
        destination = self.root / "environment"
        environment = prepare_source_environment(
            release, task_id, "primary", destination
        )
        self.assertEqual(environment.environment_dir, destination.resolve())
        self.assertEqual(
            sorted(path.name for path in destination.iterdir()),
            ["compose.yaml", "source_server.py"],
        )
        compose = yaml.safe_load(
            (destination / "compose.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(compose["services"]),
            {
                "elt-postgres",
                "elt-mongodb",
                "elt-localstack",
                "elt-api",
                "elt-files",
            },
        )
        for service, image in SOURCE_SERVICE_IMAGES.items():
            self.assertEqual(compose["services"][service]["image"], image)
            self.assertRegex(image, r":.+@sha256:[0-9a-f]{64}$")
        self.assertNotIn(
            ":latest",
            "\n".join(
                service["image"] for service in compose["services"].values()
            ),
        )
        mounts = compose["services"]["elt-api"]["volumes"]
        self.assertTrue(
            any(str(destination / "source_server.py") in mount for mount in mounts)
        )
        self.assertFalse(any(destination.rglob("*.jsonl")))

    def test_seed_executes_every_database_and_s3_artifact(self) -> None:
        release, task_id = _release(self.root)
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )
        runner = FakeRunner()
        environment.seed(runner)
        commands = [" ".join(call[0]) for call in runner.calls]
        stdin_names = [call[2].name for call in runner.calls if call[2] is not None]
        self.assertTrue(any("dropdb" in command for command in commands))
        self.assertTrue(any("createdb" in command for command in commands))
        self.assertTrue(any("mongoimport" in command for command in commands))
        mongo_command = next(command for command in commands if "mongoimport" in command)
        self.assertNotIn(" --file ", f" {mongo_command.split('mongoimport', 1)[1]} ")
        self.assertEqual(sum("awslocal s3 cp" in command for command in commands), 2)
        s3_call = next(
            index
            for index, command in enumerate(commands)
            if "s3://demo-task-bucket/clicks.jsonl" in command
        )
        self.assertIn("s3://demo-task-bucket/clicks.jsonl", commands[s3_call])
        self.assertEqual(runner.stdin_bytes[s3_call], b'{"id":1}\n{"id":2}\n')
        file_compatibility_call = next(
            index
            for index, command in enumerate(commands)
            if "s3://demo-task-bucket/order_items.csv" in command
        )
        self.assertEqual(
            runner.stdin_bytes[file_compatibility_call],
            b"order_id,quantity\n1,2\n",
        )
        self.assertEqual(
            sorted(
                name
                for name in stdin_names
                if not name.startswith((".s3-upload-", ".mongo-import-"))
            ),
            ["order_items.csv", "users.sql"],
        )
        self.assertEqual(
            sum(name.startswith(".s3-upload-") for name in stdin_names), 1
        )
        self.assertEqual(
            sum(name.startswith(".mongo-import-") for name in stdin_names), 1
        )

    def test_seed_inserts_mongo_documents_without_null_fields(self) -> None:
        release, task_id = _release(self.root)
        logs = next(release.rglob("mongodb/logs.jsonl"))
        logs.write_text(
            '{"id":1,"level":"info","score":null,"note":null}\n'
            '{"id":2,"level":null,"score":3,"note":null}\n',
            encoding="utf-8",
        )
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )
        runner = FakeRunner()
        environment.seed(runner)
        index = next(
            index
            for index, call in enumerate(runner.calls)
            if "mongoimport" in " ".join(call[0])
        )
        self.assertEqual(
            runner.stdin_bytes[index].decode("utf-8"),
            '{"id":1,"level":"info","note":null}\n{"id":2,"note":null,"score":3}\n',
        )
        self.assertFalse(any((self.root / "environment").glob(".mongo-import-*")))

    def test_seed_refuses_flat_file_compatibility_key_collision(self) -> None:
        release, task_id = _release(self.root)
        answer = release / "private" / task_id / "answer_key"
        manifest_path = answer / "sources_serving.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = (
            release
            / "private"
            / task_id
            / "populations"
            / "primary"
            / "rendered"
            / "files"
        )
        (source / "order_items.csv").rename(source / "clicks.jsonl")
        manifest["tables"]["order_items"]["rendered_file"] = "files/clicks.jsonl"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )
        runner = FakeRunner()

        with self.assertRaisesRegex(
            SourceEnvironmentError,
            "duplicate generated S3 object key for flat-file compatibility",
        ):
            environment.seed(runner)

        self.assertFalse(
            any("elt-localstack" in call[0] for call in runner.calls),
            "collision must be rejected before the S3 bucket is mutated",
        )

    def test_start_uses_fresh_compose_network_and_connects_airbyte(self) -> None:
        release, task_id = _release(self.root)
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )
        runner = FakeRunner()
        environment.start(runner)
        self.assertEqual(
            runner.calls[0][0],
            (
                "docker",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                "airbyte-abctl-control-plane",
            ),
        )
        self.assertEqual(runner.calls[1][0][-3:], ("up", "--detach", "--wait"))
        self.assertEqual(
            runner.calls[2][0],
            (
                "docker",
                "network",
                "connect",
                environment.network,
                "airbyte-abctl-control-plane",
            ),
        )

    def test_start_interrupt_after_network_attach_cleans_up_partial_stack(self) -> None:
        release, task_id = _release(self.root)
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )
        runner = InterruptingStartRunner()

        with self.assertRaises(KeyboardInterrupt):
            environment.start(runner)

        self.assertEqual(
            [call[0] for call in runner.calls[-2:]],
            [
                (
                    "docker",
                    "network",
                    "disconnect",
                    environment.network,
                    "airbyte-abctl-control-plane",
                ),
                environment._compose("down", "--volumes", "--remove-orphans"),
            ],
        )

    def test_airbyte_readiness_requires_full_sustained_window(self) -> None:
        clock = FakeClock()
        runner = ReadinessRunner([set(), set(), set()])

        wait_for_airbyte_control_plane(
            runner=runner,
            timeout=20,
            stable_for=10,
            poll_interval=5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertEqual(clock.now, 10)
        self.assertEqual(clock.sleeps, [5, 5])
        self.assertEqual(len(runner.calls), 12)

    def test_airbyte_readiness_flap_resets_stability_window(self) -> None:
        clock = FakeClock()
        runner = ReadinessRunner(
            [
                set(),
                set(),
                {"kube-scheduler"},
                set(),
                set(),
                set(),
            ]
        )

        wait_for_airbyte_control_plane(
            runner=runner,
            timeout=30,
            stable_for=10,
            poll_interval=5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        self.assertEqual(clock.now, 25)
        self.assertEqual(clock.sleeps, [5, 5, 5, 5, 5])
        self.assertEqual(len(runner.calls), 24)

    def test_airbyte_readiness_timeout_is_bounded_and_secret_safe(self) -> None:
        clock = FakeClock()
        runner = ReadinessRunner(
            [{"kube-scheduler"}], failure_detail="secret response body"
        )

        with self.assertRaisesRegex(
            SourceEnvironmentError,
            r"did not remain ready for 10 seconds within 12 seconds.*kube-scheduler",
        ) as raised:
            wait_for_airbyte_control_plane(
                runner=runner,
                timeout=12,
                stable_for=10,
                poll_interval=5,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        self.assertNotIn("secret response body", str(raised.exception))
        self.assertEqual(clock.now, 12)
        self.assertEqual(clock.sleeps, [5, 5, 2])

    def test_airbyte_readiness_cannot_succeed_after_global_deadline(self) -> None:
        clock = FakeClock()
        runner = SlowReadinessRunner(
            clock, [set()], seconds_per_probe=2
        )

        with self.assertRaisesRegex(
            SourceEnvironmentError,
            r"did not remain ready for 5 seconds within 10 seconds",
        ):
            wait_for_airbyte_control_plane(
                runner=runner,
                timeout=10,
                stable_for=5,
                poll_interval=2,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        self.assertEqual(clock.now, 10)
        self.assertEqual(len(runner.calls), 4)

    def test_airbyte_readiness_uses_shell_free_in_node_probes(self) -> None:
        clock = FakeClock()
        runner = FakeRunner()

        wait_for_airbyte_control_plane(
            runner=runner,
            timeout=3,
            stable_for=2,
            poll_interval=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        first_sample = [call[0] for call in runner.calls[:4]]
        self.assertEqual(
            [command[-1] for command in first_sample],
            [
                "http://127.0.0.1:80/api/public/v1/health",
                "https://127.0.0.1:6443/readyz",
                "https://127.0.0.1:10259/readyz",
                "https://127.0.0.1:10257/healthz",
            ],
        )
        for command in first_sample:
            self.assertEqual(
                command[:4],
                (
                    "docker",
                    "exec",
                    "airbyte-abctl-control-plane",
                    "curl",
                ),
            )
            self.assertIn("--output", command)
            self.assertIn("/dev/null", command)
            self.assertNotIn("sh", command)

    def test_airbyte_readiness_rejects_impossible_or_invalid_timing(self) -> None:
        runner = ReadinessRunner([set()])
        invalid = (
            {"timeout": 0, "stable_for": 1, "poll_interval": 1},
            {"timeout": 2, "stable_for": 3, "poll_interval": 1},
            {"timeout": 2, "stable_for": 2, "poll_interval": 1},
            {"timeout": 2, "stable_for": 1, "poll_interval": float("inf")},
            {"timeout": 20, "stable_for": 10, "poll_interval": 6},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(SourceEnvironmentError):
                wait_for_airbyte_control_plane(runner=runner, **values)
        self.assertEqual(runner.calls, [])

    def test_invalid_airbyte_container_is_rejected_before_mutation(self) -> None:
        release, task_id = _release(self.root)
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )

        for operation in (environment.start, environment.stop):
            runner = FakeRunner()
            with self.subTest(operation=operation.__name__), self.assertRaisesRegex(
                SourceEnvironmentError,
                "invalid Airbyte control-plane container name",
            ):
                operation(runner, airbyte_container="--privileged")
            self.assertEqual(runner.calls, [])

        runner = ReadinessRunner([set()])
        with self.assertRaisesRegex(
            SourceEnvironmentError,
            "invalid Airbyte control-plane container name",
        ):
            wait_for_airbyte_control_plane(
                runner=runner,
                airbyte_container="--privileged",
            )
        self.assertEqual(runner.calls, [])

    def test_start_refuses_ambiguous_airbyte_source_networks_before_mutation(
        self,
    ) -> None:
        release, task_id = _release(self.root)
        environment = prepare_source_environment(
            release, task_id, "primary", self.root / "environment"
        )
        runner = FakeRunner(
            attached_networks={
                "airbyte-abctl": {},
                "elt-other-primary-deadbeef00-source-network": {},
            }
        )

        with self.assertRaisesRegex(
            SourceEnvironmentError,
            "already attached to a generated source network",
        ):
            environment.start(runner)

        self.assertEqual(len(runner.calls), 1)

    def test_prepare_refuses_reuse_or_unknown_population(self) -> None:
        release, task_id = _release(self.root)
        destination = self.root / "environment"
        destination.mkdir()
        with self.assertRaisesRegex(SourceEnvironmentError, "already exists"):
            prepare_source_environment(release, task_id, "primary", destination)
        with self.assertRaisesRegex(
            SourceEnvironmentError, "no released source population"
        ):
            prepare_source_environment(
                release, task_id, "stress", self.root / "other"
            )


if __name__ == "__main__":
    unittest.main()

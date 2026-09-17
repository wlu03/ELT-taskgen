"""Focused contracts for configured-run validation and evidence readiness."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from elt_taskgen import demo_fixture
from elt_taskgen import pipeline_readiness as readiness_mod
from elt_taskgen.engine import (
    Engine,
    FINAL_REJECTED,
    StageName,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
)
from elt_taskgen.generation import source_data
from elt_taskgen.pipeline_readiness import (
    GenerationRunSpec,
    ReadinessFailureClass,
    ReadinessProfile,
    ReadinessState,
    StageReadiness,
    _failure_descriptor,
    _task_failure,
    _task_disposition,
    make_run_report,
    seal_configured_stage,
    stage_readiness,
    task_readiness,
)
from elt_taskgen.review import providers


def _spec(**updates) -> GenerationRunSpec:
    values = {
        "candidate_count": 7,
        "profile": ReadinessProfile.LOCAL_READY,
        "budget_per_task": 3.0,
    }
    values.update(updates)
    return GenerationRunSpec(**values)


class GenerationRunSpecTests(unittest.TestCase):
    def test_failure_taxonomy_keeps_protocol_adjudication_and_transport_distinct(
        self,
    ) -> None:
        protocol = _failure_descriptor(
            verdict=VERDICT_BLOCKED,
            payload={
                "data": {
                    "failure_class": "protocol_failure",
                    "failure_code": "critic_attack_handoff_invalid",
                }
            },
        )
        adjudication = _failure_descriptor(
            verdict=VERDICT_BLOCKED,
            payload={
                "status": "needs_adjudication",
                "data": {"blocked_on": "human"},
            },
        )
        infrastructure = _failure_descriptor(
            verdict=VERDICT_FAIL,
            payload={"infrastructure": "ProviderFault"},
        )
        quality = _failure_descriptor(verdict=VERDICT_FATAL, payload={})
        stale = _failure_descriptor(
            verdict=VERDICT_PASS,
            payload={},
            stale_code="configured_input_mismatch",
        )

        self.assertEqual(protocol[0], ReadinessFailureClass.PROTOCOL_FAILURE)
        self.assertEqual(protocol[1], "critic_attack_handoff_invalid")
        self.assertEqual(
            adjudication,
            (
                ReadinessFailureClass.PENDING_ADJUDICATION,
                "needs_adjudication",
                "human",
            ),
        )
        self.assertEqual(
            infrastructure[0], ReadinessFailureClass.INFRASTRUCTURE_FAILURE
        )
        self.assertEqual(quality[0], ReadinessFailureClass.QUALITY_REJECTION)
        self.assertEqual(stale[0], ReadinessFailureClass.STALE_EVIDENCE)

    def test_pending_adjudication_outranks_generic_blocking_cause(self) -> None:
        stages = (
            StageReadiness(
                stage=StageName.REVIEW,
                state=ReadinessState.BLOCKED,
                failure_class=ReadinessFailureClass.PENDING_ADJUDICATION,
                failure_code="gold_witness_disagreement",
                blocked_on="human",
            ),
            StageReadiness(
                stage=StageName.ATTACK,
                state=ReadinessState.BLOCKED,
                failure_class=ReadinessFailureClass.OPERATIONAL_BLOCKING,
                failure_code="provider_unavailable",
            ),
        )
        self.assertEqual(
            _task_failure(stages, "in_progress"),
            (
                ReadinessFailureClass.PENDING_ADJUDICATION,
                "gold_witness_disagreement",
                "human",
            ),
        )

    def test_rejection_outranks_stale_prerequisite_passes(self) -> None:
        disposition, blocker = _task_disposition(
            (
                StageReadiness(
                    stage=StageName.INTAKE,
                    state=ReadinessState.STALE,
                    reason="older identity",
                ),
                StageReadiness(
                    stage=StageName.AUTHOR,
                    state=ReadinessState.FAIL,
                    reason="repair budget exhausted",
                ),
            ),
            FINAL_REJECTED,
        )
        self.assertEqual(disposition, "rejected")
        self.assertEqual(blocker, "author: repair budget exhausted")

    def test_defaults_are_typed_stable_and_fingerprinted(self) -> None:
        spec = _spec()
        self.assertEqual(spec.candidate_count, 7)
        self.assertEqual(spec.profile, ReadinessProfile.LOCAL_READY)
        self.assertEqual(
            spec.source_families,
            ("dbt", "dlt", "synsql", "schemapile", "wikidbs"),
        )
        self.assertTrue(spec.resume)
        self.assertEqual(spec.http_retries, 4)
        self.assertEqual(spec.schema_retries, 2)
        self.assertEqual(spec, GenerationRunSpec.model_validate(spec.model_dump()))
        replay = GenerationRunSpec.model_validate(spec.model_dump())
        self.assertEqual(spec.fingerprint(), replay.fingerprint())

    def test_source_allocation_must_be_exact_selected_and_nonnegative(self) -> None:
        exact = _spec(
            source_families=("dbt", "synsql"),
            source_allocation={"dbt": 2, "synsql": 5},
        )
        self.assertEqual(sum(exact.source_allocation.values()), 7)

        invalid = (
            ({"source_families": ()}, "must not be empty"),
            (
                {"source_families": ("dbt", "dbt")},
                "must not contain duplicates",
            ),
            (
                {
                    "source_families": ("dbt", "synsql"),
                    "source_allocation": {"dbt": 1, "synsql": 1},
                },
                "must sum to candidate_count",
            ),
            (
                {
                    "source_families": ("dbt",),
                    "source_allocation": {"dbt": 7, "dlt": 0},
                },
                "unselected families",
            ),
            (
                {
                    "source_families": ("dbt",),
                    "source_allocation": {"dbt": -1},
                },
                "non-negative",
            ),
        )
        for updates, message in invalid:
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(ValidationError, message):
                    _spec(**updates)

    def test_profiles_require_their_operational_inputs(self) -> None:
        for profile in (ReadinessProfile.PACKAGED, ReadinessProfile.RELEASE):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValidationError, "requires export_dir"):
                    _spec(
                        profile=profile,
                        empirical=profile is ReadinessProfile.RELEASE,
                    )
                self.assertEqual(
                    _spec(
                        profile=profile,
                        export_dir="packages",
                        empirical=profile is ReadinessProfile.RELEASE,
                    ).profile,
                    profile,
                )

        with self.assertRaisesRegex(ValidationError, "requires empirical=true"):
            _spec(profile=ReadinessProfile.CALIBRATED)
        self.assertTrue(
            _spec(profile=ReadinessProfile.CALIBRATED, empirical=True).empirical
        )

    def test_retry_concurrency_and_budget_bounds_fail_closed(self) -> None:
        invalid = (
            ({"candidate_count": 0}, "greater than or equal to 1"),
            ({"workers": 0}, "greater than or equal to 1"),
            ({"http_retries": 21}, "less than or equal to 20"),
            ({"schema_retries": 11}, "less than or equal to 10"),
            ({"budget_per_task": 0}, "greater than 0"),
            ({"budget_total": float("inf")}, "finite"),
        )
        for updates, message in invalid:
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(ValidationError, message):
                    _spec(**updates)

    def test_numeric_and_boolean_fields_refuse_bool_or_coercion(self) -> None:
        integer_fields = (
            "candidate_count",
            "seed",
            "workers",
            "max_repair_rounds",
            "repair_attempts",
            "http_retries",
            "schema_retries",
        )
        for field in integer_fields:
            with self.subTest(field=field), self.assertRaisesRegex(
                ValidationError, "must not be booleans"
            ):
                _spec(**{field: True})
        for field in (
            "budget_per_task",
            "budget_total",
            "http_timeout_seconds",
            "http_backoff_seconds",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValidationError, "must not be booleans"
            ):
                _spec(**{field: True})
        for field, value in (("resume", 1), ("resume", "true"), ("empirical", 1)):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                ValidationError, "must be true or false"
            ):
                _spec(**{field: value})
        with self.assertRaisesRegex(ValidationError, "must not be booleans"):
            _spec(
                source_families=("dbt",),
                source_allocation={"dbt": True},
            )

    def test_runtime_evidence_configuration_is_explicit_release_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            store = str(root / "certifications")
            reports = {demo_fixture.DEMO_TASK_ID: str(root / "difficulty.json")}
            configured = _spec(
                profile=ReadinessProfile.RELEASE,
                export_dir=str(root / "release"),
                empirical=True,
                runtime_certification_store=store,
                runtime_difficulty_reports=reports,
            )
            self.assertEqual(configured.runtime_certification_store, store)
            self.assertEqual(configured.runtime_difficulty_reports, reports)

            invalid = (
                (
                    {"runtime_certification_store": store},
                    "must be configured together",
                ),
                (
                    {"runtime_difficulty_reports": reports},
                    "must be configured together",
                ),
                (
                    {
                        "runtime_certification_store": store,
                        "runtime_difficulty_reports": reports,
                    },
                    "requires profile='release'",
                ),
                (
                    {
                        "profile": ReadinessProfile.RELEASE,
                        "export_dir": str(root / "release"),
                        "empirical": True,
                        "runtime_certification_store": "relative/store",
                        "runtime_difficulty_reports": reports,
                    },
                    "absolute path",
                ),
            )
            for updates, message in invalid:
                with self.subTest(updates=updates), self.assertRaisesRegex(
                    ValidationError, message
                ):
                    _spec(**updates)


    def test_session_seat_digest_resolves_against_its_trajectory_record(self):
        """A review evidence row records the digest of the SESSION, which for
        a bounded seat is the trajectory record keyed by the turn-0 prompt —
        equal to turn 0's raw response only when the session took ONE turn.
        Comparing it against the turn entry passed a one-turn seat by
        coincidence and could never pass a seat that took a second turn; run
        11 (2026-09-11) lost 4 of its 5 completed tasks that way, every one
        naming population_adversary."""
        import json as json_mod
        import tempfile
        from pathlib import Path as PathType

        from elt_taskgen import pipeline_readiness as readiness

        with tempfile.TemporaryDirectory() as tmp:
            role_dir = PathType(tmp) / "transcripts" / "population_adversary"
            role_dir.mkdir(parents=True)
            prompt, turn0, session = "a" * 64, "b" * 64, "c" * 64
            # Turn 0 holds its own raw response, not the session's.
            (role_dir / f"{prompt}.json").write_text(
                json_mod.dumps({"prompt_sha256": prompt, "response_sha256": turn0})
            )
            traj = role_dir.parent / "trajectories" / "population_adversary"
            traj.mkdir(parents=True)
            (traj / "deadbeef.json").write_text(
                json_mod.dumps({"prompt_sha256": prompt, "response_sha256": session})
            )
            found = readiness._resolve_recorded_response(
                role_dir, prompt, session, role_name="population_adversary"
            )
            self.assertIsNotNone(found)
            self.assertEqual(found["response_sha256"], session)
            # A digest belonging to neither is still refused.
            self.assertIsNone(
                readiness._resolve_recorded_response(
                    role_dir, prompt, "d" * 64, role_name="population_adversary"
                )
            )
            # And a displaced turn entry is still accepted.
            (role_dir / f"{prompt}.{('e' * 64)[:16]}.json").write_text(
                json_mod.dumps({"prompt_sha256": prompt, "response_sha256": "e" * 64})
            )
            self.assertIsNotNone(
                readiness._resolve_recorded_response(
                    role_dir, prompt, "e" * 64, role_name="population_adversary"
                )
            )

class GenerateReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.engine.register(demo_fixture.demo_task())
        self.task = self.engine.load_task(demo_fixture.DEMO_TASK_ID)
        self.engine.record_report(
            self.task,
            StageName.GENERATE.value,
            VERDICT_PASS,
            StagePayload(detail="generation completed"),
        )

    def _materialize_all(self) -> None:
        root = self.engine.task_dir(self.task.task_id) / "populations"
        for spec in self.task.populations:
            source_data.materialize_population(
                self.task, spec.name, root / spec.name.value
            )

    def _runtime_spec(self, report_path: Path) -> GenerationRunSpec:
        return _spec(
            profile=ReadinessProfile.RELEASE,
            export_dir=str((self.workspace / "release").resolve()),
            empirical=True,
            runtime_certification_store=str(
                (self.workspace / "certifications").resolve()
            ),
            runtime_difficulty_reports={
                self.task.task_id: str(report_path.resolve())
            },
        )

    def _write_current_review_evidence(self) -> None:
        roles = []
        for role_name in (
            "ambiguity_critic",
            "feasibility_reviewer",
            "population_adversary",
            "shortcut_attacker",
        ):
            prompt_sha = hashlib.sha256(
                f"{role_name}:prompt".encode("utf-8")
            ).hexdigest()
            response_sha = hashlib.sha256(b'{"findings":[]}').hexdigest()
            bindings = readiness_mod._current_review_bindings(
                role_name, entry_schema=2, agents_config=None
            )
            role = {
                "task_id": self.task.task_id,
                "task_content_hash": self.task.content_hash(),
                "role": role_name,
                "prompt_sha256": prompt_sha,
                "response_sha256": response_sha,
                "attempt_count": 1,
                "correction_count": 0,
                "entry_schema": 2,
                "finding_count": 0,
                "zero_findings": True,
                **bindings,
            }
            roles.append(role)
            transcript = self.workspace / "transcripts" / role_name / f"{prompt_sha}.json"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                json.dumps(
                    {
                        "response_sha256": response_sha,
                        "route": {"entry_schema": 2, **bindings},
                    }
                ),
                encoding="utf-8",
            )
        manifest = (
            self.engine.task_dir(self.task.task_id)
            / "reports"
            / "review_transcript_manifest.json"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "task_content_hash": self.task.content_hash(),
                    "integrity_problems": [],
                    "roles": roles,
                }
            ),
            encoding="utf-8",
        )
        self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_PASS,
            StagePayload(detail="critic council passed"),
        )

    def test_pass_ledger_without_populations_is_stale_not_ready(self) -> None:
        readiness = stage_readiness(self.engine, self.task, StageName.GENERATE)
        self.assertEqual(readiness.state, ReadinessState.STALE)
        self.assertIn("population", readiness.reason)
        self.assertIn("missing", readiness.reason)

    def test_author_behavior_is_owned_evidence_even_without_configured_seal(self) -> None:
        self.task = self.task.model_copy(
            update={"solver_prompt": "Build the declared customer summary mart."}
        )
        self.engine.register(self.task)
        prose_sha = hashlib.sha256(
            self.task.solver_prompt.encode("utf-8")
        ).hexdigest()
        self.engine.record_report(
            self.task,
            StageName.AUTHOR.value,
            VERDICT_PASS,
            StagePayload(data={"prose_sha256": prose_sha}),
        )
        stale = stage_readiness(self.engine, self.task, StageName.AUTHOR)
        self.assertEqual(stale.state, ReadinessState.STALE)
        self.assertEqual(stale.failure_code, "owned_evidence_mismatch")
        self.assertIn("semantic-author behavior", stale.reason)

    def test_review_prompt_and_protocol_bindings_are_owned_evidence(self) -> None:
        self._write_current_review_evidence()
        current = stage_readiness(self.engine, self.task, StageName.REVIEW)
        self.assertEqual(current.state, ReadinessState.PASS)

        original = providers.role_behavior_sha256

        def changed(role_name: str, *, agents_config=None) -> str:
            if role_name == "ambiguity_critic":
                return "c" * 64
            return original(role_name, agents_config=agents_config)

        with mock.patch.object(
            providers, "role_behavior_sha256", side_effect=changed
        ):
            stale = stage_readiness(self.engine, self.task, StageName.REVIEW)
        self.assertEqual(stale.state, ReadinessState.STALE)
        self.assertEqual(stale.failure_code, "owned_evidence_mismatch")
        self.assertIn("behavior_sha256", stale.reason)

    def test_current_materializations_pass_then_deleted_data_is_stale(self) -> None:
        self._materialize_all()
        readiness = stage_readiness(self.engine, self.task, StageName.GENERATE)
        self.assertEqual(readiness.state, ReadinessState.PASS)
        self.assertEqual(len(readiness.evidence), len(self.task.populations))

        population = self.task.populations[0].name
        rows_dir = (
            self.engine.task_dir(self.task.task_id)
            / "populations"
            / population.value
            / source_data.POPULATION_ROWS_DIR
        )
        next(path for path in sorted(rows_dir.iterdir()) if path.is_file()).unlink()
        readiness = stage_readiness(self.engine, self.task, StageName.GENERATE)
        self.assertEqual(readiness.state, ReadinessState.STALE)
        self.assertIn("file inventory or digest changed", readiness.reason)

    def test_manifest_identity_tamper_is_stale(self) -> None:
        self._materialize_all()
        population = self.task.populations[0].name
        manifest_path = (
            self.engine.task_dir(self.task.task_id)
            / "populations"
            / population.value
            / source_data.POPULATION_MANIFEST_FILENAME
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["task_content_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        readiness = stage_readiness(self.engine, self.task, StageName.GENERATE)
        self.assertEqual(readiness.state, ReadinessState.STALE)
        self.assertIn("task_content_hash is stale", readiness.reason)

    def test_config_sensitive_pass_requires_matching_immutable_seal(self) -> None:
        self.task = self.task.model_copy(
            update={
                "solver_prompt": (
                    "Build the customer summary mart exactly as specified by "
                    "the declared grain and output columns."
                )
            }
        )
        self.engine.register(self.task)
        self.engine.record_report(
            self.task,
            StageName.AUTHOR.value,
            VERDICT_PASS,
            StagePayload(
                data={
                    "prose_sha256": hashlib.sha256(
                        self.task.solver_prompt.encode("utf-8")
                    ).hexdigest(),
                    "behavior_sha256": providers.role_behavior_sha256(
                        "semantic_author"
                    ),
                }
            ),
        )
        spec = _spec(destination="snowflake")
        missing = stage_readiness(
            self.engine, self.task, StageName.AUTHOR, spec=spec
        )
        self.assertEqual(missing.state, ReadinessState.STALE)
        self.assertIn("seal is missing", missing.reason)

        seal_configured_stage(self.engine, self.task, StageName.AUTHOR, spec)
        current = stage_readiness(
            self.engine, self.task, StageName.AUTHOR, spec=spec
        )
        self.assertEqual(current.state, ReadinessState.PASS)

        alternate_agents = self.workspace / "alternate-agents.yaml"
        alternate_agents.write_text(
            providers.default_agents_config_path()
            .read_text(encoding="utf-8")
            .replace("claude-opus-5", "claude-opus-5-alternate", 1),
            encoding="utf-8",
        )
        changed = stage_readiness(
            self.engine,
            self.task,
            StageName.AUTHOR,
            spec=spec.model_copy(update={"agents_config": str(alternate_agents)}),
        )
        self.assertEqual(changed.state, ReadinessState.STALE)
        self.assertIn("seal is missing", changed.reason)

    def test_provider_stage_seal_binds_role_behavior_and_stage_code(self) -> None:
        self.task = self.task.model_copy(
            update={"solver_prompt": "Build the declared customer summary mart."}
        )
        self.engine.register(self.task)
        self.engine.record_report(
            self.task,
            StageName.AUTHOR.value,
            VERDICT_PASS,
            StagePayload(
                data={
                    "prose_sha256": hashlib.sha256(
                        self.task.solver_prompt.encode("utf-8")
                    ).hexdigest(),
                    "behavior_sha256": providers.role_behavior_sha256(
                        "semantic_author"
                    ),
                }
            ),
        )
        spec = _spec()
        seal_path = seal_configured_stage(
            self.engine, self.task, StageName.AUTHOR, spec
        )
        self.assertIsNotNone(seal_path)
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        inputs = seal["inputs"]
        self.assertEqual(
            set(inputs["role_behavior_sha256"]), {"semantic_author"}
        )
        self.assertIn(
            "elt_taskgen.review.prose_fidelity",
            inputs["module_source_sha256"],
        )
        self.assertRegex(inputs["contract_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(inputs["implementation_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            stage_readiness(
                self.engine, self.task, StageName.AUTHOR, spec=spec
            ).state,
            ReadinessState.PASS,
        )

        with mock.patch.object(
            providers, "role_behavior_sha256", return_value="f" * 64
        ):
            role_drift = stage_readiness(
                self.engine, self.task, StageName.AUTHOR, spec=spec
            )
        self.assertEqual(role_drift.state, ReadinessState.STALE)
        self.assertEqual(
            role_drift.failure_class, ReadinessFailureClass.STALE_EVIDENCE
        )
        self.assertEqual(role_drift.failure_code, "owned_evidence_mismatch")

        original_digest = readiness_mod._module_source_sha256

        def drifted_digest(module_name: str) -> str:
            if module_name == "elt_taskgen.review.prose_fidelity":
                return "e" * 64
            return original_digest(module_name)

        with mock.patch.object(
            readiness_mod,
            "_module_source_sha256",
            side_effect=drifted_digest,
        ):
            code_drift = stage_readiness(
                self.engine, self.task, StageName.AUTHOR, spec=spec
            )
        self.assertEqual(code_drift.state, ReadinessState.STALE)
        self.assertEqual(code_drift.failure_code, "configured_input_mismatch")

    def test_review_seal_binds_all_critic_role_behaviors(self) -> None:
        spec = _spec()
        seal = readiness_mod._configured_seal(
            self.task, StageName.REVIEW, spec
        )
        self.assertIsNotNone(seal)
        self.assertEqual(
            set(seal.inputs["role_behavior_sha256"]),
            {
                "ambiguity_critic",
                "population_adversary",
                "shortcut_attacker",
                "feasibility_reviewer",
            },
        )
        self.assertIn(
            "elt_taskgen.review.tools.critic_validators",
            seal.inputs["module_source_sha256"],
        )

        original_behavior = providers.role_behavior_sha256

        def drifted_behavior(
            role_name: str, *, agents_config=None
        ) -> str:
            if role_name == "ambiguity_critic":
                return "d" * 64
            return original_behavior(role_name, agents_config=agents_config)

        with mock.patch.object(
            providers,
            "role_behavior_sha256",
            side_effect=drifted_behavior,
        ):
            drifted = readiness_mod._configured_seal(
                self.task, StageName.REVIEW, spec
            )
        self.assertIsNotNone(drifted)
        self.assertNotEqual(seal.inputs_sha256, drifted.inputs_sha256)

    def test_every_pipeline_stage_has_a_configured_implementation_seal(self) -> None:
        spec = _spec()
        self.assertEqual(
            set(readiness_mod._CONFIGURED_STAGE_MODULES), set(StageName)
        )
        for stage in StageName:
            with self.subTest(stage=stage.value):
                seal = readiness_mod._configured_seal(self.task, stage, spec)
                self.assertIsNotNone(seal)
                self.assertEqual(
                    seal.inputs["contract_version"],
                    readiness_mod.CONFIGURED_STAGE_SEAL_VERSION,
                )
                self.assertRegex(
                    seal.inputs["implementation_sha256"], r"^[0-9a-f]{64}$"
                )
                self.assertTrue(seal.inputs["module_source_sha256"])
                self.assertIn(
                    "elt_taskgen.cli", seal.inputs["module_source_sha256"]
                )

    def test_attack_seal_changes_when_compiler_implementation_changes(self) -> None:
        spec = _spec()
        seal = readiness_mod._configured_seal(
            self.task, StageName.ATTACK, spec
        )
        self.assertIsNotNone(seal)
        original_digest = readiness_mod._module_source_sha256

        def drifted_digest(module_name: str) -> str:
            if module_name == "elt_taskgen.verification.attacks":
                return "c" * 64
            return original_digest(module_name)

        with mock.patch.object(
            readiness_mod,
            "_module_source_sha256",
            side_effect=drifted_digest,
        ):
            drifted = readiness_mod._configured_seal(
                self.task, StageName.ATTACK, spec
            )
        self.assertIsNotNone(drifted)
        self.assertNotEqual(seal.inputs_sha256, drifted.inputs_sha256)

    def test_provider_stage_seal_binds_exact_admission_reference_bytes(self) -> None:
        self.task = self.task.model_copy(
            update={"solver_prompt": "Build the declared customer summary mart."}
        )
        self.engine.register(self.task)
        self.engine.record_report(
            self.task,
            StageName.AUTHOR.value,
            VERDICT_PASS,
            StagePayload(
                data={
                    "prose_sha256": hashlib.sha256(
                        self.task.solver_prompt.encode("utf-8")
                    ).hexdigest(),
                    "behavior_sha256": providers.role_behavior_sha256(
                        "semantic_author"
                    ),
                }
            ),
        )
        admission = (self.workspace / "admission.json").resolve()
        admission.write_text('{"admitted":true}\n', encoding="utf-8")
        spec = _spec(admission_reference=str(admission))
        seal_path = seal_configured_stage(
            self.engine, self.task, StageName.AUTHOR, spec
        )
        self.assertIsNotNone(seal_path)
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        self.assertEqual(
            seal["inputs"]["admission_reference_path"], admission.as_posix()
        )
        self.assertEqual(
            seal["inputs"]["admission_reference_sha256"],
            hashlib.sha256(admission.read_bytes()).hexdigest(),
        )
        unconfigured_path = seal_configured_stage(
            self.engine, self.task, StageName.AUTHOR, _spec()
        )
        self.assertIsNotNone(unconfigured_path)
        unconfigured = json.loads(unconfigured_path.read_text(encoding="utf-8"))
        self.assertEqual(unconfigured["inputs"]["admission_reference_path"], "")
        self.assertEqual(unconfigured["inputs"]["admission_reference_sha256"], "")
        self.assertEqual(
            stage_readiness(
                self.engine, self.task, StageName.AUTHOR, spec=spec
            ).state,
            ReadinessState.PASS,
        )

        admission.write_text('{"admitted":false}\n', encoding="utf-8")
        stale = stage_readiness(
            self.engine, self.task, StageName.AUTHOR, spec=spec
        )
        self.assertEqual(stale.state, ReadinessState.STALE)
        self.assertIn("configured stage seal is missing", stale.reason)

        missing = spec.model_copy(
            update={
                "admission_reference": str(
                    (self.workspace / "removed-admission.json").resolve()
                )
            }
        )
        unreadable = stage_readiness(
            self.engine, self.task, StageName.AUTHOR, spec=missing
        )
        self.assertEqual(unreadable.state, ReadinessState.STALE)
        self.assertIn("admission reference is missing", unreadable.reason)

    def test_configured_runtime_evidence_is_verified_read_only_and_surfaced(
        self,
    ) -> None:
        from elt_taskgen.corpus import certified_difficulty as certified_mod
        from elt_taskgen.runtime import certification_lifecycle as lifecycle_mod

        release = self.workspace / "release"
        release.mkdir()
        difficulty_path = self.workspace / "runtime-difficulty.json"
        difficulty_path.write_text("{}", encoding="utf-8")
        spec = self._runtime_spec(difficulty_path)
        lifecycle = SimpleNamespace(
            certification_id="certification-test",
            lifecycle_digest="1" * 64,
            destination="snowflake",
        )
        expected = SimpleNamespace(
            task_content_hash=self.task.content_hash(),
            runtime_lifecycle_digest=lifecycle.lifecycle_digest,
            evidence_digest="2" * 64,
            empirical_band="hard",
        )
        with (
            mock.patch.object(
                lifecycle_mod,
                "verify_completed_lifecycle",
                return_value=lifecycle,
            ) as verify_lifecycle,
            mock.patch.object(
                certified_mod,
                "build_runtime_certified_difficulty",
                return_value=expected,
            ) as build_difficulty,
            mock.patch.object(
                certified_mod.RuntimeCertifiedDifficulty,
                "model_validate_json",
                return_value=expected,
            ),
            mock.patch.object(
                certified_mod,
                "verify_certified_difficulty",
                return_value=expected,
            ) as verify_difficulty,
            mock.patch.object(lifecycle_mod, "begin_runtime_certification") as begin,
            mock.patch.object(lifecycle_mod, "finish_runtime_certification") as finish,
        ):
            readiness = task_readiness(
                self.engine,
                self.task,
                package_path=release,
                spec=spec,
            )

        self.assertTrue(readiness.runtime_certified)
        runtime = readiness.runtime_evidence
        self.assertTrue(runtime.configured)
        self.assertTrue(runtime.certification_verified)
        self.assertTrue(runtime.difficulty_verified)
        self.assertEqual(runtime.certification_id, lifecycle.certification_id)
        self.assertEqual(runtime.lifecycle_digest, lifecycle.lifecycle_digest)
        self.assertEqual(runtime.difficulty_evidence_digest, expected.evidence_digest)
        self.assertEqual(runtime.empirical_band, "hard")
        self.assertFalse(runtime.certification_reason)
        self.assertFalse(runtime.difficulty_reason)
        verify_lifecycle.assert_called()
        build_difficulty.assert_called_once()
        verify_difficulty.assert_called_once_with(expected)
        begin.assert_not_called()
        finish.assert_not_called()

    def test_missing_or_mismatched_runtime_evidence_never_sets_difficulty_label(
        self,
    ) -> None:
        from elt_taskgen.corpus import certified_difficulty as certified_mod
        from elt_taskgen.runtime import certification_lifecycle as lifecycle_mod

        release = self.workspace / "release"
        release.mkdir()
        difficulty_path = self.workspace / "runtime-difficulty.json"
        spec = self._runtime_spec(difficulty_path)

        with mock.patch.object(
            lifecycle_mod,
            "verify_completed_lifecycle",
            side_effect=lifecycle_mod.CertificationLifecycleError("missing seal"),
        ):
            missing = task_readiness(
                self.engine,
                self.task,
                package_path=release,
                spec=spec,
            )
        self.assertFalse(missing.runtime_certified)
        self.assertFalse(missing.runtime_evidence.difficulty_verified)
        self.assertIn("missing seal", missing.runtime_evidence.certification_reason)

        lifecycle = SimpleNamespace(
            certification_id="certification-test",
            lifecycle_digest="1" * 64,
            destination="snowflake",
        )
        expected = SimpleNamespace(
            task_content_hash=self.task.content_hash(),
            runtime_lifecycle_digest=lifecycle.lifecycle_digest,
            evidence_digest="2" * 64,
            empirical_band="hard",
        )
        with (
            mock.patch.object(
                lifecycle_mod,
                "verify_completed_lifecycle",
                return_value=lifecycle,
            ),
            mock.patch.object(
                certified_mod,
                "build_runtime_certified_difficulty",
                return_value=expected,
            ),
        ):
            absent = task_readiness(
                self.engine,
                self.task,
                package_path=release,
                spec=spec,
            )
        self.assertTrue(absent.runtime_certified)
        self.assertFalse(absent.runtime_evidence.difficulty_verified)
        self.assertIn("missing", absent.runtime_evidence.difficulty_reason)

        difficulty_path.write_text("{}", encoding="utf-8")
        mismatched = SimpleNamespace(
            task_content_hash=self.task.content_hash(),
            runtime_lifecycle_digest=lifecycle.lifecycle_digest,
            evidence_digest="3" * 64,
            empirical_band="easy",
        )
        with (
            mock.patch.object(
                lifecycle_mod,
                "verify_completed_lifecycle",
                return_value=lifecycle,
            ),
            mock.patch.object(
                certified_mod,
                "build_runtime_certified_difficulty",
                return_value=expected,
            ),
            mock.patch.object(
                certified_mod.RuntimeCertifiedDifficulty,
                "model_validate_json",
                return_value=mismatched,
            ),
            mock.patch.object(
                certified_mod,
                "verify_certified_difficulty",
                return_value=mismatched,
            ),
        ):
            changed = task_readiness(
                self.engine,
                self.task,
                package_path=release,
                spec=spec,
            )
        self.assertTrue(changed.runtime_certified)
        self.assertFalse(changed.runtime_evidence.difficulty_verified)
        self.assertIn("does not match", changed.runtime_evidence.difficulty_reason)

    def test_unconfigured_local_readiness_never_touches_runtime_apis(self) -> None:
        from elt_taskgen.corpus import certified_difficulty as certified_mod
        from elt_taskgen.runtime import certification_lifecycle as lifecycle_mod

        with mock.patch.object(
            lifecycle_mod, "verify_completed_lifecycle"
        ) as verify_lifecycle, mock.patch.object(
            certified_mod, "build_runtime_certified_difficulty"
        ) as build_difficulty:
            readiness = task_readiness(
                self.engine,
                self.task,
                package_path=self.workspace / "local-package",
                spec=_spec(),
            )
        self.assertFalse(readiness.runtime_evidence.configured)
        self.assertFalse(readiness.runtime_certified)
        verify_lifecycle.assert_not_called()
        build_difficulty.assert_not_called()

    def test_configured_runtime_failure_is_a_report_blocker(self) -> None:
        from elt_taskgen.runtime import certification_lifecycle as lifecycle_mod

        release = self.workspace / "release"
        release.mkdir()
        difficulty_path = self.workspace / "runtime-difficulty.json"
        spec = self._runtime_spec(difficulty_path)
        with mock.patch.object(
            lifecycle_mod,
            "verify_completed_lifecycle",
            side_effect=lifecycle_mod.CertificationLifecycleError("not complete"),
        ):
            report = make_run_report(
                engine=self.engine,
                run_id="runtime-report-test",
                spec=spec,
                task_ids=(self.task.task_id,),
                state="COMPLETE",
                package_paths={self.task.task_id: release},
            )
        self.assertEqual(report.state, "COMPLETE_WITH_ISSUES")
        self.assertEqual(report.counts.runtime_certified, 0)
        self.assertEqual(report.counts.runtime_difficulty_certified, 0)
        self.assertTrue(any("not complete" in blocker for blocker in report.blockers))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DestinationRosterTests(unittest.TestCase):
    """`destinations` defaults to all three warehouses; the first is the root."""

    def test_default_is_all_three_with_snowflake_root(self) -> None:
        spec = _spec()
        self.assertEqual(spec.destinations, ("snowflake", "databricks", "redshift"))
        self.assertEqual(spec.destination, "snowflake")
        self.assertEqual(spec.extra_destinations, ("databricks", "redshift"))
        bare = GenerationRunSpec(candidate_count=1, profile="draft")
        self.assertEqual(bare.max_repair_rounds, 3)
        self.assertEqual(bare.budget_per_task, 25.0)

    def test_roster_accepts_all_comma_list_and_sequence(self) -> None:
        self.assertEqual(_spec(destinations="all").destinations, ("snowflake", "databricks", "redshift"))
        listed = _spec(destinations="databricks, snowflake")
        self.assertEqual(listed.destinations, ("databricks", "snowflake"))
        self.assertEqual(listed.destination, "databricks")
        self.assertEqual(listed.extra_destinations, ("snowflake",))
        single = _spec(destinations=["redshift"])
        self.assertEqual((single.destination, single.extra_destinations), ("redshift", ()))

    def test_document_naming_only_destination_packages_that_one(self) -> None:
        spec = _spec(destination="databricks")
        self.assertEqual(spec.destinations, ("databricks",))
        self.assertEqual(spec.extra_destinations, ())

    def test_roster_refuses_unknown_duplicate_empty_and_mismatched_root(self) -> None:
        for updates, message in (
            ({"destinations": "bigquery"}, "Input should be"),
            ({"destinations": "snowflake,snowflake"}, "duplicates"),
            ({"destinations": []}, "at least one"),
            ({"destinations": ["snowflake", "redshift"], "destination": "redshift"}, "first entry"),
        ):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValidationError, message):
                _spec(**updates)

    def test_gates_stage_inputs_name_the_extra_destinations(self) -> None:
        stage = readiness_mod.StageName.GATES_TRANSFORM
        self.assertEqual(
            readiness_mod._configured_inputs(_spec(), stage)["extra_destinations"],
            ["databricks", "redshift"],
        )
        single = readiness_mod._configured_inputs(_spec(destinations="snowflake"), stage)
        self.assertNotIn("extra_destinations", single)
        self.assertEqual(single["destination"], "snowflake")

"""Contract tests for DuckDB semantic-proxy versus real-warehouse evidence."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from elt_taskgen import runtime_matrix
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.destinations import Destination
from elt_taskgen.models import (
    ColumnSpec,
    ColumnType,
    TaskIR,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.verification.canonical_fingerprint import (
    CANONICAL_FINGERPRINT_VERSION,
    CanonicalRowOrder,
    canonical_relation_fingerprint,
)
from elt_taskgen.verification.parity import (
    PARITY_CASE_SPECS,
    PARITY_CONTRACT_VERSION,
    PARITY_OBSERVATION_SCHEMA_VERSION,
    PARITY_REGISTRY_DIGEST,
    ParityComparison,
    ParityEngine,
    ParityEvidenceKind,
    ParityEvidenceOrigin,
    ParityError,
    ParityObservation,
    ParityVerdict,
    parity_case_specs,
    parity_case_set_digest,
    parity_matrix_digest,
    parity_report_digest,
    parity_registry_digest,
    relation_observation,
    verify_parity,
)
from elt_taskgen.verification.parity_scope import (
    PARITY_SCOPE_POLICY_VERSION,
    derive_task_parity_manifest,
)
from tests.semantic_gate_fixture import gate_task


def _all_case_task() -> TaskIR:
    """Five-backend fixture extended to exercise every logical type."""

    task = gate_task()
    tables = []
    for table in task.tables:
        if table.name == "events":
            table = table.model_copy(
                update={
                    "columns": table.columns
                    + (
                        ColumnSpec(
                            name="ratio",
                            type=ColumnType.FLOAT,
                            nullable=True,
                            description="Finite floating-point parity probe.",
                        ),
                        ColumnSpec(
                            name="enabled",
                            type=ColumnType.BOOLEAN,
                            nullable=True,
                            description="Boolean parity probe.",
                        ),
                        ColumnSpec(
                            name="event_date",
                            type=ColumnType.DATE,
                            nullable=True,
                            description="Calendar-date parity probe.",
                        ),
                        ColumnSpec(
                            name="payload",
                            type=ColumnType.JSON,
                            nullable=True,
                            description="Nested JSON parity probe.",
                        ),
                    )
                }
            )
        tables.append(table)
    return TaskIR.model_validate(
        task.model_copy(update={"tables": tuple(tables)}).model_dump(mode="python")
    )


class ParityVerifierTests(unittest.TestCase):
    task = _all_case_task()
    semantic_release_id = "semantic-release-test"
    task_id = task.task_id
    runtime_bundle_id = "runtime-bundle-test"
    attempt_id = "attempt-test"
    solution_digest = "d" * 64
    probe_id = "reference"
    probe_binding_digest = "f" * 64
    population = "primary"
    population_digest = "7" * 64

    @staticmethod
    def matrix(destination: Destination | str) -> dict[str, str]:
        return runtime_matrix.certification_matrix(destination)

    @classmethod
    def required_case_ids(
        cls,
        destination: Destination,
        task: TaskIR | None = None,
    ) -> tuple[str, ...]:
        return derive_task_parity_manifest(
            task or cls.task, destination
        ).case_ids

    @classmethod
    def required_case_digest(
        cls,
        destination: Destination,
        task: TaskIR | None = None,
    ) -> str:
        return derive_task_parity_manifest(
            task or cls.task, destination
        ).case_set_digest

    def observation(
        self,
        *,
        destination: Destination,
        case_id: str,
        engine: ParityEngine,
        succeeded: bool = True,
        result_digest: str | None = None,
        schema_digest: str = "b" * 64,
        row_count: int | None = 3,
        matrix: dict[str, str] | None = None,
        semantic_release_id: str | None = None,
        task_id: str | None = None,
        runtime_bundle_id: str | None = None,
        attempt_id: str | None = None,
        solution_digest: str | None = None,
        probe_id: str | None = None,
        probe_binding_digest: str | None = None,
        population: str | None = None,
        population_digest: str | None = None,
        evidence_kind: ParityEvidenceKind | None = None,
        task: TaskIR | None = None,
        task_content_hash: str | None = None,
        required_case_manifest_digest: str | None = None,
        required_case_set_digest: str | None = None,
    ) -> ParityObservation:
        selected_task = task or self.task
        manifest = derive_task_parity_manifest(selected_task, destination)
        if result_digest is None:
            result_digest = sha256_hex(
                canonical_json(
                    {
                        "case_id": case_id,
                        "destination": destination.value,
                        "outcome": "canonical-match",
                    }
                )
            )
        selected_matrix = matrix or self.matrix(destination)
        spec = next(
            spec
            for spec in parity_case_specs(destination)
            if spec.case_id == case_id
        )
        if evidence_kind is None:
            evidence_kind = spec.evidence_kind
        row_order = (
            spec.required_row_order
            if evidence_kind is ParityEvidenceKind.RELATION_FINGERPRINT
            else None
        )
        return ParityObservation(
            case_id=case_id,
            destination=destination,
            engine=engine,
            evidence_origin={
                ParityEngine.DUCKDB_SEMANTIC_PROXY: (
                    ParityEvidenceOrigin.LOCAL_EXECUTION
                ),
                ParityEngine.LOCAL_STATIC_CONTRACT: (
                    ParityEvidenceOrigin.LOCAL_STATIC_INSPECTION
                ),
                ParityEngine.LOCAL_PROTOCOL_MODEL: (
                    ParityEvidenceOrigin.LOCAL_PROTOCOL_EXECUTION
                ),
                ParityEngine.REAL_WAREHOUSE: ParityEvidenceOrigin.LIVE_CLOUD,
            }[engine],
            evidence_kind=evidence_kind,
            row_order=row_order,
            semantic_release_id=(
                semantic_release_id or self.semantic_release_id
            ),
            task_id=task_id or selected_task.task_id,
            task_content_hash=(
                task_content_hash or selected_task.content_hash()
            ),
            required_case_manifest_digest=(
                required_case_manifest_digest or manifest.manifest_digest
            ),
            runtime_bundle_id=runtime_bundle_id or self.runtime_bundle_id,
            attempt_id=attempt_id or self.attempt_id,
            solution_digest=solution_digest or self.solution_digest,
            probe_id=probe_id or self.probe_id,
            probe_binding_digest=(
                probe_binding_digest or self.probe_binding_digest
            ),
            matrix_digest=parity_matrix_digest(selected_matrix),
            required_case_set_digest=(
                required_case_set_digest
                or manifest.case_set_digest
            ),
            population=population or self.population,
            population_digest=population_digest or self.population_digest,
            producer_version="test-collector-1",
            succeeded=succeeded,
            result_digest=result_digest,
            schema_digest=schema_digest,
            row_count=row_count,
            failure_code="" if succeeded else "stable_test_failure",
        )

    def complete_observations(
        self,
        destination: Destination,
        *,
        task: TaskIR | None = None,
    ) -> list[ParityObservation]:
        selected_task = task or self.task
        observations: list[ParityObservation] = []
        for spec in parity_case_specs(
            destination,
            case_ids=self.required_case_ids(destination, selected_task),
        ):
            if spec.comparison is ParityComparison.EXACT:
                observations.extend(
                    (
                        self.observation(
                            destination=destination,
                            case_id=spec.case_id,
                            engine=spec.reference_engine,
                            task=selected_task,
                        ),
                        self.observation(
                            destination=destination,
                            case_id=spec.case_id,
                            engine=ParityEngine.REAL_WAREHOUSE,
                            task=selected_task,
                        ),
                    )
                )
            else:
                observations.append(
                    self.observation(
                        destination=destination,
                        case_id=spec.case_id,
                        engine=ParityEngine.REAL_WAREHOUSE,
                        task=selected_task,
                        schema_digest="",
                        row_count=None,
                    )
                )
        return observations

    def verify(
        self,
        destination: Destination,
        observations: list[ParityObservation],
        *,
        case_ids: list[str] | None = None,
        matrix: dict[str, str] | None = None,
        expected_matrix: dict[str, str] | None = None,
        task: TaskIR | None = None,
        expected_task_content_hash: str | None = None,
    ):
        selected_task = task or self.task
        selected_matrix = matrix or self.matrix(destination)
        return verify_parity(
            destination=destination,
            semantic_release_id=self.semantic_release_id,
            task=selected_task,
            expected_task_content_hash=(
                expected_task_content_hash or selected_task.content_hash()
            ),
            runtime_bundle_id=self.runtime_bundle_id,
            attempt_id=self.attempt_id,
            solution_digest=self.solution_digest,
            probe_id=self.probe_id,
            probe_binding_digest=self.probe_binding_digest,
            matrix=selected_matrix,
            expected_matrix=expected_matrix or selected_matrix,
            population=self.population,
            population_digest=self.population_digest,
            observations=observations,
            case_ids=case_ids,
        )

    def test_registry_is_unique_versioned_and_covers_declared_surface(self) -> None:
        ids = [spec.case_id for spec in PARITY_CASE_SPECS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(PARITY_CONTRACT_VERSION, "1")
        self.assertEqual(PARITY_REGISTRY_DIGEST, parity_registry_digest())
        self.assertEqual(len(PARITY_REGISTRY_DIGEST), 64)

        shared = {
            "el.source.files.records",
            "el.source.postgres.records",
            "el.source.mongodb.records",
            "el.source.rest.pagination",
            "el.source.s3.multipart",
            "el.stream_selection",
            "el.full_refresh_append",
            "el.raw_table_counts",
            "warehouse.namespace_mapping",
            "warehouse.identifier_projection",
            "warehouse.integer_width",
            "warehouse.float_finite_exact",
            "warehouse.boolean_mapping",
            "warehouse.date_boundary",
            "warehouse.text_unicode_whitespace",
            "warehouse.empty_string_vs_null",
            "warehouse.sql_null",
            "warehouse.decimal_38_9",
            "warehouse.timestamp_utc_microsecond",
            "warehouse.json_scalar",
            "warehouse.null_ordering",
            "warehouse.metadata_projection",
            "t.dbt_profile_namespace",
            "t.mart_schema",
            "t.mart_rows",
            "runtime.connector_discovery",
            "runtime.authentication",
            "runtime.network_and_job_lifecycle",
        }
        target_specific = {
            Destination.SNOWFLAKE: {
                "snowflake.variant_object_array",
                "snowflake.timestamp_types",
                "snowflake.warehouse_lifecycle",
            },
            Destination.DATABRICKS: {
                "databricks.json_string_projection",
                "databricks.delta_table_shape",
                "databricks.unity_catalog_volume_permissions",
            },
            Destination.REDSHIFT: {
                "redshift.super_projection",
                "redshift.identifier_sanitization",
                "redshift.s3_copy_cleanup",
                "redshift.large_text_boundary",
                "redshift.large_text_roundtrip",
            },
        }
        for destination in Destination:
            with self.subTest(destination=destination.value):
                actual = {
                    spec.case_id for spec in parity_case_specs(destination)
                }
                self.assertTrue(shared <= actual)
                self.assertEqual(actual - shared, target_specific[destination])
                self.assertTrue(
                    any(
                        spec.comparison is ParityComparison.EXACT
                        for spec in parity_case_specs(destination)
                    )
                )
                self.assertTrue(
                    any(
                        spec.comparison is ParityComparison.REAL_ONLY
                        for spec in parity_case_specs(destination)
                    )
                )
                for spec in parity_case_specs(destination):
                    if spec.comparison is ParityComparison.REAL_ONLY:
                        self.assertIsNone(spec.reference_engine)
                    elif spec.evidence_kind is ParityEvidenceKind.RELATION_FINGERPRINT:
                        self.assertIs(
                            spec.reference_engine,
                            ParityEngine.DUCKDB_SEMANTIC_PROXY,
                        )
                    elif spec.evidence_kind is ParityEvidenceKind.PROTOCOL_ATTESTATION:
                        self.assertIs(
                            spec.reference_engine,
                            ParityEngine.LOCAL_PROTOCOL_MODEL,
                        )
                    else:
                        self.assertIs(
                            spec.reference_engine,
                            ParityEngine.LOCAL_STATIC_CONTRACT,
                        )

    def test_all_case_fixture_derives_every_declared_destination_case(self) -> None:
        for destination in Destination:
            with self.subTest(destination=destination.value):
                declared = tuple(
                    spec.case_id for spec in parity_case_specs(destination)
                )
                derived = derive_task_parity_manifest(
                    self.task, destination
                ).case_ids
                self.assertEqual(derived, declared)

    def test_certification_matrix_binds_the_parity_contract_version(self) -> None:
        from elt_taskgen.runtime.source_images import SOURCE_SERVICE_IMAGES

        for destination in Destination:
            with self.subTest(destination=destination.value):
                matrix = runtime_matrix.certification_matrix(destination)
                self.assertEqual(
                    matrix["parity_contract_version"], PARITY_CONTRACT_VERSION
                )
                self.assertEqual(
                    matrix["canonical_fingerprint_version"],
                    CANONICAL_FINGERPRINT_VERSION,
                )
                self.assertEqual(
                    matrix["parity_registry_digest"],
                    PARITY_REGISTRY_DIGEST,
                )
                self.assertEqual(
                    matrix["parity_observation_schema_version"],
                    PARITY_OBSERVATION_SCHEMA_VERSION,
                )
                self.assertEqual(
                    matrix["parity_scope_policy_version"],
                    PARITY_SCOPE_POLICY_VERSION,
                )
                self.assertEqual(
                    matrix["matrix_version"],
                    runtime_matrix.CERTIFICATION_MATRIX_VERSION,
                )
                for service, image in SOURCE_SERVICE_IMAGES.items():
                    self.assertEqual(
                        matrix[f"source_service_image:{service}"], image
                    )

    def test_complete_matching_suite_verifies_all_three_destinations(self) -> None:
        for destination in Destination:
            with self.subTest(destination=destination.value):
                observations = self.complete_observations(destination)
                report = self.verify(destination, observations)
                self.assertTrue(report.reference_complete)
                self.assertTrue(report.real_complete)
                self.assertTrue(report.reference_real_verified)
                self.assertTrue(report.real_only_verified)
                self.assertTrue(report.live_evidence_complete)
                self.assertTrue(report.selected_scope_verified)
                self.assertTrue(report.required_scope_coverage)
                self.assertTrue(report.fully_verified)
                self.assertTrue(
                    all(
                        case.verdict
                        in {
                            ParityVerdict.MATCHED,
                            ParityVerdict.REAL_ONLY_VERIFIED,
                        }
                        for case in report.cases
                    )
                )

    def test_complete_task_required_scope_can_verify_without_global_registry(self) -> None:
        destination = Destination.SNOWFLAKE
        task = demo_task()
        required = derive_task_parity_manifest(task, destination)
        self.assertLess(
            len(required.case_ids),
            len(parity_case_specs(destination)),
        )
        observations = self.complete_observations(destination, task=task)
        report = self.verify(
            destination,
            observations,
            task=task,
        )
        self.assertEqual(report.required_case_ids, required.case_ids)
        self.assertEqual(report.task_content_hash, task.content_hash())
        self.assertEqual(
            report.required_case_manifest_digest,
            required.manifest_digest,
        )
        self.assertTrue(report.required_scope_coverage)
        self.assertTrue(report.fully_verified)
        self.assertFalse(report.certification_eligible)

    def test_frozen_task_hash_rejects_a_narrower_replacement_task(self) -> None:
        destination = Destination.SNOWFLAKE
        narrower_task = demo_task().model_copy(
            update={"task_id": self.task.task_id}
        )
        with self.assertRaisesRegex(ParityError, "frozen expected task"):
            self.verify(
                destination,
                [],
                task=narrower_task,
                expected_task_content_hash=self.task.content_hash(),
            )

    def test_report_and_digest_are_deterministic_across_observation_order(self) -> None:
        destination = Destination.DATABRICKS
        observations = self.complete_observations(destination)
        first = self.verify(destination, observations)
        second = self.verify(destination, list(reversed(observations)))
        self.assertEqual(first, second)
        self.assertEqual(parity_report_digest(first), parity_report_digest(second))

    def test_missing_real_evidence_is_local_only_not_parity(self) -> None:
        destination = Destination.SNOWFLAKE
        case_id = "el.raw_table_counts"
        report = self.verify(
            destination,
            [
                self.observation(
                    destination=destination,
                    case_id=case_id,
                    engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
                )
            ],
            case_ids=[case_id],
        )
        self.assertTrue(report.reference_complete)
        self.assertFalse(report.real_complete)
        self.assertFalse(report.reference_real_verified)
        self.assertFalse(report.live_evidence_complete)
        self.assertFalse(report.selected_scope_verified)
        self.assertFalse(report.required_scope_coverage)
        self.assertFalse(report.fully_verified)
        self.assertIs(report.cases[0].verdict, ParityVerdict.REFERENCE_ONLY)

    def test_missing_semantic_and_both_missing_are_reported_separately(self) -> None:
        destination = Destination.REDSHIFT
        cases = ["el.raw_table_counts", "t.mart_rows"]
        report = self.verify(
            destination,
            [
                self.observation(
                    destination=destination,
                    case_id=cases[0],
                    engine=ParityEngine.REAL_WAREHOUSE,
                )
            ],
            case_ids=cases,
        )
        verdicts = {case.case_id: case.verdict for case in report.cases}
        self.assertIs(verdicts[cases[0]], ParityVerdict.MISSING_REFERENCE)
        self.assertIs(verdicts[cases[1]], ParityVerdict.MISSING_BOTH)
        self.assertFalse(report.fully_verified)

    def test_result_schema_and_count_differences_break_semantic_runtime_parity(self) -> None:
        destination = Destination.DATABRICKS
        case_id = "t.mart_rows"
        changes = {
            "result_digest": {"result_digest": "c" * 64},
            "schema_digest": {"schema_digest": "d" * 64},
            "row_count": {"row_count": 4},
        }
        for differing_field, updates in changes.items():
            with self.subTest(differing_field=differing_field):
                semantic = self.observation(
                    destination=destination,
                    case_id=case_id,
                    engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
                )
                real = self.observation(
                    destination=destination,
                    case_id=case_id,
                    engine=ParityEngine.REAL_WAREHOUSE,
                    **updates,
                )
                report = self.verify(
                    destination,
                    [semantic, real],
                    case_ids=[case_id],
                )
                self.assertIs(report.cases[0].verdict, ParityVerdict.MISMATCH)
                self.assertIn(differing_field, report.cases[0].reason)
                self.assertFalse(report.fully_verified)

    def test_successful_exact_evidence_requires_schema_and_row_count(self) -> None:
        destination = Destination.SNOWFLAKE
        case_id = "el.raw_table_counts"
        valid = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
        )
        for updates in ({"schema_digest": ""}, {"row_count": None}):
            with self.subTest(updates=updates):
                incomplete = valid.model_copy(update=updates)
                with self.assertRaisesRegex(ParityError, "schema and row-count"):
                    self.verify(
                        destination,
                        [incomplete],
                        case_ids=[case_id],
                    )

    def test_failed_semantic_real_and_both_fail_closed(self) -> None:
        destination = Destination.SNOWFLAKE
        case_id = "warehouse.decimal_38_9"
        for semantic_ok, real_ok, expected in (
            (False, True, ParityVerdict.REFERENCE_FAILED),
            (True, False, ParityVerdict.REAL_FAILED),
            (False, False, ParityVerdict.BOTH_FAILED),
        ):
            with self.subTest(semantic_ok=semantic_ok, real_ok=real_ok):
                report = self.verify(
                    destination,
                    [
                        self.observation(
                            destination=destination,
                            case_id=case_id,
                            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
                            succeeded=semantic_ok,
                        ),
                        self.observation(
                            destination=destination,
                            case_id=case_id,
                            engine=ParityEngine.REAL_WAREHOUSE,
                            succeeded=real_ok,
                        ),
                    ],
                    case_ids=[case_id],
                )
                self.assertIs(report.cases[0].verdict, expected)
                self.assertFalse(report.fully_verified)

    def test_real_only_case_requires_real_and_rejects_semantic_claims(self) -> None:
        destination = Destination.REDSHIFT
        case_id = "redshift.s3_copy_cleanup"
        missing = self.verify(destination, [], case_ids=[case_id])
        self.assertIs(missing.cases[0].verdict, ParityVerdict.MISSING_REAL)
        self.assertFalse(missing.fully_verified)

        real = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.REAL_WAREHOUSE,
            schema_digest="",
            row_count=None,
        )
        verified = self.verify(destination, [real], case_ids=[case_id])
        self.assertTrue(verified.real_only_verified)
        self.assertTrue(verified.selected_scope_verified)
        self.assertFalse(verified.required_scope_coverage)
        self.assertFalse(verified.fully_verified)

        semantic = real.model_copy(
            update={
                "engine": ParityEngine.DUCKDB_SEMANTIC_PROXY,
                "evidence_origin": ParityEvidenceOrigin.LOCAL_EXECUTION,
            }
        )
        with self.assertRaisesRegex(ParityError, "real-only"):
            self.verify(destination, [semantic, real], case_ids=[case_id])

        replay = real.model_copy(
            update={
                "evidence_origin": ParityEvidenceOrigin.RECORDED_LIVE_REPLAY
            }
        )
        replay_report = self.verify(destination, [replay], case_ids=[case_id])
        self.assertIs(
            replay_report.cases[0].verdict,
            ParityVerdict.REAL_ONLY_REPLAYED,
        )
        self.assertFalse(replay_report.real_only_verified)
        self.assertFalse(replay_report.selected_scope_verified)

    def test_evidence_is_bound_to_every_execution_identity(self) -> None:
        destination = Destination.SNOWFLAKE
        case_id = "el.raw_table_counts"
        valid = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
        )
        invalid_cases = {
            "destination": valid.model_copy(
                update={"destination": Destination.DATABRICKS}
            ),
            "semantic release": valid.model_copy(
                update={"semantic_release_id": "another-semantic-release"}
            ),
            "task": valid.model_copy(update={"task_id": "another-task"}),
            "task content": valid.model_copy(
                update={"task_content_hash": "1" * 64}
            ),
            "required case manifest": valid.model_copy(
                update={"required_case_manifest_digest": "2" * 64}
            ),
            "runtime bundle": valid.model_copy(
                update={"runtime_bundle_id": "another-bundle"}
            ),
            "attempt": valid.model_copy(update={"attempt_id": "another-attempt"}),
            "solution": valid.model_copy(
                update={"solution_digest": "6" * 64}
            ),
            "another probe$": valid.model_copy(
                update={"probe_id": "another-probe"}
            ),
            "probe implementation": valid.model_copy(
                update={"probe_binding_digest": "9" * 64}
            ),
            "execution matrix": valid.model_copy(
                update={"matrix_digest": "e" * 64}
            ),
            "population": valid.model_copy(update={"population": "stress"}),
            "population content": valid.model_copy(
                update={"population_digest": "5" * 64}
            ),
        }
        for label, observation in invalid_cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ParityError, label):
                    self.verify(
                        destination,
                        [observation],
                        case_ids=[case_id],
                    )

    def test_duplicate_unknown_and_wrong_matrix_destination_are_refused(self) -> None:
        destination = Destination.DATABRICKS
        case_id = "t.mart_rows"
        observation = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
        )
        with self.assertRaisesRegex(ParityError, "duplicate"):
            self.verify(
                destination,
                [observation, observation],
                case_ids=[case_id],
            )
        with self.assertRaisesRegex(ParityError, "not declared"):
            parity_case_specs(destination, case_ids=["snowflake.timestamp_types"])
        with self.assertRaisesRegex(ParityError, "destination"):
            self.verify(
                destination,
                [],
                case_ids=[case_id],
                matrix=self.matrix(Destination.SNOWFLAKE),
            )

    def test_abbreviated_and_unpinned_execution_matrices_are_refused(self) -> None:
        destination = Destination.SNOWFLAKE
        case_id = "el.raw_table_counts"
        with self.assertRaisesRegex(ParityError, "missing keys"):
            self.verify(
                destination,
                [],
                case_ids=[case_id],
                matrix={"destination": destination.value},
            )
        unpinned = self.matrix(destination)
        unpinned["airbyte_abctl"] = "unpinned-legacy"
        with self.assertRaisesRegex(ParityError, "unpinned values"):
            self.verify(
                destination,
                [],
                case_ids=[case_id],
                matrix=unpinned,
            )
        observed = self.matrix(destination)
        observed["destination_connector"] = "fabricated-pin"
        with self.assertRaisesRegex(ParityError, "frozen expected matrix"):
            self.verify(
                destination,
                [],
                case_ids=[case_id],
                matrix=observed,
                expected_matrix=self.matrix(destination),
            )

    def test_matrix_change_invalidates_previously_collected_observations(self) -> None:
        destination = Destination.REDSHIFT
        case_id = "el.raw_table_counts"
        original = self.matrix(destination)
        observation = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
            matrix=original,
        )
        changed = dict(original)
        changed["destination_connector"] = "new-pin"
        self.assertNotEqual(
            parity_matrix_digest(original), parity_matrix_digest(changed)
        )
        with self.assertRaisesRegex(ParityError, "execution matrix"):
            self.verify(
                destination,
                [observation],
                case_ids=[case_id],
                matrix=changed,
            )

    def test_observation_schema_rejects_raw_failure_messages_and_bad_digests(self) -> None:
        manifest = derive_task_parity_manifest(
            self.task, Destination.SNOWFLAKE
        )
        base = dict(
            case_id="el.raw_table_counts",
            destination=Destination.SNOWFLAKE,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
            evidence_origin=ParityEvidenceOrigin.LOCAL_EXECUTION,
            evidence_kind=ParityEvidenceKind.RELATION_FINGERPRINT,
            row_order=CanonicalRowOrder.UNORDERED,
            semantic_release_id=self.semantic_release_id,
            task_id=self.task_id,
            task_content_hash=self.task.content_hash(),
            required_case_manifest_digest=manifest.manifest_digest,
            runtime_bundle_id=self.runtime_bundle_id,
            attempt_id=self.attempt_id,
            solution_digest=self.solution_digest,
            probe_id=self.probe_id,
            probe_binding_digest=self.probe_binding_digest,
            matrix_digest="a" * 64,
            required_case_set_digest=manifest.case_set_digest,
            population=self.population,
            population_digest=self.population_digest,
            producer_version="test-collector-1",
            succeeded=False,
            result_digest="b" * 64,
            failure_code="connection failed with secret text",
        )
        with self.assertRaises(ValidationError):
            ParityObservation(**base)
        with self.assertRaises(ValidationError):
            ParityObservation(**(base | {"failure_code": "stable", "result_digest": "x"}))
        with self.assertRaises(ValidationError):
            ParityObservation(
                **(
                    base
                    | {
                        "succeeded": True,
                        "failure_code": "should_not_exist",
                    }
                )
            )
        with self.assertRaises(ValidationError):
            ParityObservation(**(base | {"succeeded": "yes", "failure_code": ""}))
        with self.assertRaises(ValidationError):
            ParityObservation(**(base | {"row_count": True}))

    def test_recorded_live_replay_can_match_but_cannot_fully_verify(self) -> None:
        destination = Destination.SNOWFLAKE
        case_id = "el.raw_table_counts"
        semantic = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
        )
        replay = self.observation(
            destination=destination,
            case_id=case_id,
            engine=ParityEngine.REAL_WAREHOUSE,
        ).model_copy(
            update={
                "evidence_origin": ParityEvidenceOrigin.RECORDED_LIVE_REPLAY
            }
        )
        report = self.verify(
            destination,
            [semantic, replay],
            case_ids=[case_id],
        )
        self.assertTrue(report.reference_real_verified)
        self.assertFalse(report.live_evidence_complete)
        self.assertFalse(report.selected_scope_verified)
        self.assertFalse(report.fully_verified)

    def test_typed_relation_fingerprints_feed_exact_parity_observations(self) -> None:
        destination = Destination.REDSHIFT
        matrix = self.matrix(destination)
        fingerprint = canonical_relation_fingerprint(
            actual_columns=("ID", "AMOUNT", "_airbyte_raw_id"),
            expected_columns=(
                ("id", ColumnType.BIGINT),
                ("amount", ColumnType.DECIMAL),
            ),
            rows=[("9007199254740993", "1.230000000", "metadata")],
            allowed_extra_columns=("_airbyte_raw_id",),
        )
        semantic = relation_observation(
            case_id="t.mart_rows",
            destination=destination,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
            evidence_origin=ParityEvidenceOrigin.LOCAL_EXECUTION,
            semantic_release_id=self.semantic_release_id,
            task=self.task,
            runtime_bundle_id=self.runtime_bundle_id,
            attempt_id=self.attempt_id,
            solution_digest=self.solution_digest,
            probe_id=self.probe_id,
            probe_binding_digest=self.probe_binding_digest,
            matrix=matrix,
            population=self.population,
            population_digest=self.population_digest,
            producer_version="semantic-collector-1",
            fingerprint=fingerprint,
        )
        real = relation_observation(
            case_id="t.mart_rows",
            destination=destination,
            engine=ParityEngine.REAL_WAREHOUSE,
            evidence_origin=ParityEvidenceOrigin.LIVE_CLOUD,
            semantic_release_id=self.semantic_release_id,
            task=self.task,
            runtime_bundle_id=self.runtime_bundle_id,
            attempt_id=self.attempt_id,
            solution_digest=self.solution_digest,
            probe_id=self.probe_id,
            probe_binding_digest=self.probe_binding_digest,
            matrix=matrix,
            population=self.population,
            population_digest=self.population_digest,
            producer_version="redshift-collector-1",
            fingerprint=fingerprint,
        )
        report = self.verify(
            destination,
            [semantic, real],
            case_ids=["t.mart_rows"],
            matrix=matrix,
        )
        self.assertTrue(report.selected_scope_verified)
        self.assertFalse(report.required_scope_coverage)
        self.assertFalse(report.fully_verified)

    def test_relation_evidence_is_bound_to_kind_and_row_order_policy(self) -> None:
        destination = Destination.SNOWFLAKE
        matrix = self.matrix(destination)
        unordered = canonical_relation_fingerprint(
            actual_columns=("id",),
            expected_columns=(("id", ColumnType.INTEGER),),
            rows=[(1,), (2,)],
        )
        ordered = canonical_relation_fingerprint(
            actual_columns=("id",),
            expected_columns=(("id", ColumnType.INTEGER),),
            rows=[(1,), (2,)],
            row_order=CanonicalRowOrder.ORDERED,
        )
        common = dict(
            destination=destination,
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
            evidence_origin=ParityEvidenceOrigin.LOCAL_EXECUTION,
            semantic_release_id=self.semantic_release_id,
            task=self.task,
            runtime_bundle_id=self.runtime_bundle_id,
            attempt_id=self.attempt_id,
            solution_digest=self.solution_digest,
            probe_id=self.probe_id,
            probe_binding_digest=self.probe_binding_digest,
            matrix=matrix,
            population=self.population,
            population_digest=self.population_digest,
            producer_version="semantic-collector-1",
        )
        with self.assertRaisesRegex(ParityError, "does not accept"):
            relation_observation(
                case_id="runtime.authentication",
                fingerprint=unordered,
                **common,
            )
        with self.assertRaisesRegex(ParityError, "requires ordered"):
            relation_observation(
                case_id="warehouse.null_ordering",
                fingerprint=unordered,
                **common,
            )
        accepted = relation_observation(
            case_id="warehouse.null_ordering",
            fingerprint=ordered,
            **common,
        )
        self.assertIs(
            accepted.evidence_kind,
            ParityEvidenceKind.RELATION_FINGERPRINT,
        )
        wrong_direct = self.observation(
            destination=destination,
            case_id="warehouse.null_ordering",
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
        ).model_copy(update={"row_order": CanonicalRowOrder.UNORDERED})
        with self.assertRaisesRegex(ParityError, "row order 'ordered'"):
            self.verify(
                destination,
                [wrong_direct],
                case_ids=["warehouse.null_ordering"],
            )

    def test_report_checksum_revalidates_derived_fields(self) -> None:
        destination = Destination.SNOWFLAKE
        report = self.verify(
            destination,
            [],
            case_ids=["runtime.authentication"],
        )
        forged = report.model_copy(
            update={
                "selected_scope_verified": True,
                "fully_verified": True,
            }
        )
        with self.assertRaisesRegex(ParityError, "invariant validation"):
            parity_report_digest(forged)

        for updates in (
            {"semantic_release_id": "another-release"},
            {"task_id": "another-task"},
            {"task_content_hash": "1" * 64},
            {"required_case_manifest_digest": "2" * 64},
            {"runtime_bundle_id": "another-bundle"},
            {"attempt_id": "another-attempt"},
            {"solution_digest": "4" * 64},
            {"population": "stress"},
            {"population_digest": "3" * 64},
            {"matrix_digest": "2" * 64},
        ):
            with self.subTest(updates=updates):
                rebound = report.model_copy(update=updates)
                with self.assertRaisesRegex(ParityError, "invariant validation"):
                    parity_report_digest(rebound)

    def test_observation_evidence_kind_must_match_case_protocol(self) -> None:
        destination = Destination.DATABRICKS
        observation = self.observation(
            destination=destination,
            case_id="t.mart_rows",
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
            evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        )
        with self.assertRaisesRegex(ParityError, "evidence kind"):
            self.verify(
                destination,
                [observation],
                case_ids=["t.mart_rows"],
            )

        wrong_plane = self.observation(
            destination=destination,
            case_id="t.dbt_profile_namespace",
            engine=ParityEngine.DUCKDB_SEMANTIC_PROXY,
        )
        with self.assertRaisesRegex(ParityError, "reference engine"):
            self.verify(
                destination,
                [wrong_plane],
                case_ids=["t.dbt_profile_namespace"],
            )

    def test_evidence_origin_must_match_the_execution_plane(self) -> None:
        base = self.observation(
            destination=Destination.DATABRICKS,
            case_id="el.raw_table_counts",
            engine=ParityEngine.REAL_WAREHOUSE,
        ).model_dump(mode="python")
        with self.assertRaises(ValidationError):
            ParityObservation(
                **(base | {"evidence_origin": ParityEvidenceOrigin.LOCAL_EXECUTION})
            )
        static = self.observation(
            destination=Destination.DATABRICKS,
            case_id="t.dbt_profile_namespace",
            engine=ParityEngine.LOCAL_STATIC_CONTRACT,
        ).model_dump(mode="python")
        with self.assertRaises(ValidationError):
            ParityObservation(
                **(
                    static
                    | {"evidence_origin": ParityEvidenceOrigin.LOCAL_EXECUTION}
                )
            )


if __name__ == "__main__":
    unittest.main()

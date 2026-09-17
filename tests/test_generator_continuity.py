"""Provider-free evidence checks for code-implementation run continuity."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from elt_taskgen import cli as cli_mod, demo_fixture
from elt_taskgen.engine import (
    EVIDENCE_SUPERSESSION_KEY,
    Engine,
    ReportRevalidationEvidence,
    ReportSupersession,
    StageName,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    validate_author_revalidation_records,
)
from elt_taskgen.ingest_manifest import (
    ArtifactPin,
    DbtIngest,
    FIVE_ORIGINS,
    GeneratorPin,
    GeneratorReportRevalidationRequest,
    SelectedSourceEntries,
    SelectedSourceIngestManifest,
    persist_selected_manifest,
)
from elt_taskgen.models import Origin, canonical_json
from elt_taskgen.pipeline_readiness import (
    GenerationRunSpec,
    ReadinessProfile,
    ReadinessState,
    stage_readiness,
)
from elt_taskgen.review import trajectory as trajectory_mod


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _generator(*, version: str, digest: str) -> GeneratorPin:
    return GeneratorPin(
        name="elt-taskgen-five-source",
        version=version,
        digest_kind="sha256-generator-tree-v1",
        sha256=digest,
        lock_sha256="f" * 64,
    )


def _dbt_entry(
    task_id: str,
    *,
    selector: str | None = None,
    adapter_version: str = "adapter-v1",
    adapter_digest: str = "a" * 64,
) -> DbtIngest:
    return DbtIngest(
        pool="dbt",
        selector=selector or task_id,
        expected_task_id=task_id,
        upstream_url="https://example.invalid/source",
        upstream_revision="source-v1",
        adapter_version=adapter_version,
        adapter_digest=adapter_digest,
        license="Apache-2.0",
        license_evidence="LICENSE",
        manifest=ArtifactPin(
            path=f"{task_id}.manifest.json",
            digest_kind="sha256-file",
            sha256="b" * 64,
        ),
        family=task_id,
    )


def _selected_dbt(
    entries: tuple[DbtIngest, ...],
    *,
    generator: GeneratorPin,
    parent: str,
) -> SelectedSourceIngestManifest:
    return SelectedSourceIngestManifest(
        expected_task_count=len(entries),
        parent_manifest_sha256=parent,
        selection_seed=17,
        source_allocation={
            origin: len(entries) if origin is Origin.DBT else 0
            for origin in FIVE_ORIGINS
        },
        generator=generator,
        catalog=ArtifactPin(
            path="catalog.yaml",
            digest_kind="sha256-file",
            sha256="c" * 64,
        ),
        sources=SelectedSourceEntries(dbt=entries),
    )


def _author_records():
    task_id = "dlt__continuity_fixture"
    root = "1" * 64
    prompt = "Produce the account mart exactly as specified."
    turn_keys = ("2" * 64, "3" * 64)
    route = {
        "provider": "fixture",
        "model": "fixture-model",
        "max_tokens": 4096,
        "effort": "high",
        "behavior_sha256": "4" * 64,
        "tools_sha256": "5" * 64,
        "policy_sha256": "6" * 64,
        "diagnostics_version": "fixture-v1",
        "entry_schema": 3,
    }
    admission = {
        "admission_mode": "verified",
        "admission_record_path": "state/admission.json",
        "admission_routing_fingerprint": "7" * 64,
        "admission_evidence_sha256": "8" * 64,
        "admission_seed": "19",
    }
    contents = (
        [
            {
                "type": "tool_use",
                "id": "toolu_check",
                "name": "check_prose",
                "input": {"text": "first draft"},
            }
        ],
        [
            {
                "type": "tool_use",
                "id": "toolu_submit",
                "name": "submit_prose",
                "input": {"text": prompt},
            }
        ],
    )
    transcripts = []
    model_turns = []
    turns = []
    for index, (key, content) in enumerate(zip(turn_keys, contents, strict=True)):
        response = canonical_json(content)
        request_sha = _sha(f"request-{index}")
        entry = {
            "role": "semantic_author",
            "prompt_sha256": key,
            "system_sha256": route["behavior_sha256"],
            "provider": route["provider"],
            "model": route["model"],
            "served_model": f"served-{index}",
            "response": response,
            "response_sha256": _sha(response),
            "task_id": task_id,
            "task_content_hash": root,
            "attempt_count": 1,
            "correction_count": 0,
            "finding_count": None,
            "usage": {},
            "elapsed_ms": 1,
            "raw_attempts": [],
            "route": route,
            "admission": admission,
            "turn": {
                "turn_index": index * 2,
                "memo_key": key,
                "messages_sha256": "9" * 64,
                "user_messages": [],
                "content": content,
                "content_sha256": _sha(canonical_json(content)),
                "tool_use_ids": [content[0]["id"]],
                "tool_uses": [
                    {"id": content[0]["id"], "name": content[0]["name"]}
                ],
                "stop_reason": "tool_use",
                "truncated": False,
                "tool_choice": {},
                "tools_sha256": route["tools_sha256"],
                "observations_sha256": [],
                "session_salt": 0,
            },
        }
        transcripts.append((f"transcripts/semantic_author/{key}.json", entry))
        model_turns.append(
            {
                "index": index * 2,
                "model_turn": index,
                "category": "terminal" if index else "tool_call",
                "turn_key": key,
                # A session memo key binds the complete prefix and is not the
                # same digest as the model request recorded in the hash-chain
                # turn.  Live semantic-author evidence has this shape.
                "request_sha256": request_sha,
                "response_sha256": entry["response_sha256"],
                "raw_response": content,
                "usage": {},
                "stop_reason": "tool_use",
                "live": True,
                "outcome_code": "",
                "tool_name": content[0]["name"],
                "served_model": entry["served_model"],
            }
        )
        turns.append(
            {
                "turn_index": index * 2,
                "kind": "model",
                "model_turn": index,
                "category": "terminal" if index else "tool_call",
                "memo_key": key,
                "prompt_sha256": request_sha,
                "response_sha256": entry["response_sha256"],
                "tool_name": content[0]["name"],
                "args_sha256": "a" * 64,
                "output_sha256": "",
                "outcome_code": "",
                "refused": False,
                "action_fingerprint": "",
                "observation_fingerprint": "",
                "surface_fingerprint": "",
                "state_epoch": 0,
                "stop_reason": "tool_use",
                "route": route,
                "fresh": True,
                "usage": {},
                "admission": admission,
                "usd": 0.0,
                "replayed": False,
                "elapsed_model_ms": 1,
                "elapsed_tool_ms": 0,
            }
        )
    chain = trajectory_mod.TrajectoryChain(
        task_content_hash=root,
        tools_sha256=route["tools_sha256"],
        policy_sha256=route["policy_sha256"],
        role="semantic_author",
    )
    for turn in turns:
        chain.append(turn)
    session_sha = chain.digest
    trajectory_sha = trajectory_mod.trial_trajectory_sha256(
        session_sha,
        task_content_hash=root,
        role="semantic_author",
        behavior_sha256=route["behavior_sha256"],
        prompt_sha256=turn_keys[0],
    )
    trajectory = {
        "record_version": trajectory_mod.TRAJECTORY_RECORD_VERSION,
        "entry_schema": 3,
        "role": "semantic_author",
        "task_id": task_id,
        "task_content_hash": root,
        "trial_nonce": "",
        "trial_index": None,
        "session_key": turn_keys[0],
        "prompt_sha256": turn_keys[0],
        "response_sha256": _sha(canonical_json({"text": prompt})),
        "behavior_sha256": route["behavior_sha256"],
        "tools_sha256": route["tools_sha256"],
        "policy_sha256": route["policy_sha256"],
        "session_salt": 0,
        "session_sha256": session_sha,
        "trajectory_sha256": trajectory_sha,
        "terminal": "SUBMITTED",
        "stop_reason": "submitted",
        "turns": turns,
        "chain_hashes": chain.hashes,
        "model_turns": model_turns,
        "validator_turns": [],
        "counts": {"model_call_count": 2},
        "fault": None,
        "route": route,
        "fingerprint_components": {
            "behavior_sha256": route["behavior_sha256"],
            "tools_sha256": route["tools_sha256"],
            "policy_sha256": route["policy_sha256"],
            "diagnostics_version": route["diagnostics_version"],
        },
        "admission": admission,
    }
    session_index = {
        "entry_schema": 3,
        "role": "semantic_author",
        "session_key": turn_keys[0],
        "task_id": task_id,
        "task_content_hash": root,
        "policy_sha256": route["policy_sha256"],
        "tools_sha256": route["tools_sha256"],
        "session_salt": 0,
        "session_sha256": session_sha,
        "chain_hashes": chain.hashes,
        "terminal": "SUBMITTED",
        "turn_keys": list(turn_keys),
        "observations_sha256": [],
        "model_call_count": 2,
        "stale_tool_result_count": 0,
        "trajectory_sha256": trajectory_sha,
    }
    summary = {
        "role": "semantic_author",
        "terminal": "SUBMITTED",
        "drafts": 2,
        "revisions": 1,
        "contamination_precheck": None,
        "session": {
            "role": "semantic_author",
            "terminal": "submitted",
            "terminal_name": "SUBMITTED",
            "session_sha256": session_sha,
            "trajectory_sha256": session_sha,
            "task_content_hash": root,
            "tools_sha256": route["tools_sha256"],
            "policy_sha256": route["policy_sha256"],
            "session_salt": 0,
            "final": {"text": prompt},
            "turns": turns,
            "chain_hashes": chain.hashes,
            "model_call_count": 2,
        },
    }
    return {
        "task_id": task_id,
        "root": root,
        "prompt": prompt,
        "session_sha": session_sha,
        "trajectory_sha": trajectory_sha,
        "transcripts": tuple(transcripts),
        "session_index": session_index,
        "trajectory": trajectory,
        "summary": summary,
    }


class GeneratorContinuityEvidenceTests(unittest.TestCase):
    def test_selected_transition_binds_exact_adapter_digest_repin(self) -> None:
        old_entry = _dbt_entry("dbt__adapter_digest")
        new_entry = old_entry.model_copy(update={"adapter_digest": "d" * 64})
        authoritative = _selected_dbt(
            (old_entry,),
            generator=_generator(version="generator-v1", digest="1" * 64),
            parent="2" * 64,
        )
        reproduced = _selected_dbt(
            (new_entry,),
            generator=_generator(version="generator-v1", digest="3" * 64),
            parent="4" * 64,
        )

        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, reproduced
        )

        self.assertEqual(problem, "")
        self.assertEqual(len(transitions), 1)
        transition = transitions[0]
        self.assertEqual(transition.task_id, old_entry.expected_task_id)
        self.assertIs(transition.origin, Origin.DBT)
        self.assertEqual(transition.authoritative_version, "adapter-v1")
        self.assertEqual(transition.reproduced_version, "adapter-v1")
        self.assertEqual(transition.authoritative_digest, "a" * 64)
        self.assertEqual(transition.reproduced_digest, "d" * 64)
        self.assertEqual(
            transition.authoritative_source_entry_sha256,
            authoritative.source_entry_sha256(old_entry),
        )
        self.assertEqual(
            transition.reproduced_source_entry_sha256,
            reproduced.source_entry_sha256(new_entry),
        )

    def test_selected_transition_binds_coordinated_version_repin(self) -> None:
        old_entry = _dbt_entry(
            "dbt__adapter_version", adapter_version="package-v1"
        )
        new_entry = old_entry.model_copy(
            update={
                "adapter_version": "package-v2",
                "adapter_digest": "d" * 64,
            }
        )
        authoritative = _selected_dbt(
            (old_entry,),
            generator=_generator(version="package-v1", digest="1" * 64),
            parent="2" * 64,
        )
        reproduced = _selected_dbt(
            (new_entry,),
            generator=_generator(version="package-v2", digest="3" * 64),
            parent="4" * 64,
        )

        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, reproduced
        )

        self.assertEqual(problem, "")
        self.assertEqual(
            (
                transitions[0].authoritative_version,
                transitions[0].reproduced_version,
            ),
            ("package-v1", "package-v2"),
        )

    def test_selected_transition_rejects_nonimplementation_source_change(self) -> None:
        old_entry = _dbt_entry("dbt__semantic_drift")
        changed_entry = old_entry.model_copy(
            update={
                "selector": "different-selector",
                "adapter_digest": "d" * 64,
            }
        )
        authoritative = _selected_dbt(
            (old_entry,),
            generator=_generator(version="generator-v1", digest="1" * 64),
            parent="2" * 64,
        )
        reproduced = _selected_dbt(
            (changed_entry,),
            generator=_generator(version="generator-v1", digest="3" * 64),
            parent="4" * 64,
        )

        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, reproduced
        )

        self.assertIn("selector, source artifacts, revision, license", problem)
        self.assertEqual(transitions, ())

    def test_selected_transition_rejects_inconsistent_pool_repin(self) -> None:
        first = _dbt_entry("dbt__adapter_a")
        second = _dbt_entry("dbt__adapter_b")
        authoritative = _selected_dbt(
            (first, second),
            generator=_generator(version="generator-v1", digest="1" * 64),
            parent="2" * 64,
        )
        reproduced = _selected_dbt(
            (
                first.model_copy(update={"adapter_digest": "d" * 64}),
                second.model_copy(update={"adapter_digest": "e" * 64}),
            ),
            generator=_generator(version="generator-v1", digest="3" * 64),
            parent="4" * 64,
        )

        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, reproduced
        )

        self.assertIn("inconsistent adapter transitions", problem)
        self.assertEqual(transitions, ())

    def test_selected_transition_rejects_uncoordinated_version_repin(self) -> None:
        entry = _dbt_entry("dbt__version_mismatch")
        authoritative = _selected_dbt(
            (entry,),
            generator=_generator(version="package-v1", digest="1" * 64),
            parent="2" * 64,
        )
        reproduced = _selected_dbt(
            (entry.model_copy(update={"adapter_digest": "d" * 64}),),
            generator=_generator(version="package-v2", digest="3" * 64),
            parent="4" * 64,
        )

        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, reproduced
        )

        self.assertIn("same adapter version transition", problem)
        self.assertEqual(transitions, ())

        adapter_only = _selected_dbt(
            (
                entry.model_copy(
                    update={
                        "adapter_version": "package-v2",
                        "adapter_digest": "d" * 64,
                    }
                ),
            ),
            generator=_generator(version="package-v1", digest="3" * 64),
            parent="4" * 64,
        )
        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, adapter_only
        )
        self.assertIn("adapter version changed without", problem)
        self.assertEqual(transitions, ())

    def test_pool_reconstruction_updates_only_represented_code_pins(self) -> None:
        old_selected_entry = _dbt_entry("dbt__selected")
        old_selected = _selected_dbt(
            (old_selected_entry,),
            generator=_generator(version="generator-v1", digest="1" * 64),
            parent="2" * 64,
        )
        current_entries = (
            old_selected_entry.model_copy(update={"adapter_digest": "d" * 64}),
            _dbt_entry("dbt__unselected", adapter_digest="d" * 64),
        )
        current_sources = SelectedSourceEntries(dbt=current_entries)
        current_generator = _generator(version="generator-v1", digest="3" * 64)

        class Pool:
            def __init__(self, generator, sources):
                self.generator = generator
                self.sources = sources

            def model_copy(self, *, update):
                return Pool(
                    update.get("generator", self.generator),
                    update.get("sources", self.sources),
                )

        reconstructed = cli_mod._pool_reconstructed_at_selected_implementation(
            Pool(current_generator, current_sources), old_selected
        )

        self.assertEqual(reconstructed.generator, old_selected.generator)
        self.assertEqual(
            {entry.adapter_digest for entry in reconstructed.sources.dbt},
            {old_selected_entry.adapter_digest},
        )
        self.assertEqual(
            tuple(entry.selector for entry in reconstructed.sources.dbt),
            tuple(entry.selector for entry in current_entries),
        )
        self.assertEqual(reconstructed.sources.dlt, ())
        self.assertEqual(reconstructed.sources.synsql, ())
        self.assertEqual(reconstructed.sources.schemapile, ())
        self.assertEqual(reconstructed.sources.wikidbs, ())

    def test_v2_adapter_migration_refuses_report_revalidation_capability(self) -> None:
        old_entry = _dbt_entry("dbt__report_scope")
        new_entry = old_entry.model_copy(update={"adapter_digest": "d" * 64})
        authoritative = _selected_dbt(
            (old_entry,),
            generator=_generator(version="generator-v1", digest="1" * 64),
            parent="2" * 64,
        )
        reproduced = _selected_dbt(
            (new_entry,),
            generator=_generator(version="generator-v1", digest="3" * 64),
            parent="4" * 64,
        )
        problem, transitions = cli_mod._selected_implementation_transition(
            authoritative, reproduced
        )
        self.assertEqual(problem, "")
        pending = cli_mod._PendingConfiguredMigration(
            prior_report=SimpleNamespace(),
            prior_report_bytes=b"{}\n",
            authoritative_selected=authoritative,
            reproduced_selected=reproduced,
            authoritative_pool_sha256=authoritative.parent_manifest_sha256,
            reproduced_pool_sha256=reproduced.parent_manifest_sha256,
            adapter_transitions=transitions,
        )

        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            cli_mod.CliUsageError, "not supported while adapter implementation pins move"
        ):
            cli_mod._prepare_generator_authorization(
                workspace=Path(temporary),
                run_id="implementation-report-scope",
                spec=SimpleNamespace(),
                pool_path=Path(temporary) / "pool.json",
                pending=pending,
                report_revalidation_request=object(),
                report_revalidation_request_path=(
                    Path(temporary) / "request.json"
                ),
            )

    def test_v1_receipt_bytes_remain_schema_disjoint_from_v2(self) -> None:
        task = cli_mod._GeneratorRevalidationTask(
            task_id="dbt__receipt_compatibility",
            intake_content_hash="1" * 64,
            authoritative_provenance_sha256="2" * 64,
            reproduced_provenance_sha256="3" * 64,
            equivalence_path="state/equivalence.json",
            equivalence_sha256="4" * 64,
        )
        receipt = cli_mod._GeneratorRevalidationReceipt(
            run_id="receipt-compatibility",
            config_sha256="5" * 64,
            authoritative_selected_sha256="6" * 64,
            authoritative_selected_path="state/old-selected.json",
            authoritative_pool_sha256="7" * 64,
            authoritative_generator_sha256="8" * 64,
            reproduced_selected_sha256="9" * 64,
            reproduced_selected_path="state/new-selected.json",
            reproduced_pool_sha256="a" * 64,
            reproduced_generator_sha256="b" * 64,
            prior_readiness_sha256="c" * 64,
            ordered_task_ids=(task.task_id,),
            tasks=(task,),
            revalidation_artifact_path="",
            revalidation_artifact_sha256="0" * 64,
            authorization_path="state/authorization.json",
            authorization_sha256="d" * 64,
            equivalence_commit_path="state/commit.json",
            equivalence_commit_sha256="e" * 64,
            superseded_report_ids=(),
            replacement_report_id=0,
            replacement_payload_sha256="0" * 64,
            resumed_existing=1,
            adapter_taskirs_rederived=1,
            source_provenance_equivalence_attested=1,
            reports_locally_revalidated_and_superseded=0,
            budget_snapshot_before_sha256="f" * 64,
            budget_snapshot_after_sha256="f" * 64,
            budget_committed_usd=0.0,
            budget_reserved_usd=0.0,
            budget_uncertain_usd=0.0,
        )
        payload = receipt.deterministic_bytes()

        self.assertEqual(
            cli_mod._GeneratorRevalidationReceipt.model_validate_json(payload),
            receipt,
        )
        self.assertEqual(
            receipt.evidence_digest(),
            "7550b954e166c2d7b84bdfb5806c1faa925d7123def8ed8682e7394b37a74328",
        )
        with self.assertRaises(ValidationError):
            cli_mod._ImplementationRevalidationReceipt.model_validate_json(payload)
        v2_payload = receipt.model_copy(
            update={"schema_version": "configured-implementation-revalidation-v2"}
        ).model_dump(mode="json")
        with self.assertRaises(ValidationError):
            cli_mod._GeneratorRevalidationReceipt.model_validate(v2_payload)
        v2_receipt = cli_mod._ImplementationRevalidationReceipt.model_validate(
            v2_payload
        )
        self.assertEqual(
            v2_receipt.schema_version,
            "configured-implementation-revalidation-v2",
        )
        self.assertNotEqual(v2_receipt.evidence_digest(), receipt.evidence_digest())

    def _configured_chain_fixture(self, workspace: Path):
        task_id = "dbt__generator_chain_fixture"
        base_generator = GeneratorPin(
            name="elt-taskgen-five-source",
            version="test",
            digest_kind="sha256-generator-tree-v1",
            sha256="a" * 64,
            lock_sha256="b" * 64,
        )
        active_generator = base_generator.model_copy(update={"sha256": "c" * 64})
        final_generator = base_generator.model_copy(update={"sha256": "d" * 64})
        catalog = ArtifactPin(
            path="catalog.yaml", digest_kind="sha256-file", sha256="e" * 64
        )
        entry = DbtIngest(
            pool="dbt",
            selector="fixture",
            expected_task_id=task_id,
            upstream_url="https://example.invalid/source",
            upstream_revision="v1",
            adapter_version="v1",
            adapter_digest="f" * 64,
            license="Apache-2.0",
            license_evidence="LICENSE",
            manifest=ArtifactPin(
                path="manifest.json",
                digest_kind="sha256-file",
                sha256="0" * 64,
            ),
            family="fixture",
        )

        def selected(generator, parent):
            return SelectedSourceIngestManifest(
                expected_task_count=1,
                parent_manifest_sha256=parent,
                selection_seed=7,
                source_allocation={
                    origin: int(origin is Origin.DBT) for origin in FIVE_ORIGINS
                },
                generator=generator,
                catalog=catalog,
                sources=SelectedSourceEntries(dbt=(entry,)),
            )

        base = selected(base_generator, "1" * 64)
        active = selected(active_generator, "2" * 64)
        final = selected(final_generator, "3" * 64)

        class Pool:
            def __init__(self, generator):
                self.generator = generator

            def manifest_sha256(self):
                return {
                    base_generator.sha256: "1" * 64,
                    active_generator.sha256: "2" * 64,
                    final_generator.sha256: "3" * 64,
                }[self.generator.sha256]

            def model_copy(self, *, update):
                return Pool(update["generator"])

        task = demo_fixture.demo_task().model_copy(
            update={"task_id": task_id, "family_id": task_id, "cluster_id": task_id}
        )
        engine = Engine(workspace)
        try:
            engine.register(task)
        finally:
            engine.close()
        persist_selected_manifest(base, workspace=workspace)
        active_path = persist_selected_manifest(active, workspace=workspace)
        spec = GenerationRunSpec(
            candidate_count=1,
            source_families=(Origin.DBT.value,),
            source_allocation={Origin.DBT.value: 1},
            profile=ReadinessProfile.DRAFT,
            budget_per_task=7.0,
        )
        cli_mod._write_configured_report(
            workspace=workspace,
            run_id="generator-chain-run",
            spec=spec,
            task_ids=(task_id,),
            state="COMPLETE",
            selected_manifest=base,
            ingest_outcomes=({"task_id": task_id, "state": "created"},),
        )
        report_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / "generator-chain-run"
            / "readiness.json"
        )
        document = json.loads(report_path.read_text(encoding="utf-8"))
        document.update(
            {
                "active_source_manifest_sha256": active.manifest_sha256(),
                "active_selected_manifest_path": str(active_path),
                "generator_revalidation_receipt": str(
                    workspace / "state" / "fixture-receipt.json"
                ),
                "identity_migration_sha256": "9" * 64,
            }
        )
        report_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return base, active, final, Pool, spec

    def test_second_generator_migration_validates_prior_head_then_stays_base_anchored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            base, active, final, Pool, spec = self._configured_chain_fixture(workspace)
            committed = object()

            def verify_prior(*, workspace, pending, **_kwargs):
                self.assertEqual(pending.authoritative_selected, base)
                self.assertEqual(pending.reproduced_selected, active)
                return pending.__class__(
                    **{
                        **pending.__dict__,
                        "committed_receipt": committed,
                    }
                )

            with mock.patch.object(
                cli_mod,
                "_load_committed_generator_migration",
                side_effect=verify_prior,
            ) as verify:
                pending = cli_mod._assert_configured_run_identity(
                    workspace=workspace,
                    run_id="generator-chain-run",
                    spec=spec,
                    pool_manifest=Pool(final.generator),
                    selected_manifest=final,
                    reingest=True,
                )
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(pending.authoritative_selected, base)
            self.assertEqual(pending.reproduced_selected, final)
            self.assertIsNone(pending.committed_receipt)

            with mock.patch.object(
                cli_mod,
                "_load_committed_generator_migration",
                side_effect=verify_prior,
            ):
                with self.assertRaisesRegex(
                    cli_mod.CliUsageError, "requires explicit --reingest"
                ):
                    cli_mod._assert_configured_run_identity(
                        workspace=workspace,
                        run_id="generator-chain-run",
                        spec=spec,
                        pool_manifest=Pool(final.generator),
                        selected_manifest=final,
                        reingest=False,
                    )

            with mock.patch.object(
                cli_mod,
                "_load_committed_generator_migration",
                side_effect=verify_prior,
            ):
                resumed = cli_mod._assert_configured_run_identity(
                    workspace=workspace,
                    run_id="generator-chain-run",
                    spec=spec,
                    pool_manifest=Pool(active.generator),
                    selected_manifest=active,
                    reingest=False,
                )
            self.assertIs(resumed.committed_receipt, committed)

    def test_adapter_repin_after_generator_only_head_reconstructs_active_pool(self) -> None:
        """The direct active->new transition controls active-pool reconstruction."""

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            base, active, final, Pool, spec = self._configured_chain_fixture(workspace)
            moved_entry = final.sources.dbt[0].model_copy(
                update={"adapter_digest": "8" * 64}
            )
            final = final.model_copy(
                update={
                    "sources": final.sources.model_copy(
                        update={"dbt": (moved_entry,)}
                    )
                }
            )
            reconstructed_targets = []

            def reconstruct(_pool, target):
                reconstructed_targets.append(target)
                return SimpleNamespace(
                    manifest_sha256=lambda: target.parent_manifest_sha256
                )

            with mock.patch.object(
                cli_mod,
                "_load_committed_generator_migration",
                side_effect=lambda **kwargs: kwargs["pending"],
            ), mock.patch.object(
                cli_mod,
                "_pool_reconstructed_at_selected_implementation",
                side_effect=reconstruct,
            ):
                pending = cli_mod._assert_configured_run_identity(
                    workspace=workspace,
                    run_id="generator-chain-run",
                    spec=spec,
                    pool_manifest=Pool(final.generator),
                    selected_manifest=final,
                    reingest=True,
                )

            self.assertEqual(reconstructed_targets, [base, active])
            self.assertEqual(len(pending.adapter_transitions), 1)
            self.assertEqual(
                pending.adapter_transitions[0].reproduced_digest, "8" * 64
            )

    def test_historical_supersession_does_not_require_current_stage_readiness(
        self,
    ) -> None:
        """Receipt ancestry stays valid while the later PASS remains stale."""

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            task_id = "dlt__continuity_fixture"
            engine = Engine(workspace)
            try:
                intake = demo_fixture.demo_task().model_copy(
                    update={
                        "task_id": task_id,
                        "family_id": task_id,
                        "cluster_id": task_id,
                    }
                )
                engine.register(intake)
                registered = engine.load_task(task_id)
                historical = registered.model_copy(
                    update={"solver_prompt": "Historical solver specification."}
                )
                engine.save_task(historical)
                cause_id = engine.record_report(
                    historical,
                    StageName.AUTHOR.value,
                    VERDICT_FAIL,
                    StagePayload(error="obsolete deterministic validator finding"),
                )
                target_id = engine.record_report(
                    historical,
                    StageName.AUTHOR.value,
                    VERDICT_FATAL,
                    StagePayload(detail="old repair budget exhausted"),
                )
                cause = engine._report_by_id(cause_id)
                target = engine._report_by_id(target_id)
                assert cause is not None and target is not None

                snapshot_rel = "state/history/archived-task-ir.json"
                snapshot_path = workspace / snapshot_rel
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                snapshot_bytes = (
                    engine.task_dir(task_id) / "task_ir.json"
                ).read_bytes()
                snapshot_path.write_bytes(snapshot_bytes)
                original_keys = {
                    "prose_sha256",
                    "session_sha256",
                    "source",
                    "drafts",
                    "revisions",
                    "session_terminal",
                    "transcript_path",
                    "transcript_sha256",
                    "session_summary_path",
                    "session_summary_sha256",
                    "transcript_document_sha256",
                    "transcript_manifest",
                    "session_index_path",
                    "session_index_sha256",
                    "trajectory_path",
                    "trajectory_sha256",
                    "request_path",
                    "request_sha256",
                    "old_error",
                    "old_error_sha256",
                }
                reason = "declarative-prose-function-call-false-positive-v1"
                artifact = ReportRevalidationEvidence(
                    migration_id="1" * 64,
                    authorization_path="state/history/authorization.json",
                    authorization_sha256="2" * 64,
                    equivalence_commit_path="state/history/commit.json",
                    equivalence_commit_sha256="3" * 64,
                    run_id="continuity-run",
                    config_sha256="4" * 64,
                    authoritative_selected_sha256="5" * 64,
                    reproduced_selected_sha256="6" * 64,
                    authoritative_generator_sha256="7" * 64,
                    reproduced_generator_sha256="8" * 64,
                    task_id=task_id,
                    intake_content_hash=registered.revisions[0].content_hash,
                    task_content_hash=historical.content_hash(),
                    archived_task_ir_path=snapshot_rel,
                    archived_task_ir_sha256=hashlib.sha256(
                        snapshot_bytes
                    ).hexdigest(),
                    stage=StageName.AUTHOR.value,
                    cause_report_id=cause_id,
                    cause_payload_sha256=hashlib.sha256(
                        cause.payload_json.encode("utf-8")
                    ).hexdigest(),
                    target_report_id=target_id,
                    target_payload_sha256=hashlib.sha256(
                        target.payload_json.encode("utf-8")
                    ).hexdigest(),
                    validator_identity={
                        "generator_sha256": "8" * 64,
                        "prose_fidelity_module_sha256": "9" * 64,
                        "declarative_prose_module_sha256": "a" * 64,
                    },
                    original_evidence={key: key for key in original_keys},
                    deterministic_preconditions=(
                        "structural-completeness-green",
                        "required-attack-matrix-green",
                        "prose-fidelity-zero-findings",
                        "persisted-author-session-bound",
                        "persisted-author-transcript-exact-prompt-bound",
                    ),
                    replacement_detail="historical local revalidation passed",
                    replacement_data={"revalidation_reason": reason},
                )
                artifact_rel = "state/history/revalidation.json"
                artifact_path = workspace / artifact_rel
                artifact_path.write_bytes(artifact.deterministic_bytes())
                artifact_sha = hashlib.sha256(
                    artifact.deterministic_bytes()
                ).hexdigest()
                supersession = ReportSupersession(
                    migration_id=artifact.migration_id,
                    reason=reason,
                    stage=artifact.stage,
                    content_hash=artifact.task_content_hash,
                    revalidation_path=artifact_rel,
                    revalidation_sha256=artifact_sha,
                    cause_report_id=cause_id,
                    cause_payload_sha256=artifact.cause_payload_sha256,
                    target_report_id=target_id,
                    target_payload_sha256=artifact.target_payload_sha256,
                    replacement_detail=artifact.replacement_detail,
                    replacement_data=tuple(sorted(artifact.replacement_data.items())),
                )
                replacement_id = engine.record_report(
                    historical,
                    artifact.stage,
                    VERDICT_PASS,
                    StagePayload(
                        detail=artifact.replacement_detail,
                        data={
                            **artifact.replacement_data,
                            EVIDENCE_SUPERSESSION_KEY: canonical_json(
                                supersession.as_dict()
                            ),
                        },
                    ),
                )
                replacement = engine._report_by_id(replacement_id)
                assert replacement is not None
                receipt = SimpleNamespace(
                    run_id=artifact.run_id,
                    config_sha256=artifact.config_sha256,
                    authoritative_selected_sha256=(
                        artifact.authoritative_selected_sha256
                    ),
                    reproduced_selected_sha256=artifact.reproduced_selected_sha256,
                    authoritative_generator_sha256=(
                        artifact.authoritative_generator_sha256
                    ),
                    reproduced_generator_sha256=artifact.reproduced_generator_sha256,
                    authorization_path=artifact.authorization_path,
                    authorization_sha256=artifact.authorization_sha256,
                    equivalence_commit_path=artifact.equivalence_commit_path,
                    equivalence_commit_sha256=artifact.equivalence_commit_sha256,
                    revalidation_artifact_path=artifact_rel,
                    revalidation_artifact_sha256=artifact_sha,
                    superseded_report_ids=(target_id,),
                    replacement_report_id=replacement_id,
                    replacement_payload_sha256=hashlib.sha256(
                        replacement.payload_json.encode("utf-8")
                    ).hexdigest(),
                )

                current = historical.model_copy(
                    update={"solver_prompt": "Later ordinary solver specification."}
                )
                engine.save_task(current)
                engine.record_report(
                    current,
                    StageName.AUTHOR.value,
                    VERDICT_PASS,
                    StagePayload(
                        detail="ordinary forward author PASS",
                        data={
                            "prose_sha256": hashlib.sha256(
                                current.solver_prompt.encode("utf-8")
                            ).hexdigest()
                            # Deliberately no behavior_sha256: current readiness
                            # must remain stale after ancestry verification.
                        },
                    ),
                )
                report_count_before = engine._con.execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM reports"
                ).fetchone()[0]
                task_bytes_before = (
                    engine.task_dir(task_id) / "task_ir.json"
                ).read_bytes()

                self.assertTrue(
                    cli_mod._committed_supersession_or_forward_pass_is_current(
                        engine=engine,
                        task=current,
                        artifact=artifact,
                        receipt=receipt,
                    )
                )
                readiness = stage_readiness(
                    engine, current, StageName.AUTHOR, spec=None
                )
                self.assertIs(readiness.state, ReadinessState.STALE)
                self.assertIn("semantic-author behavior", readiness.reason)
                self.assertEqual(
                    engine._con.execute(  # noqa: SLF001
                        "SELECT COUNT(*) FROM reports"
                    ).fetchone()[0],
                    report_count_before,
                )
                self.assertEqual(
                    (engine.task_dir(task_id) / "task_ir.json").read_bytes(),
                    task_bytes_before,
                )

                # Every historical binding is material: a replacement digest
                # or artifact/marker mismatch fails closed.
                bad_receipt = SimpleNamespace(
                    **{
                        **vars(receipt),
                        "replacement_payload_sha256": "0" * 64,
                    }
                )
                self.assertFalse(
                    cli_mod._committed_supersession_or_forward_pass_is_current(
                        engine=engine,
                        task=current,
                        artifact=artifact,
                        receipt=bad_receipt,
                    )
                )
                bad_artifact = artifact.model_copy(
                    update={"cause_payload_sha256": "0" * 64}
                )
                self.assertFalse(
                    cli_mod._committed_supersession_or_forward_pass_is_current(
                        engine=engine,
                        task=current,
                        artifact=bad_artifact,
                        receipt=receipt,
                    )
                )

                original_report_by_id = engine._report_by_id

                def wrong_target(report_id):
                    row = original_report_by_id(report_id)
                    if report_id == target_id and row is not None:
                        return SimpleNamespace(
                            **{**vars(row), "verdict": VERDICT_PASS}
                        )
                    return row

                with mock.patch.object(
                    engine, "_report_by_id", side_effect=wrong_target
                ):
                    self.assertFalse(
                        cli_mod._committed_supersession_or_forward_pass_is_current(
                            engine=engine,
                            task=current,
                            artifact=artifact,
                            receipt=receipt,
                        )
                    )

                original_marker = engine._supersession_marker

                def wrong_marker(row):
                    marker = original_marker(row)
                    if row.id == replacement_id and marker is not None:
                        return {**marker, "reason": "different-bound-reason"}
                    return marker

                with mock.patch.object(
                    engine, "_supersession_marker", side_effect=wrong_marker
                ):
                    self.assertFalse(
                        cli_mod._committed_supersession_or_forward_pass_is_current(
                            engine=engine,
                            task=current,
                            artifact=artifact,
                            receipt=receipt,
                        )
                    )

                original_read = engine._read_supersession_evidence

                def wrong_archive(path, *, label):
                    payload = original_read(path, label=label)
                    return payload + b" " if path == snapshot_rel else payload

                with mock.patch.object(
                    engine,
                    "_read_supersession_evidence",
                    side_effect=wrong_archive,
                ):
                    self.assertFalse(
                        cli_mod._committed_supersession_or_forward_pass_is_current(
                            engine=engine,
                            task=current,
                            artifact=artifact,
                            receipt=receipt,
                        )
                    )
            finally:
                engine.close()

    def test_exact_author_session_records_verify_and_bind_route(self) -> None:
        records = _author_records()
        bindings = validate_author_revalidation_records(
            task_id=records["task_id"],
            intake_content_hash=records["root"],
            prompt=records["prompt"],
            session_sha256=records["session_sha"],
            transcript_records=records["transcripts"],
            session_index_path=(
                f"transcripts/semantic_author/sessions/"
                f"{records['transcripts'][0][0].split('/')[-1]}"
            ),
            session_index=records["session_index"],
            trajectory_path=f"trajectories/semantic_author/{records['trajectory_sha']}.json",
            trajectory=records["trajectory"],
            session_summary=records["summary"],
        )
        self.assertEqual(bindings["provider"], "fixture")
        self.assertEqual(bindings["session_key"], "2" * 64)

    def test_author_record_tamper_fails_closed(self) -> None:
        records = _author_records()
        tampered = copy.deepcopy(records["transcripts"])
        tampered[-1][1]["turn"]["content"][0]["input"]["text"] = "other"
        with self.assertRaisesRegex(ValueError, "transcript turn"):
            validate_author_revalidation_records(
                task_id=records["task_id"],
                intake_content_hash=records["root"],
                prompt=records["prompt"],
                session_sha256=records["session_sha"],
                transcript_records=tampered,
                session_index_path=f"sessions/{'2' * 64}.json",
                session_index=records["session_index"],
                trajectory_path=f"trajectories/{records['trajectory_sha']}.json",
                trajectory=records["trajectory"],
                session_summary=records["summary"],
            )

    def test_request_binds_the_complete_old_error(self) -> None:
        error = "prose fidelity: 2 omissions — first; second"
        request = GeneratorReportRevalidationRequest(
            run_id="continuity-run",
            config_sha256="a" * 64,
            authoritative_selected_sha256="b" * 64,
            reproduced_selected_sha256="c" * 64,
            authoritative_generator_sha256="d" * 64,
            reproduced_generator_sha256="e" * 64,
            reason="declarative-prose-function-call-false-positive-v1",
            task_id="dlt__continuity_fixture",
            task_content_hash="f" * 64,
            cause_report_id=10,
            cause_payload_sha256="1" * 64,
            target_report_id=11,
            target_payload_sha256="2" * 64,
            prose_sha256="3" * 64,
            author_session_sha256="4" * 64,
            transcript_paths=("transcripts/a.json",),
            transcript_sha256s=("5" * 64,),
            session_index_path="sessions/a.json",
            session_index_sha256="6" * 64,
            trajectory_path="trajectories/a.json",
            trajectory_sha256="7" * 64,
            session_summary_path="tasks/session.json",
            session_summary_sha256="8" * 64,
            expected_old_error=error,
            expected_old_error_sha256=_sha(error),
        )
        with self.assertRaisesRegex(ValidationError, "digest differs"):
            GeneratorReportRevalidationRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "expected_old_error_sha256": "9" * 64,
                }
            )

    def test_legacy_readiness_without_selected_count_allows_exact_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            task_id = "dbt__legacy_selected_count"
            old_generator = GeneratorPin(
                name="elt-taskgen-five-source",
                version="test",
                digest_kind="sha256-generator-tree-v1",
                sha256="a" * 64,
                lock_sha256="b" * 64,
            )
            new_generator = old_generator.model_copy(update={"sha256": "c" * 64})
            catalog = ArtifactPin(
                path="catalog.yaml", digest_kind="sha256-file", sha256="d" * 64
            )
            entry = DbtIngest(
                pool="dbt",
                selector="fixture",
                expected_task_id=task_id,
                upstream_url="https://example.invalid/source",
                upstream_revision="v1",
                adapter_version="v1",
                adapter_digest="e" * 64,
                license="Apache-2.0",
                license_evidence="LICENSE",
                manifest=ArtifactPin(
                    path="manifest.json",
                    digest_kind="sha256-file",
                    sha256="f" * 64,
                ),
                family="fixture",
            )

            def selected(generator, parent):
                return SelectedSourceIngestManifest(
                    expected_task_count=1,
                    parent_manifest_sha256=parent,
                    selection_seed=7,
                    source_allocation={
                        origin: int(origin is Origin.DBT) for origin in FIVE_ORIGINS
                    },
                    generator=generator,
                    catalog=catalog,
                    sources=SelectedSourceEntries(dbt=(entry,)),
                )

            old_selected = selected(old_generator, "1" * 64)
            new_selected = selected(new_generator, "2" * 64)

            class Pool:
                def __init__(self, generator):
                    self.generator = generator

                def manifest_sha256(self):
                    return "2" * 64 if self.generator == new_generator else "1" * 64

                def model_copy(self, *, update):
                    return Pool(update["generator"])

            task = demo_fixture.demo_task().model_copy(
                update={"task_id": task_id, "family_id": task_id, "cluster_id": task_id}
            )
            engine = Engine(workspace)
            try:
                engine.register(task)
            finally:
                engine.close()
            persist_selected_manifest(old_selected, workspace=workspace)
            spec = GenerationRunSpec(
                candidate_count=1,
                source_families=(Origin.DBT.value,),
                source_allocation={Origin.DBT.value: 1},
                profile=ReadinessProfile.DRAFT,
                budget_per_task=7.0,
            )
            cli_mod._write_configured_report(
                workspace=workspace,
                run_id="legacy-selected-run",
                spec=spec,
                task_ids=(task_id,),
                state="COMPLETE",
                selected_manifest=old_selected,
                ingest_outcomes=(
                    {"task_id": task_id, "state": "created"},
                ),
            )
            report_path = (
                workspace
                / "state"
                / "pipeline_runs"
                / "legacy-selected-run"
                / "readiness.json"
            )
            document = json.loads(report_path.read_text(encoding="utf-8"))
            document["counts"].pop("selected")
            report_path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            pending = cli_mod._assert_configured_run_identity(
                workspace=workspace,
                run_id="legacy-selected-run",
                spec=spec,
                pool_manifest=Pool(new_generator),
                selected_manifest=new_selected,
                reingest=True,
            )
            self.assertIsNotNone(pending)

            document["counts"]["selected"] = 0
            report_path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                cli_mod.CliUsageError, "task roster/order/count"
            ):
                cli_mod._assert_configured_run_identity(
                    workspace=workspace,
                    run_id="legacy-selected-run",
                    spec=spec,
                    pool_manifest=Pool(new_generator),
                    selected_manifest=new_selected,
                    reingest=True,
                )


if __name__ == "__main__":
    unittest.main()

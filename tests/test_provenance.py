"""Typed, append-only ingest provenance and source digest tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.engine import Engine
from elt_taskgen.models import Origin
from elt_taskgen.provenance import (
    GENERATOR_EQUIVALENCE_SCHEMA_VERSION,
    INGEST_PROVENANCE_DIRNAME,
    IMPLEMENTATION_EQUIVALENCE_SCHEMA_VERSION,
    GeneratorEquivalenceAttestation,
    IngestProvenance,
    ImplementationAdapterTransition,
    ImplementationEquivalenceAttestation,
    ProvenanceArtifact,
    SourceIdentity,
    generator_equivalence_problem,
    implementation_equivalence_problem,
    load_current,
    load_for_lineage,
    publish_generator_equivalence,
    publish_implementation_equivalence,
    publish_or_confirm,
    sha256_canonical_json,
    sha256_file,
    sha256_tree,
)


class IngestProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.engine = Engine(self.workspace)
        self.engine.register(demo_fixture.demo_task())
        self.task = self.engine.load_task(demo_fixture.demo_task().task_id)

    def tearDown(self) -> None:
        self.engine.close()
        self.tmp.cleanup()

    def identity(self, **updates: str) -> SourceIdentity:
        selection_inputs = {
            "ingest_manifest": ProvenanceArtifact(
                locator="manifest-schema:five-source-ingest-v1",
                digest="3" * 64,
                digest_kind="sha256-canonical-json-v1",
            ),
            "source_catalog": ProvenanceArtifact(
                locator="manifest:catalog",
                digest="4" * 64,
                digest_kind="sha256-file",
            ),
        }
        values = {
            "pool": self.task.origin.value,
            "origin": self.task.origin,
            "selector": "fixture/demo-v1",
            "upstream_url": "https://example.invalid/demo",
            "upstream_revision": "demo-v1",
            "source_digest": hashlib.sha256(b"source").hexdigest(),
            "source_digest_kind": "sha256-file",
            "adapter_name": "elt_taskgen.demo_fixture",
            "adapter_version": "test-v1",
            "adapter_digest": hashlib.sha256(b"adapter").hexdigest(),
            "license": self.task.license,
            "license_evidence": "repository:LICENSE",
            "selection_inputs": selection_inputs,
        }
        values.update(updates)
        return SourceIdentity(**values)

    def record(self, **source_updates: str) -> IngestProvenance:
        return IngestProvenance(
            task_id=self.task.task_id,
            task_content_hash=self.task.revisions[0].content_hash,
            source=self.identity(**source_updates),
        )

    def test_publish_is_idempotent_append_only_and_hash_neutral(self) -> None:
        before = self.task.content_hash()
        record = self.record()
        first = publish_or_confirm(self.engine.task_dir(self.task.task_id), record)
        second = publish_or_confirm(self.engine.task_dir(self.task.task_id), record)

        self.assertEqual(first, second)
        self.assertEqual(
            first.name,
            f"{self.task.revisions[0].content_hash}.{record.evidence_digest()}.json",
        )
        self.assertEqual(load_current(self.engine.task_dir(self.task.task_id)), record)
        self.assertEqual(self.engine.load_task(self.task.task_id).content_hash(), before)
        self.assertNotIn("timestamp", first.read_text(encoding="utf-8"))
        self.assertEqual(first.read_bytes(), record.deterministic_bytes())
        claim = first.parent / f"{record.task_content_hash}.claim"
        self.assertEqual(claim.read_text(encoding="ascii"), record.evidence_digest() + "\n")

    def test_same_lineage_cannot_be_relabelled(self) -> None:
        publish_or_confirm(self.engine.task_dir(self.task.task_id), self.record())
        with self.assertRaisesRegex(ValueError, "lineage already has different"):
            publish_or_confirm(
                self.engine.task_dir(self.task.task_id),
                self.record(upstream_revision="demo-v2"),
            )

    def test_authoring_hash_move_keeps_intake_provenance_current(self) -> None:
        record = self.record()
        publish_or_confirm(self.engine.task_dir(self.task.task_id), record)
        authored = self.task.model_copy(update={"title": "Authored title"})
        self.assertNotEqual(authored.content_hash(), self.task.content_hash())
        self.engine.save_task(authored)

        self.assertEqual(
            load_current(self.engine.task_dir(self.task.task_id)),
            record,
        )

    def test_reingest_appends_a_new_lineage_record(self) -> None:
        old = self.record()
        publish_or_confirm(self.engine.task_dir(self.task.task_id), old)
        reingested = self.task.model_copy(
            update={"title": "new source semantics", "revisions": ()}
        )
        self.engine.register(reingested, allow_overwrite=True)
        self.task = self.engine.load_task(self.task.task_id)
        new = self.record(
            upstream_revision="demo-v2",
            source_digest=hashlib.sha256(b"source-v2").hexdigest(),
        )
        publish_or_confirm(self.engine.task_dir(self.task.task_id), new)

        store = self.engine.task_dir(self.task.task_id) / INGEST_PROVENANCE_DIRNAME
        self.assertEqual(len(tuple(store.glob("*.json"))), 2)
        self.assertEqual(load_current(self.engine.task_dir(self.task.task_id)), new)

    def test_arbitrary_lineage_can_be_preflighted_before_task_replacement(self) -> None:
        old = self.record()
        publish_or_confirm(self.engine.task_dir(self.task.task_id), old)
        reingested = self.task.model_copy(
            update={"title": "new source semantics", "revisions": ()}
        )
        self.engine.register(reingested, allow_overwrite=True)
        self.task = self.engine.load_task(self.task.task_id)
        new = self.record(
            upstream_revision="demo-v2",
            source_digest=hashlib.sha256(b"source-v2").hexdigest(),
        )
        publish_or_confirm(self.engine.task_dir(self.task.task_id), new)

        self.assertEqual(
            load_for_lineage(
                self.engine.task_dir(self.task.task_id),
                task_id=self.task.task_id,
                lineage_hash=old.task_content_hash,
            ),
            old,
        )

    def test_lineage_preflight_is_read_only_and_rejects_ambiguity(self) -> None:
        task_dir = self.engine.task_dir(self.task.task_id)
        store = task_dir / INGEST_PROVENANCE_DIRNAME
        self.assertIsNone(
            load_for_lineage(
                task_dir,
                task_id=self.task.task_id,
                lineage_hash=self.record().task_content_hash,
                required=False,
            )
        )
        self.assertFalse(store.exists())

        first = self.record()
        publish_or_confirm(task_dir, first)
        competing = self.record(upstream_revision="demo-v2")
        competing_path = store / (
            f"{competing.task_content_hash}.{competing.evidence_digest()}.json"
        )
        competing_path.write_bytes(competing.deterministic_bytes())
        with self.assertRaisesRegex(ValueError, "2 competing"):
            load_for_lineage(
                task_dir,
                task_id=self.task.task_id,
                lineage_hash=first.task_content_hash,
            )

    def test_concurrent_different_claims_publish_exactly_one_record(self) -> None:
        task_dir = self.engine.task_dir(self.task.task_id)
        first = self.record()
        second = self.record(upstream_revision="demo-v2")
        barrier = threading.Barrier(2)
        real_link = __import__("os").link

        def synchronized_link(source, destination, *args, **kwargs):
            if str(destination).endswith(".claim"):
                barrier.wait(timeout=5)
            return real_link(source, destination, *args, **kwargs)

        with mock.patch("elt_taskgen.provenance.os.link", synchronized_link):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(publish_or_confirm, task_dir, record)
                    for record in (first, second)
                ]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except ValueError as exc:
                        outcomes.append(exc)

        self.assertEqual(sum(isinstance(value, Path) for value in outcomes), 1)
        errors = [value for value in outcomes if isinstance(value, ValueError)]
        self.assertEqual(len(errors), 1)
        self.assertIn("lineage already has different", str(errors[0]))
        winner = load_current(task_dir)
        self.assertIn(winner, (first, second))
        store = task_dir / INGEST_PROVENANCE_DIRNAME
        self.assertEqual(len(tuple(store.glob("*.json"))), 1)
        self.assertEqual(len(tuple(store.glob("*.claim"))), 1)

    def test_same_digest_recovers_a_claim_only_crash(self) -> None:
        task_dir = self.engine.task_dir(self.task.task_id)
        record = self.record()
        competing = self.record(upstream_revision="demo-v2")
        real_link = __import__("os").link
        interrupted = False

        def interrupt_record_link(source, destination, *args, **kwargs):
            nonlocal interrupted
            if str(destination).endswith(".json") and not interrupted:
                interrupted = True
                raise OSError("simulated crash after claim publication")
            return real_link(source, destination, *args, **kwargs)

        with mock.patch("elt_taskgen.provenance.os.link", interrupt_record_link):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                publish_or_confirm(task_dir, record)

        store = task_dir / INGEST_PROVENANCE_DIRNAME
        self.assertEqual(len(tuple(store.glob("*.claim"))), 1)
        self.assertEqual(len(tuple(store.glob("*.json"))), 0)
        with self.assertRaisesRegex(ValueError, r"claim\(s\) without records"):
            load_current(task_dir)
        self.assertIsNone(
            load_for_lineage(
                task_dir,
                task_id=self.task.task_id,
                lineage_hash=record.task_content_hash,
                required=False,
                recoverable_evidence_digest=record.evidence_digest(),
            )
        )
        with self.assertRaisesRegex(ValueError, r"claim\(s\) without records"):
            load_for_lineage(
                task_dir,
                task_id=self.task.task_id,
                lineage_hash=record.task_content_hash,
                required=False,
                recoverable_evidence_digest=competing.evidence_digest(),
            )
        with self.assertRaisesRegex(ValueError, r"claim\(s\) without records"):
            publish_or_confirm(task_dir, competing)

        published = publish_or_confirm(task_dir, record)
        self.assertTrue(published.is_file())
        self.assertEqual(load_current(task_dir), record)

    def test_no_replace_race_never_follows_a_planted_symlink(self) -> None:
        task_dir = self.engine.task_dir(self.task.task_id)
        outside = self.workspace / "outside.txt"
        outside.write_bytes(b"do not read or chmod")

        def plant_symlink(_source, destination, *args, **kwargs):
            Path(destination).symlink_to(outside)
            raise FileExistsError(destination)

        with mock.patch("elt_taskgen.provenance.os.link", plant_symlink):
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                publish_or_confirm(task_dir, self.record())

        self.assertEqual(outside.read_bytes(), b"do not read or chmod")
        self.assertTrue(outside.stat().st_mode & 0o200)

    def test_tampered_or_nondeterministic_record_fails_closed(self) -> None:
        path = publish_or_confirm(
            self.engine.task_dir(self.task.task_id), self.record()
        )
        path.chmod(0o644)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(parsed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not in deterministic form"):
            load_current(self.engine.task_dir(self.task.task_id))


class ImplementationEquivalenceTest(unittest.TestCase):
    """V2 admits only exact, task-neutral implementation-pin movement."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.engine = Engine(self.workspace)
        base = demo_fixture.demo_task()
        draft = base.model_copy(
            update={
                "task_id": "dlt__implementation_equivalence_fixture",
                "family_id": "dlt__implementation_equivalence_fixture",
                "cluster_id": "dlt__implementation_equivalence_fixture",
                "origin": Origin.DLT,
                "revisions": (),
            }
        )
        self.engine.register(draft)
        self.task = self.engine.load_task(draft.task_id)

    def tearDown(self) -> None:
        self.engine.close()
        self.tmp.cleanup()

    def record(
        self,
        *,
        generator_digest: str,
        adapter_digest: str = "2" * 64,
        adapter_version: str = "0.2.0",
        generator_version: str | None = None,
        source_entry_digest: str = "3" * 64,
        source_updates: dict | None = None,
        input_updates: dict[str, ProvenanceArtifact] | None = None,
    ) -> IngestProvenance:
        generator_version = generator_version or adapter_version
        selection_inputs = {
            "ingest_manifest": ProvenanceArtifact(
                locator=(
                    "manifest-entry:selected-source-ingest-v3:dlt:"
                    f"{self.task.task_id}"
                ),
                digest=source_entry_digest,
                digest_kind="sha256-canonical-json-v1",
            ),
            "source_catalog": ProvenanceArtifact(
                locator="manifest:catalog",
                digest="4" * 64,
                digest_kind="sha256-file",
            ),
            "generator_code": ProvenanceArtifact(
                locator=(
                    "generator:elt-taskgen-five-source@"
                    f"{generator_version}"
                ),
                digest=generator_digest,
                digest_kind="sha256-canonical-json-v1",
            ),
            "dependency_lock": ProvenanceArtifact(
                locator="package-resource:uv.lock",
                digest="5" * 64,
                digest_kind="sha256-file",
            ),
        }
        selection_inputs.update(input_updates or {})
        source_values = {
            "pool": "dlt",
            "origin": Origin.DLT,
            "selector": "fixture/implementation-v1",
            "upstream_url": "https://example.invalid/dlt-fixture",
            "upstream_revision": "fixture-v1",
            "source_digest": "1" * 64,
            "source_digest_kind": "sha256-file",
            "adapter_name": "elt_taskgen.adapters.dlt",
            "adapter_version": adapter_version,
            "adapter_digest": adapter_digest,
            "license": self.task.license,
            "license_evidence": "repository:LICENSE",
            "selection_inputs": selection_inputs,
        }
        source_values.update(source_updates or {})
        return IngestProvenance(
            task_id=self.task.task_id,
            task_content_hash=self.task.revisions[0].content_hash,
            source=SourceIdentity.model_validate(source_values),
        )

    def transition(self, **updates) -> ImplementationAdapterTransition:
        values = {
            "pool": Origin.DLT,
            "adapter_name": "elt_taskgen.adapters.dlt",
            "authoritative_version": "0.2.0",
            "reproduced_version": "0.2.0",
            "authoritative_digest": "2" * 64,
            "reproduced_digest": "6" * 64,
            "authoritative_source_entry_sha256": "3" * 64,
            "reproduced_source_entry_sha256": "7" * 64,
        }
        values.update(updates)
        return ImplementationAdapterTransition(**values)

    def test_v1_bytes_remain_stable_and_v1_still_refuses_adapter_drift(self) -> None:
        authoritative = self.record(generator_digest="8" * 64)
        reproduced = self.record(generator_digest="9" * 64)
        publish_or_confirm(self.engine.task_dir(self.task.task_id), authoritative)

        attestation = GeneratorEquivalenceAttestation(
            task_id=self.task.task_id,
            task_content_hash=authoritative.task_content_hash,
            authoritative_evidence_digest=authoritative.evidence_digest(),
            reproduced_provenance=reproduced,
        )
        self.assertEqual(
            attestation.schema_version, GENERATOR_EQUIVALENCE_SCHEMA_VERSION
        )
        self.assertEqual(
            hashlib.sha256(attestation.deterministic_bytes()).hexdigest(),
            "ab63b496f1365d87322e37805b717e2810e7f8fa6e6280d3d0b59a1399a938e3",
        )
        path = publish_generator_equivalence(
            self.engine.task_dir(self.task.task_id),
            authoritative=authoritative,
            reproduced=reproduced,
        )
        self.assertTrue(path.name.endswith(".generator-equivalence.json"))
        self.assertEqual(path.read_bytes(), attestation.deterministic_bytes())

        adapter_reproduced = self.record(
            generator_digest="a" * 64,
            adapter_digest="6" * 64,
            source_entry_digest="7" * 64,
        )
        self.assertIn(
            "adapter",
            generator_equivalence_problem(authoritative, adapter_reproduced),
        )
        with self.assertRaisesRegex(ValueError, "invalid generator equivalence"):
            publish_generator_equivalence(
                self.engine.task_dir(self.task.task_id),
                authoritative=authoritative,
                reproduced=adapter_reproduced,
            )

    def test_v2_publishes_exact_adapter_and_source_entry_transition(self) -> None:
        authoritative = self.record(generator_digest="8" * 64)
        reproduced = self.record(
            generator_digest="9" * 64,
            adapter_digest="6" * 64,
            source_entry_digest="7" * 64,
        )
        transition = self.transition()
        publish_or_confirm(self.engine.task_dir(self.task.task_id), authoritative)

        self.assertEqual(
            implementation_equivalence_problem(
                authoritative,
                reproduced,
                adapter_transition=transition,
            ),
            "",
        )
        first = publish_implementation_equivalence(
            self.engine.task_dir(self.task.task_id),
            authoritative=authoritative,
            reproduced=reproduced,
            adapter_transition=transition,
        )
        second = publish_implementation_equivalence(
            self.engine.task_dir(self.task.task_id),
            authoritative=authoritative,
            reproduced=reproduced,
            adapter_transition=transition,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first.name,
            (
                f"{authoritative.task_content_hash}."
                f"{reproduced.evidence_digest()}.implementation-equivalence.json"
            ),
        )
        attestation = ImplementationEquivalenceAttestation.model_validate_json(
            first.read_bytes()
        )
        self.assertEqual(
            attestation.schema_version, IMPLEMENTATION_EQUIVALENCE_SCHEMA_VERSION
        )
        self.assertEqual(attestation.adapter_transition, transition)
        self.assertEqual(
            attestation.evidence_digest(),
            sha256_canonical_json(attestation.model_dump(mode="json")),
        )
        self.assertEqual(
            load_current(self.engine.task_dir(self.task.task_id)), authoritative
        )

    def test_v2_permits_version_only_transition_and_binds_generator_locator(
        self,
    ) -> None:
        authoritative = self.record(generator_digest="8" * 64)
        reproduced = self.record(
            generator_digest="9" * 64,
            adapter_version="0.3.0",
            adapter_digest="2" * 64,
            source_entry_digest="7" * 64,
        )
        transition = self.transition(
            reproduced_version="0.3.0",
            reproduced_digest="2" * 64,
        )
        publish_or_confirm(self.engine.task_dir(self.task.task_id), authoritative)

        self.assertEqual(
            implementation_equivalence_problem(
                authoritative,
                reproduced,
                adapter_transition=transition,
            ),
            "",
        )
        path = publish_implementation_equivalence(
            self.engine.task_dir(self.task.task_id),
            authoritative=authoritative,
            reproduced=reproduced,
            adapter_transition=transition,
        )
        parsed = ImplementationEquivalenceAttestation.model_validate_json(
            path.read_bytes()
        )
        self.assertEqual(parsed.adapter_transition, transition)
        self.assertEqual(
            parsed.reproduced_provenance.source.adapter_version, "0.3.0"
        )

    def test_v2_uses_one_envelope_for_unchanged_adapter_roster_members(self) -> None:
        authoritative = self.record(generator_digest="8" * 64)
        reproduced = self.record(generator_digest="9" * 64)
        publish_or_confirm(self.engine.task_dir(self.task.task_id), authoritative)

        self.assertEqual(
            implementation_equivalence_problem(
                authoritative, reproduced, adapter_transition=None
            ),
            "",
        )
        path = publish_implementation_equivalence(
            self.engine.task_dir(self.task.task_id),
            authoritative=authoritative,
            reproduced=reproduced,
            adapter_transition=None,
        )
        parsed = ImplementationEquivalenceAttestation.model_validate_json(
            path.read_bytes()
        )
        self.assertIsNone(parsed.adapter_transition)
        self.assertTrue(path.name.endswith(".implementation-equivalence.json"))

    def test_v2_refuses_unbound_or_unrelated_provenance_drift(self) -> None:
        authoritative = self.record(generator_digest="8" * 64)
        reproduced = self.record(
            generator_digest="9" * 64,
            adapter_digest="6" * 64,
            source_entry_digest="7" * 64,
        )
        transition = self.transition()
        cases = {
            "missing transition": (reproduced, None),
            "wrong pool": (
                reproduced,
                transition.model_copy(update={"pool": Origin.DBT}),
            ),
            "wrong adapter name": (
                reproduced,
                transition.model_copy(update={"adapter_name": "other.adapter"}),
            ),
            "wrong old adapter version": (
                reproduced,
                transition.model_copy(update={"authoritative_version": "0.1.0"}),
            ),
            "wrong new adapter version": (
                reproduced,
                transition.model_copy(update={"reproduced_version": "0.3.0"}),
            ),
            "wrong old adapter digest": (
                reproduced,
                transition.model_copy(update={"authoritative_digest": "b" * 64}),
            ),
            "wrong new adapter digest": (
                reproduced,
                transition.model_copy(update={"reproduced_digest": "b" * 64}),
            ),
            "wrong old source entry": (
                reproduced,
                transition.model_copy(
                    update={"authoritative_source_entry_sha256": "b" * 64}
                ),
            ),
            "wrong new source entry": (
                reproduced,
                transition.model_copy(
                    update={"reproduced_source_entry_sha256": "b" * 64}
                ),
            ),
            "upstream revision": (
                self.record(
                    generator_digest="9" * 64,
                    adapter_digest="6" * 64,
                    source_entry_digest="7" * 64,
                    source_updates={"upstream_revision": "fixture-v2"},
                ),
                transition,
            ),
            "source catalog": (
                self.record(
                    generator_digest="9" * 64,
                    adapter_digest="6" * 64,
                    source_entry_digest="7" * 64,
                    input_updates={
                        "source_catalog": ProvenanceArtifact(
                            locator="manifest:catalog",
                            digest="c" * 64,
                            digest_kind="sha256-file",
                        )
                    },
                ),
                transition,
            ),
            "dependency lock": (
                self.record(
                    generator_digest="9" * 64,
                    adapter_digest="6" * 64,
                    source_entry_digest="7" * 64,
                    input_updates={
                        "dependency_lock": ProvenanceArtifact(
                            locator="package-resource:uv.lock",
                            digest="d" * 64,
                            digest_kind="sha256-file",
                        )
                    },
                ),
                transition,
            ),
            "unchanged generator": (
                self.record(
                    generator_digest="8" * 64,
                    adapter_digest="6" * 64,
                    source_entry_digest="7" * 64,
                ),
                transition,
            ),
            "version move without generator locator move": (
                self.record(
                    generator_digest="9" * 64,
                    adapter_version="0.3.0",
                    generator_version="0.2.0",
                    adapter_digest="2" * 64,
                    source_entry_digest="7" * 64,
                ),
                self.transition(
                    reproduced_version="0.3.0",
                    reproduced_digest="2" * 64,
                ),
            ),
        }
        for label, (candidate, candidate_transition) in cases.items():
            with self.subTest(label=label):
                self.assertTrue(
                    implementation_equivalence_problem(
                        authoritative,
                        candidate,
                        adapter_transition=candidate_transition,
                    )
                )

        with self.assertRaisesRegex(ValueError, "version and/or digest"):
            self.transition(reproduced_digest="2" * 64)
        with self.assertRaisesRegex(ValueError, "must change the source-entry digest"):
            self.transition(reproduced_source_entry_sha256="3" * 64)
        with self.assertRaisesRegex(ValueError, "five sources"):
            self.transition(pool=Origin.DEMO)
        with self.assertRaisesRegex(ValueError, "clean printable"):
            self.transition(reproduced_version=" 0.3.0")

    def test_v2_store_loader_rejects_tampered_transition_and_filename(self) -> None:
        authoritative = self.record(generator_digest="8" * 64)
        reproduced = self.record(
            generator_digest="9" * 64,
            adapter_digest="6" * 64,
            source_entry_digest="7" * 64,
        )
        transition = self.transition()
        publish_or_confirm(self.engine.task_dir(self.task.task_id), authoritative)
        path = publish_implementation_equivalence(
            self.engine.task_dir(self.task.task_id),
            authoritative=authoritative,
            reproduced=reproduced,
            adapter_transition=transition,
        )
        parsed = json.loads(path.read_text(encoding="utf-8"))
        parsed["adapter_transition"]["reproduced_digest"] = "b" * 64
        path.chmod(0o644)
        path.write_text(
            json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "invalid implementation equivalence"):
            load_current(self.engine.task_dir(self.task.task_id))

        path.unlink()
        valid = ImplementationEquivalenceAttestation(
            task_id=self.task.task_id,
            task_content_hash=authoritative.task_content_hash,
            authoritative_evidence_digest=authoritative.evidence_digest(),
            adapter_transition=transition,
            reproduced_provenance=reproduced,
        )
        wrong_name = path.parent / (
            f"{authoritative.task_content_hash}.{'c' * 64}."
            "implementation-equivalence.json"
        )
        wrong_name.write_bytes(valid.deterministic_bytes())
        with self.assertRaisesRegex(ValueError, "filename/evidence mismatch"):
            load_current(self.engine.task_dir(self.task.task_id))


class SourceDigestTest(unittest.TestCase):
    @staticmethod
    def selection_inputs(origin: Origin) -> dict[str, ProvenanceArtifact]:
        values = {
            "ingest_manifest": ProvenanceArtifact(
                locator="manifest-schema:five-source-ingest-v1",
                digest="3" * 64,
                digest_kind="sha256-canonical-json-v1",
            ),
            "source_catalog": ProvenanceArtifact(
                locator="manifest:catalog",
                digest="4" * 64,
                digest_kind="sha256-file",
            ),
        }
        if origin is Origin.SCHEMAPILE:
            values["schemapile_index"] = ProvenanceArtifact(
                locator="entry:index",
                digest="5" * 64,
                digest_kind="sha256-file",
            )
        if origin is Origin.WIKIDBS:
            values["wikidbs_family_map"] = ProvenanceArtifact(
                locator="entry:family-map",
                digest="6" * 64,
                digest_kind="sha256-file",
            )
            values["wikidbs_node_inventory"] = ProvenanceArtifact(
                locator="derived:wikidbs-node-inventory-v1",
                digest="7" * 64,
                digest_kind="sha256-canonical-json-v1",
            )
        return values

    def test_source_identity_supports_exact_selectors_for_all_five_pools(self) -> None:
        selectors = {
            Origin.DBT: "dbt_twitter:model.twitter__tweets",
            Origin.DLT: "workable",
            Origin.SYNSQL: "fisheries_data_and_management",
            Origin.SCHEMAPILE: "github.com/example/schema.sql",
            Origin.WIKIDBS: "part-0/00012 Example Database",
        }
        for origin, selector in selectors.items():
            with self.subTest(origin=origin.value):
                identity = SourceIdentity(
                    pool=origin.value,
                    origin=origin,
                    selector=selector,
                    upstream_url=f"https://example.invalid/{origin.value}",
                    upstream_revision="record-123",
                    source_digest="1" * 64,
                    source_digest_kind="sha256-file",
                    adapter_name=f"elt_taskgen.adapters.{origin.value}",
                    adapter_version="1.0.0",
                    adapter_digest="2" * 64,
                    license="test-license",
                    license_evidence="selected-record:license",
                    selection_inputs=self.selection_inputs(origin),
                )
                self.assertEqual(identity.selector, selector)

    def test_source_identity_rejects_floating_or_host_local_identity(self) -> None:
        base = {
            "pool": "synsql",
            "origin": Origin.SYNSQL,
            "selector": "database_id",
            "upstream_url": "https://example.invalid/synsql",
            "upstream_revision": "record-123",
            "source_digest": "1" * 64,
            "source_digest_kind": "sha256-file",
            "adapter_name": "elt_taskgen.adapters.synsql",
            "adapter_version": "1.0.0",
            "adapter_digest": "2" * 64,
            "license": "Apache-2.0",
            "license_evidence": "repository:LICENSE",
            "selection_inputs": self.selection_inputs(Origin.SYNSQL),
        }
        for updates in (
            {"upstream_revision": "latest"},
            {"selector": "/Users/operator/source"},
            {"upstream_url": "/Users/operator/source"},
            {"license_evidence": r"C:\source\LICENSE"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                SourceIdentity(**(base | updates))

    def test_pool_specific_selection_inputs_are_required(self) -> None:
        common = self.selection_inputs(Origin.SYNSQL)
        base = {
            "selector": "selected-record",
            "upstream_url": "https://example.invalid/source",
            "upstream_revision": "record-123",
            "source_digest": "1" * 64,
            "source_digest_kind": "sha256-file",
            "adapter_version": "1.0.0",
            "adapter_digest": "2" * 64,
            "license": "test-license",
            "license_evidence": "selected-record:license",
            "selection_inputs": common,
        }
        for origin, missing_role in (
            (Origin.SCHEMAPILE, "schemapile_index"),
            (Origin.WIKIDBS, "wikidbs_family_map"),
        ):
            with (
                self.subTest(origin=origin.value),
                self.assertRaisesRegex(ValueError, missing_role),
            ):
                SourceIdentity(
                    pool=origin.value,
                    origin=origin,
                    adapter_name=f"elt_taskgen.adapters.{origin.value}",
                    **base,
                )
        wiki_without_inventory = self.selection_inputs(Origin.WIKIDBS)
        wiki_without_inventory.pop("wikidbs_node_inventory")
        with self.assertRaisesRegex(ValueError, "wikidbs_node_inventory"):
            SourceIdentity(
                pool="wikidbs",
                origin=Origin.WIKIDBS,
                adapter_name="elt_taskgen.adapters.wikidbs",
                **(base | {"selection_inputs": wiki_without_inventory}),
            )

    def test_file_json_and_tree_digests_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.txt").write_bytes(b"b")
            (root / "nested").mkdir()
            (root / "nested" / "a.txt").write_bytes(b"a")
            first = sha256_tree(root)
            second = sha256_tree(root)
            self.assertEqual(first, second)
            self.assertEqual(
                sha256_file(root / "b.txt"), hashlib.sha256(b"b").hexdigest()
            )
            self.assertEqual(
                sha256_canonical_json({"b": 1, "a": 2}),
                sha256_canonical_json({"a": 2, "b": 1}),
            )

    def test_tree_digest_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target").write_text("source", encoding="utf-8")
            (root / "link").symlink_to(root / "target")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                sha256_tree(root)


if __name__ == "__main__":
    unittest.main()

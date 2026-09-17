"""Typed five-source ingest manifest and pipeline hand-off tests."""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import __version__
from elt_taskgen import cli as cli_mod
from elt_taskgen import ingest_manifest as ingest_mod
from elt_taskgen import provenance as provenance_mod
from elt_taskgen.adapters import schemapile as schemapile_adapter
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import Engine
from elt_taskgen.models import (
    Origin,
    PopulationName,
    RepairRoute,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.provenance import (
    IngestProvenance,
    load_current,
    publish_or_confirm,
    sha256_file,
    sha256_tree,
)


class FiveSourceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.workspace = self.root / "workspace"

        pool_roots: dict[str, Path] = {}
        for origin in ingest_mod.FIVE_ORIGINS:
            pool_root = self.root / f"pool-{origin.value}"
            pool_root.mkdir()
            pool_roots[origin.value] = pool_root

        catalog_doc = {
            "pools": {
                origin.value: {
                    "origin": origin.value,
                    "root": str(pool_roots[origin.value]),
                    **(
                        {"license_per_record": True}
                        if origin is Origin.SCHEMAPILE
                        else {"license": "Test-1.0"}
                    ),
                    "attribution": f"test {origin.value}",
                }
                for origin in ingest_mod.FIVE_ORIGINS
            }
        }
        self.catalog = self.inputs / "sources.yaml"
        self.catalog.write_text(json.dumps(catalog_doc), encoding="utf-8")

        self.dbt_manifest = self._file("dbt-manifest.json", "{}")
        self.dlt_manifest = self._file("dlt-manifest.yaml", "connector: fixture\n")
        self.synsql_tables = self._file("tables.json", "[]")
        self.schemapile_source = self._file("schemapile.json", "{}")
        self.schemapile_index = self._file("schemapile-index.json", "{}")
        self.wikidbs_root = pool_roots[Origin.WIKIDBS.value]
        for part_index in range(5):
            (self.wikidbs_root / f"part-{part_index}").mkdir()
        self.wikidbs_database = (
            self.wikidbs_root / "part-0" / "00001 TEST_DATABASE"
        )
        self.wikidbs_database.mkdir()
        (self.wikidbs_database / "schema.json").write_text("{}", encoding="utf-8")
        self.wikidbs_inventory = ingest_mod.wikidbs_node_inventory_sha256(
            self.wikidbs_root
        )
        self.family_map = self._file("wikidbs_family_map.csv.gz", "map")
        self.manifest = self.root / "five.yaml"
        self._write_manifest()

    def _file(self, relative: str, text: str) -> Path:
        path = self.inputs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def _pin(path: Path, *, tree: bool = False) -> dict[str, str]:
        return {
            "path": str(path),
            "digest_kind": "sha256-tree-v1" if tree else "sha256-file",
            "sha256": sha256_tree(path) if tree else sha256_file(path),
        }

    @staticmethod
    def _adapter_pin(origin: Origin) -> tuple[str, str]:
        module = ingest_mod._adapter_module(
            {
                Origin.DBT: ingest_mod.DbtIngest,
                Origin.DLT: ingest_mod.DltIngest,
                Origin.SYNSQL: ingest_mod.SynSQLIngest,
                Origin.SCHEMAPILE: ingest_mod.SchemaPileIngest,
                Origin.WIKIDBS: ingest_mod.WikiDBsIngest,
            }[origin].model_construct(pool=origin.value)
        )
        assert module.__file__ is not None
        return __version__, sha256_file(Path(module.__file__))

    def _common(self, origin: Origin) -> dict:
        version, digest = self._adapter_pin(origin)
        return {
            "pool": origin.value,
            "selector": {
                Origin.DBT: "dbt_fixture",
                Origin.DLT: "dlt_fixture",
                Origin.SYNSQL: "fixture_db",
                Origin.SCHEMAPILE: "fixture.sql",
                Origin.WIKIDBS: "00001 TEST_DATABASE",
            }[origin],
            "expected_task_id": f"{origin.value}__fixture",
            "upstream_url": f"https://example.test/{origin.value}",
            "upstream_revision": "revision-1",
            "adapter_version": version,
            "adapter_digest": digest,
            "license": "Test-1.0",
            "license_evidence": "fixture:LICENSE",
        }

    def _manifest_doc(self) -> dict:
        return {
            "schema_version": ingest_mod.FIVE_SOURCE_MANIFEST_SCHEMA_VERSION,
            "catalog": self._pin(self.catalog),
            "sources": {
                "dbt": {
                    **self._common(Origin.DBT),
                    "family": "fixture",
                    "manifest": self._pin(self.dbt_manifest),
                },
                "dlt": {
                    **self._common(Origin.DLT),
                    "connector": "fixture",
                    "manifest": self._pin(self.dlt_manifest),
                },
                "synsql": {
                    **self._common(Origin.SYNSQL),
                    "tables": self._pin(self.synsql_tables),
                },
                "schemapile": {
                    **self._common(Origin.SCHEMAPILE),
                    "source": self._pin(self.schemapile_source),
                    "index": self._pin(self.schemapile_index),
                },
                "wikidbs": {
                    **self._common(Origin.WIKIDBS),
                    "database": self._pin(self.wikidbs_database, tree=True),
                    "family_map": self._pin(self.family_map),
                    "verify_nodes": True,
                    "node_inventory_sha256": self.wikidbs_inventory,
                },
            },
        }

    def _write_manifest(self, doc: dict | None = None) -> None:
        self.manifest.write_text(
            json.dumps(doc if doc is not None else self._manifest_doc()),
            encoding="utf-8",
        )

    def _batch_manifest_doc(
        self, counts: dict[Origin, int] | None = None
    ) -> dict:
        requested = counts or {origin: 10 for origin in ingest_mod.FIVE_ORIGINS}
        singleton = self._manifest_doc()
        sources: dict[str, list[dict]] = {}
        for origin in ingest_mod.FIVE_ORIGINS:
            entries: list[dict] = []
            for index in range(requested[origin]):
                entry = json.loads(json.dumps(singleton["sources"][origin.value]))
                entry["expected_task_id"] = f"{origin.value}__batch_{index:02d}"
                entry["selector"] = f"{origin.value}_selector_{index:02d}"
                if origin is Origin.DBT:
                    entry["family"] = f"fixture_{index:02d}"
                elif origin is Origin.DLT:
                    entry["connector"] = f"fixture_{index:02d}"
                    entry["require_embedded_provenance"] = True
                entries.append(entry)
            sources[origin.value] = entries
        return {
            "schema_version": ingest_mod.FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION,
            "expected_task_count": sum(requested.values()),
            "require_unique_independence_units": True,
            "generator": ingest_mod.current_generator_pin().model_dump(mode="json"),
            "catalog": singleton["catalog"],
            "sources": sources,
        }

    @staticmethod
    def _task(entry, _paths, _catalog):
        origin = Origin(entry.pool)
        return demo_task().model_copy(
            update={
                "task_id": entry.expected_task_id,
                "family_id": f"{origin.value}__fixture",
                "cluster_id": f"{origin.value}__fixture",
                "origin": origin,
                "license": entry.license,
                "attribution": f"fixture {origin.value}",
            }
        )

    @staticmethod
    def _batch_task(entry, _paths, _catalog):
        origin = Origin(entry.pool)
        suffix = entry.expected_task_id.rsplit("__", 1)[-1]
        return demo_task().model_copy(
            update={
                "task_id": entry.expected_task_id,
                "family_id": f"{origin.value}__family_{suffix}",
                "cluster_id": f"{origin.value}__cluster_{suffix}",
                "origin": origin,
                "license": entry.license,
                "attribution": f"fixture {origin.value}",
            }
        )

    def _builders(self):
        stack = contextlib.ExitStack()
        for name in (
            "_build_dbt_task",
            "_build_dlt_task",
            "_build_synsql_task",
            "_build_schemapile_task",
            "_build_wikidbs_task",
        ):
            stack.enter_context(mock.patch.object(ingest_mod, name, side_effect=self._task))
        return stack

    def _batch_builders(self):
        stack = contextlib.ExitStack()
        for name in (
            "_build_dbt_task",
            "_build_dlt_task",
            "_build_synsql_task",
            "_build_schemapile_task",
            "_build_wikidbs_task",
        ):
            stack.enter_context(
                mock.patch.object(ingest_mod, name, side_effect=self._batch_task)
            )
        return stack

    def test_v2_manifest_is_exact_sorted_nonempty_and_unique(self) -> None:
        batch = self._batch_manifest_doc()
        self._write_manifest(batch)
        loaded = ingest_mod.load_five_source_batch_manifest(self.manifest)
        self.assertIsInstance(loaded, ingest_mod.FiveSourceBatchIngestManifest)
        self.assertEqual(len(loaded.ordered_entries()), 50)
        self.assertEqual(
            tuple(entry.pool for entry in loaded.ordered_entries()),
            tuple(origin.value for origin in ingest_mod.FIVE_ORIGINS for _ in range(10)),
        )

        invalid_docs: list[tuple[str, dict, str]] = []
        wrong_count = self._batch_manifest_doc()
        wrong_count["expected_task_count"] = 49
        invalid_docs.append(("count", wrong_count, "expected_task_count"))
        empty_pool = self._batch_manifest_doc()
        empty_pool["sources"]["dlt"] = []
        empty_pool["expected_task_count"] = 40
        invalid_docs.append(("empty", empty_pool, "at least 1"))
        unsorted = self._batch_manifest_doc()
        unsorted["sources"]["synsql"].reverse()
        invalid_docs.append(("order", unsorted, "sorted by expected_task_id"))
        duplicate = self._batch_manifest_doc()
        duplicate["sources"]["wikidbs"][0]["expected_task_id"] = duplicate[
            "sources"
        ]["dbt"][0]["expected_task_id"]
        invalid_docs.append(("duplicate", duplicate, "duplicate expected_task_id"))
        for label, document, message in invalid_docs:
            with self.subTest(label=label):
                self._write_manifest(document)
                with self.assertRaisesRegex(
                    ingest_mod.FiveSourceIngestError, message
                ):
                    ingest_mod.load_five_source_manifest(self.manifest)

        self._write_manifest(self._manifest_doc())
        with self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError, "batch ingestion requires"
        ):
            ingest_mod.load_five_source_batch_manifest(self.manifest)

    def _implementation_manifest_pair(self):
        counts = {origin: 1 for origin in ingest_mod.FIVE_ORIGINS}
        authoritative_doc = self._batch_manifest_doc(counts)
        current_generator = ingest_mod.current_generator_pin()
        authoritative_doc["generator"]["sha256"] = (
            "0" * 64 if current_generator.sha256 != "0" * 64 else "1" * 64
        )
        for index, origin in enumerate((Origin.DLT, Origin.SCHEMAPILE), start=1):
            current_digest = authoritative_doc["sources"][origin.value][0][
                "adapter_digest"
            ]
            stale_digest = str(index) * 64
            if stale_digest == current_digest:
                stale_digest = str(index + 5) * 64
            authoritative_doc["sources"][origin.value][0][
                "adapter_digest"
            ] = stale_digest
        authoritative = ingest_mod.FiveSourceBatchIngestManifest.model_validate(
            authoritative_doc
        )
        reproduced = ingest_mod.repin_current_implementation(authoritative)
        return authoritative, reproduced

    def test_current_implementation_repin_is_exact_for_all_five_sources(self) -> None:
        authoritative, reproduced = self._implementation_manifest_pair()

        self.assertEqual(reproduced.generator, ingest_mod.current_generator_pin())
        self.assertNotEqual(
            authoritative.manifest_sha256(), reproduced.manifest_sha256()
        )
        for old_entry, new_entry in zip(
            authoritative.ordered_entries(),
            reproduced.ordered_entries(),
            strict=True,
        ):
            old_payload = old_entry.model_dump(mode="json")
            new_payload = new_entry.model_dump(mode="json")
            old_payload.pop("adapter_version")
            old_payload.pop("adapter_digest")
            new_payload.pop("adapter_version")
            new_payload.pop("adapter_digest")
            self.assertEqual(old_payload, new_payload)

            _name, expected_version, expected_digest = (
                ingest_mod._current_adapter_identity(new_entry)
            )
            self.assertEqual(new_entry.adapter_version, expected_version)
            self.assertEqual(new_entry.adapter_digest, expected_digest)
            transition = ingest_mod.implementation_adapter_transition(
                authoritative,
                reproduced,
                task_id=old_entry.expected_task_id,
            )
            if Origin(old_entry.pool) in (Origin.DLT, Origin.SCHEMAPILE):
                assert transition is not None
                self.assertIs(transition.pool, Origin(old_entry.pool))
                self.assertEqual(
                    transition.authoritative_version,
                    old_entry.adapter_version,
                )
                self.assertEqual(
                    transition.reproduced_version,
                    new_entry.adapter_version,
                )
                self.assertEqual(
                    transition.authoritative_source_entry_sha256,
                    authoritative.source_entry_sha256(old_entry),
                )
                self.assertEqual(
                    transition.reproduced_source_entry_sha256,
                    reproduced.source_entry_sha256(new_entry),
                )
            else:
                self.assertIsNone(transition)

        selected = ingest_mod.select_candidate_manifest(
            authoritative, candidate_count=5, seed=17
        )
        with self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError, "requires the new parent pool digest"
        ):
            ingest_mod.repin_current_implementation(selected)
        repinned_selected = ingest_mod.repin_current_implementation(
            selected,
            parent_manifest_sha256=reproduced.manifest_sha256(),
        )
        self.assertIsInstance(
            repinned_selected, ingest_mod.SelectedSourceIngestManifest
        )
        self.assertEqual(
            repinned_selected.parent_manifest_sha256,
            reproduced.manifest_sha256(),
        )
        self.assertEqual(
            tuple(entry.expected_task_id for entry in selected.ordered_entries()),
            tuple(
                entry.expected_task_id
                for entry in repinned_selected.ordered_entries()
            ),
        )

        version_doc = self._batch_manifest_doc(
            {origin: 1 for origin in ingest_mod.FIVE_ORIGINS}
        )
        version_doc["generator"]["version"] = "0.0.0-prior"
        version_doc["generator"]["sha256"] = "f" * 64
        for origin in ingest_mod.FIVE_ORIGINS:
            version_doc["sources"][origin.value][0]["adapter_version"] = (
                "0.0.0-prior"
            )
        version_authoritative = (
            ingest_mod.FiveSourceBatchIngestManifest.model_validate(version_doc)
        )
        version_reproduced = ingest_mod.repin_current_implementation(
            version_authoritative
        )
        version_transition = ingest_mod.implementation_adapter_transition(
            version_authoritative,
            version_reproduced,
            task_id="dbt__batch_00",
        )
        assert version_transition is not None
        self.assertEqual(
            version_transition.authoritative_version, "0.0.0-prior"
        )
        self.assertEqual(version_transition.reproduced_version, __version__)

    def test_implementation_authorization_is_typed_and_union_loaded(self) -> None:
        authoritative, reproduced = self._implementation_manifest_pair()
        task_id = "dlt__batch_00"
        transition = ingest_mod.implementation_adapter_transition(
            authoritative, reproduced, task_id=task_id
        )
        assert transition is not None
        budget: dict[str, object] = {}
        budget_sha256 = sha256_hex(canonical_json(budget))
        binding = ingest_mod.ImplementationEquivalenceTaskBinding(
            task_id=task_id,
            origin=Origin.DLT,
            authoritative_source_entry_sha256=(
                transition.authoritative_source_entry_sha256
            ),
            reproduced_source_entry_sha256=(
                transition.reproduced_source_entry_sha256
            ),
            intake_content_hash="3" * 64,
            authoritative_provenance_sha256="4" * 64,
            reproduced_provenance_sha256="5" * 64,
            adapter_transition=transition,
        )
        authorization = ingest_mod.ImplementationEquivalenceAuthorization(
            run_id="implementation_test",
            config_sha256="6" * 64,
            prior_readiness_sha256="7" * 64,
            authoritative_selected_sha256="8" * 64,
            reproduced_selected_sha256="9" * 64,
            authoritative_pool_sha256="a" * 64,
            reproduced_pool_sha256="b" * 64,
            authoritative_generator_sha256=authoritative.generator.sha256,
            reproduced_generator_sha256=reproduced.generator.sha256,
            ordered_task_ids=(task_id,),
            tasks=(binding,),
            budget_snapshot_sha256=budget_sha256,
            budget_snapshot=budget,
        )
        store = (
            self.workspace
            / "state"
            / "pipeline_runs"
            / authorization.run_id
            / "identity_migrations"
            / "authorizations"
        )
        store.mkdir(parents=True)
        path = store / f"{authorization.evidence_digest()}.json"
        path.write_bytes(authorization.deterministic_bytes())
        loaded = ingest_mod.load_generator_equivalence_authorization(
            path, workspace=self.workspace
        )
        self.assertIsInstance(
            loaded, ingest_mod.ImplementationEquivalenceAuthorization
        )
        self.assertEqual(loaded, authorization)
        self.assertEqual(
            ingest_mod.load_implementation_equivalence_authorization(
                path, workspace=self.workspace
            ),
            authorization,
        )

        v1_binding = ingest_mod.GeneratorEquivalenceTaskBinding(
            task_id="dbt__batch_00",
            source_entry_sha256="c" * 64,
            intake_content_hash="d" * 64,
            authoritative_provenance_sha256="e" * 64,
            reproduced_provenance_sha256="f" * 64,
        )
        v1 = ingest_mod.GeneratorEquivalenceAuthorization(
            run_id="generator_test",
            config_sha256="1" * 64,
            prior_readiness_sha256="2" * 64,
            authoritative_selected_sha256="3" * 64,
            reproduced_selected_sha256="4" * 64,
            authoritative_pool_sha256="5" * 64,
            reproduced_pool_sha256="6" * 64,
            authoritative_generator_sha256="7" * 64,
            reproduced_generator_sha256="8" * 64,
            ordered_task_ids=(v1_binding.task_id,),
            tasks=(v1_binding,),
            budget_snapshot_sha256=budget_sha256,
            budget_snapshot=budget,
        )
        v1_store = (
            self.workspace
            / "state"
            / "pipeline_runs"
            / v1.run_id
            / "identity_migrations"
            / "authorizations"
        )
        v1_store.mkdir(parents=True)
        v1_path = v1_store / f"{v1.evidence_digest()}.json"
        v1_path.write_bytes(v1.deterministic_bytes())
        v1_loaded = ingest_mod.load_generator_equivalence_authorization(
            v1_path, workspace=self.workspace
        )
        self.assertIsInstance(
            v1_loaded, ingest_mod.GeneratorEquivalenceAuthorization
        )
        self.assertEqual(v1_loaded.deterministic_bytes(), v1.deterministic_bytes())

        invalid_request = authorization.model_dump(mode="json")
        invalid_request["report_revalidation_request_path"] = (
            "state/pipeline_runs/implementation_test/request.json"
        )
        with self.assertRaisesRegex(
            ValueError, "does not support report revalidation requests"
        ):
            ingest_mod.ImplementationEquivalenceAuthorization.model_validate(
                invalid_request
            )

        moved_without_transition = binding.model_dump(mode="json")
        moved_without_transition["adapter_transition"] = None
        with self.assertRaisesRegex(
            ValueError, "without an adapter transition"
        ):
            ingest_mod.ImplementationEquivalenceTaskBinding.model_validate(
                moved_without_transition
            )

    def test_implementation_reingest_keeps_authoritative_provenance_and_attests(
        self,
    ) -> None:
        authoritative_pool, reproduced_pool = self._implementation_manifest_pair()
        authoritative = ingest_mod.select_candidate_manifest(
            authoritative_pool, candidate_count=5, seed=23
        )
        reproduced = ingest_mod.repin_current_implementation(
            authoritative,
            parent_manifest_sha256=reproduced_pool.manifest_sha256(),
        )
        assert isinstance(reproduced, ingest_mod.SelectedSourceIngestManifest)
        with self._batch_builders():
            prepared = ingest_mod.prepare_five_sources(
                reproduced,
                manifest_path=self.manifest,
                workspace=self.workspace,
            )

        old_entries = {
            entry.expected_task_id: entry
            for entry in authoritative.ordered_entries()
        }
        transitions = {
            item.task.task_id: ingest_mod.implementation_adapter_transition(
                authoritative,
                reproduced,
                task_id=item.task.task_id,
            )
            for item in prepared
        }
        engine = Engine(self.workspace)
        authoritative_provenance: dict[str, IngestProvenance] = {}
        try:
            for item in prepared:
                old_entry = old_entries[item.task.task_id]
                selection_inputs = dict(item.provenance.source.selection_inputs)
                selection_inputs["generator_code"] = selection_inputs[
                    "generator_code"
                ].model_copy(
                    update={"digest": authoritative.generator.sha256}
                )
                selection_inputs["ingest_manifest"] = selection_inputs[
                    "ingest_manifest"
                ].model_copy(
                    update={
                        "digest": authoritative.source_entry_sha256(old_entry)
                    }
                )
                old_source = item.provenance.source.model_copy(
                    update={
                        "adapter_version": old_entry.adapter_version,
                        "adapter_digest": old_entry.adapter_digest,
                        "selection_inputs": selection_inputs,
                    }
                )
                old_provenance = item.provenance.model_copy(
                    update={"source": old_source}
                )
                authoritative_provenance[item.task.task_id] = old_provenance
                engine.register(item.task)
                publish_or_confirm(
                    engine.task_dir(item.task.task_id), old_provenance
                )
        finally:
            engine.close()

        bindings = tuple(
            ingest_mod.ImplementationEquivalenceTaskBinding(
                task_id=item.task.task_id,
                origin=item.origin,
                authoritative_source_entry_sha256=(
                    authoritative.source_entry_sha256(
                        old_entries[item.task.task_id]
                    )
                ),
                reproduced_source_entry_sha256=item.source_entry_sha256,
                intake_content_hash=item.task.content_hash(),
                authoritative_provenance_sha256=(
                    authoritative_provenance[
                        item.task.task_id
                    ].evidence_digest()
                ),
                reproduced_provenance_sha256=item.provenance.evidence_digest(),
                adapter_transition=transitions[item.task.task_id],
            )
            for item in prepared
        )
        budget: dict[str, object] = {}
        authorization = ingest_mod.ImplementationEquivalenceAuthorization(
            run_id="implementation_reingest",
            config_sha256="1" * 64,
            prior_readiness_sha256="2" * 64,
            authoritative_selected_sha256=authoritative.manifest_sha256(),
            reproduced_selected_sha256=reproduced.manifest_sha256(),
            authoritative_pool_sha256=authoritative_pool.manifest_sha256(),
            reproduced_pool_sha256=reproduced_pool.manifest_sha256(),
            authoritative_generator_sha256=authoritative.generator.sha256,
            reproduced_generator_sha256=reproduced.generator.sha256,
            ordered_task_ids=tuple(item.task.task_id for item in prepared),
            tasks=bindings,
            budget_snapshot_sha256=sha256_hex(canonical_json(budget)),
            budget_snapshot=budget,
        )
        authorization_store = (
            self.workspace
            / "state"
            / "pipeline_runs"
            / authorization.run_id
            / "identity_migrations"
            / "authorizations"
        )
        authorization_store.mkdir(parents=True)
        authorization_path = (
            authorization_store / f"{authorization.evidence_digest()}.json"
        )
        authorization_path.write_bytes(authorization.deterministic_bytes())

        with self._batch_builders(), mock.patch.object(
            ingest_mod,
            "_assert_receipt_compatible",
            wraps=ingest_mod._assert_receipt_compatible,
        ) as receipt_check:
            result = ingest_mod._ingest_loaded_manifest(
                reproduced,
                manifest_path=self.manifest,
                workspace=self.workspace,
                reingest=True,
                dry_run=False,
                equivalence_authorization=authorization,
                equivalence_authorization_path=authorization_path,
            )
        receipt_check.assert_not_called()
        self.assertIsNone(result.receipt)
        for item in prepared:
            task_dir = self.workspace / "tasks" / item.task.task_id
            self.assertEqual(
                load_current(task_dir),
                authoritative_provenance[item.task.task_id],
            )
            sidecars = tuple(
                (task_dir / provenance_mod.INGEST_PROVENANCE_DIRNAME).glob(
                    "*.implementation-equivalence.json"
                )
            )
            self.assertEqual(len(sidecars), 1)
            attestation = (
                provenance_mod.ImplementationEquivalenceAttestation.model_validate_json(
                    sidecars[0].read_bytes()
                )
            )
            self.assertEqual(
                attestation.adapter_transition,
                transitions[item.task.task_id],
            )

    def test_selected_roster_supports_small_and_arbitrary_balanced_counts(self) -> None:
        pool = ingest_mod.FiveSourceBatchIngestManifest.model_validate(
            self._batch_manifest_doc()
        )

        for count in (1, 2, 7):
            with self.subTest(count=count):
                selected = ingest_mod.select_candidate_manifest(
                    pool, candidate_count=count, seed=42
                )
                self.assertEqual(selected.expected_task_count, count)
                self.assertEqual(len(selected.ordered_entries()), count)
                self.assertEqual(sum(selected.source_allocation.values()), count)
                allocations = [
                    selected.source_allocation[origin]
                    for origin in ingest_mod.FIVE_ORIGINS
                ]
                self.assertLessEqual(max(allocations) - min(allocations), 1)
                for origin in ingest_mod.FIVE_ORIGINS:
                    task_ids = tuple(
                        entry.expected_task_id
                        for entry in getattr(selected.sources, origin.value)
                    )
                    self.assertEqual(task_ids, tuple(sorted(task_ids)))

        first = ingest_mod.select_candidate_manifest(
            pool, candidate_count=7, seed=-112358
        )
        replay = ingest_mod.select_candidate_manifest(
            pool, candidate_count=7, seed=-112358
        )
        self.assertEqual(first, replay)
        self.assertEqual(first.manifest_sha256(), replay.manifest_sha256())

    def test_selected_roster_supports_subsets_and_exact_uneven_allocation(self) -> None:
        pool = ingest_mod.FiveSourceBatchIngestManifest.model_validate(
            self._batch_manifest_doc()
        )
        subset = ingest_mod.select_candidate_manifest(
            pool,
            candidate_count=3,
            source_families=(Origin.DLT, Origin.WIKIDBS),
            seed=9,
        )
        self.assertEqual(
            {
                origin: subset.source_allocation[origin]
                for origin in ingest_mod.FIVE_ORIGINS
            },
            {
                Origin.DBT: 0,
                Origin.DLT: 1,
                Origin.SYNSQL: 0,
                Origin.SCHEMAPILE: 0,
                Origin.WIKIDBS: 2,
            },
        )

        allocation = {
            Origin.DBT: 1,
            Origin.DLT: 0,
            Origin.SYNSQL: 4,
            Origin.SCHEMAPILE: 2,
            Origin.WIKIDBS: 0,
        }
        uneven = ingest_mod.select_candidate_manifest(
            pool,
            candidate_count=7,
            source_allocation=allocation,
            seed=101,
        )
        self.assertEqual(uneven.source_allocation, allocation)
        self.assertEqual(
            tuple(len(getattr(uneven.sources, origin.value)) for origin in ingest_mod.FIVE_ORIGINS),
            (1, 0, 4, 2, 0),
        )

    def test_selected_roster_round_trip_and_fail_closed_validation(self) -> None:
        pool = ingest_mod.FiveSourceBatchIngestManifest.model_validate(
            self._batch_manifest_doc()
        )
        selected = ingest_mod.select_candidate_manifest(
            pool,
            candidate_count=7,
            source_families=("dbt", "synsql", "schemapile"),
            source_allocation={"dbt": 1, "synsql": 4, "schemapile": 2},
            seed=101,
        )
        self._write_manifest(selected.model_dump(mode="json"))
        loaded = ingest_mod.load_five_source_manifest(self.manifest)
        self.assertIsInstance(loaded, ingest_mod.SelectedSourceIngestManifest)
        self.assertEqual(loaded, selected)
        self.assertEqual(loaded.manifest_sha256(), selected.manifest_sha256())
        self.assertEqual(loaded.parent_manifest_sha256, pool.manifest_sha256())

        invalid_calls = (
            ({"candidate_count": 0}, "positive integer"),
            ({"candidate_count": True}, "positive integer"),
            (
                {
                    "candidate_count": 1,
                    "source_families": ("dbt", "dbt"),
                },
                "repeated",
            ),
            (
                {"candidate_count": 1, "source_families": ("unknown",)},
                "unknown source family",
            ),
            (
                {
                    "candidate_count": 11,
                    "source_families": ("dbt",),
                },
                "capacity 10",
            ),
            (
                {
                    "candidate_count": 2,
                    "source_families": ("dbt",),
                    "source_allocation": {"dbt": 1},
                },
                "sum exactly",
            ),
            (
                {
                    "candidate_count": 1,
                    "source_families": ("dbt",),
                    "source_allocation": {"dlt": 1},
                },
                "not in source_families",
            ),
            (
                {
                    "candidate_count": 1,
                    "source_families": ("dbt",),
                    "source_allocation": {"dbt": True},
                },
                "must be an integer",
            ),
        )
        for kwargs, message in invalid_calls:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(
                    ingest_mod.FiveSourceIngestError, message
                ):
                    ingest_mod.select_candidate_manifest(pool, **kwargs)

    def test_selected_ingest_retains_siblings_after_one_adapter_failure(self) -> None:
        counts = {origin: 1 for origin in ingest_mod.FIVE_ORIGINS}
        self._write_manifest(self._batch_manifest_doc(counts))
        failed_id = "dlt__batch_00"

        def flaky(entry, paths, catalog):
            if entry.expected_task_id == failed_id:
                raise ingest_mod.FiveSourceIngestError("synthetic adapter refusal")
            return self._batch_task(entry, paths, catalog)

        with contextlib.ExitStack() as stack:
            for name in (
                "_build_dbt_task",
                "_build_dlt_task",
                "_build_synsql_task",
                "_build_schemapile_task",
                "_build_wikidbs_task",
            ):
                stack.enter_context(
                    mock.patch.object(ingest_mod, name, side_effect=flaky)
                )
            selected, first = ingest_mod.ingest_selected_sources(
                self.manifest,
                workspace=self.workspace,
                candidate_count=5,
                seed=4,
                continue_on_candidate_error=True,
            )

        self.assertEqual(len(selected.ordered_entries()), 5)
        self.assertEqual(len(first.tasks), 4)
        self.assertIsNone(first.receipt)
        by_id = {outcome.task_id: outcome for outcome in first.candidate_outcomes}
        self.assertEqual(by_id[failed_id].state, "failed")
        self.assertIn("synthetic adapter refusal", by_id[failed_id].detail)
        self.assertTrue(
            all(
                (self.workspace / "tasks" / task.task_id / "task_ir.json").is_file()
                for task in first.tasks
            )
        )
        selected_path = (
            self.workspace
            / "state"
            / "candidate_manifests"
            / f"{selected.manifest_sha256()}.json"
        )
        self.assertTrue(selected_path.is_file())

        intake_hashes = {task.task_id: task.content_hash() for task in first.tasks}
        advanced_id = next(task_id for task_id in intake_hashes if task_id != failed_id)
        engine = Engine(self.workspace)
        try:
            advanced = engine.load_task(advanced_id).model_copy(
                update={"solver_prompt": "A later semantic-author draft."}
            )
            advanced = advanced.with_revision(
                route=RepairRoute.SPECIFICATION,
                reason="simulate a sibling advancing before ingest resume",
            )
            engine.save_task(advanced)
        finally:
            engine.close()

        with self._batch_builders():
            replay_selected, replay = ingest_mod.ingest_selected_sources(
                self.manifest,
                workspace=self.workspace,
                candidate_count=5,
                seed=4,
                continue_on_candidate_error=True,
            )
        self.assertEqual(replay_selected, selected)
        self.assertEqual(len(replay.tasks), 5)
        self.assertIsNotNone(replay.receipt)
        self.assertTrue(replay.receipt.is_file())
        replay_states = {
            outcome.task_id: outcome.state for outcome in replay.candidate_outcomes
        }
        self.assertEqual(replay_states[failed_id], "created")
        self.assertEqual(
            sum(state == "resumed" for state in replay_states.values()), 4
        )
        receipt = ingest_mod.SelectedSourceIngestReceipt.model_validate_json(
            replay.receipt.read_text(encoding="utf-8")
        )
        by_receipt_id = {entry.task_id: entry for entry in receipt.tasks}
        self.assertEqual(
            by_receipt_id[advanced_id].task_content_hash,
            intake_hashes[advanced_id],
        )

    def test_v2_requires_strict_generator_and_dlt_contracts(self) -> None:
        invalid_docs: list[tuple[str, dict, str]] = []

        missing_generator = self._batch_manifest_doc()
        del missing_generator["generator"]
        invalid_docs.append(("missing generator", missing_generator, "generator"))

        missing_generator_name = self._batch_manifest_doc()
        del missing_generator_name["generator"]["name"]
        invalid_docs.append(
            ("missing generator name", missing_generator_name, "name")
        )

        missing_generator_kind = self._batch_manifest_doc()
        del missing_generator_kind["generator"]["digest_kind"]
        invalid_docs.append(
            ("missing generator kind", missing_generator_kind, "digest_kind")
        )

        wrong_name = self._batch_manifest_doc()
        wrong_name["generator"]["name"] = "other-generator"
        invalid_docs.append(("generator name", wrong_name, "name"))

        wrong_kind = self._batch_manifest_doc()
        wrong_kind["generator"]["digest_kind"] = "sha256-file"
        invalid_docs.append(("generator digest kind", wrong_kind, "digest_kind"))

        extra_generator_field = self._batch_manifest_doc()
        extra_generator_field["generator"]["path"] = "/not/portable"
        invalid_docs.append(("generator extra", extra_generator_field, "path"))

        missing_dlt_requirement = self._batch_manifest_doc()
        del missing_dlt_requirement["sources"]["dlt"][0][
            "require_embedded_provenance"
        ]
        invalid_docs.append(
            (
                "dlt embedded provenance",
                missing_dlt_requirement,
                "require_embedded_provenance",
            )
        )

        for label, document, message in invalid_docs:
            with self.subTest(label=label):
                self._write_manifest(document)
                with self.assertRaisesRegex(
                    ingest_mod.FiveSourceIngestError, message
                ):
                    ingest_mod.load_five_source_batch_manifest(self.manifest)

    def test_v2_generator_mismatch_refuses_before_workspace_or_build(self) -> None:
        current = ingest_mod.current_generator_pin()
        mutations = {
            "version": "999.0.0",
            "sha256": "0" * 64 if current.sha256 != "0" * 64 else "1" * 64,
            "lock_sha256": (
                "0" * 64 if current.lock_sha256 != "0" * 64 else "1" * 64
            ),
        }
        messages = {
            "version": "generator version mismatch",
            "sha256": "generator digest mismatch",
            "lock_sha256": "dependency lock digest mismatch",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                document = self._batch_manifest_doc()
                document["generator"][field] = value
                self._write_manifest(document)
                with mock.patch.object(ingest_mod, "_builder") as builder:
                    with self.assertRaisesRegex(
                        ingest_mod.FiveSourceIngestError, messages[field]
                    ):
                        ingest_mod.ingest_five_source_batch(
                            self.manifest, workspace=self.workspace
                        )
                    builder.assert_not_called()
                self.assertFalse(self.workspace.exists())

    def test_v2_generator_pin_changes_manifest_run_identity(self) -> None:
        self._write_manifest(self._batch_manifest_doc())
        manifest = ingest_mod.load_five_source_batch_manifest(self.manifest)
        replacement_digest = (
            "0" * 64 if manifest.generator.sha256 != "0" * 64 else "1" * 64
        )
        code_changed = manifest.model_copy(
            update={
                "generator": manifest.generator.model_copy(
                    update={"sha256": replacement_digest}
                )
            }
        )
        replacement_lock = (
            "0" * 64
            if manifest.generator.lock_sha256 != "0" * 64
            else "1" * 64
        )
        lock_changed = manifest.model_copy(
            update={
                "generator": manifest.generator.model_copy(
                    update={"lock_sha256": replacement_lock}
                )
            }
        )
        self.assertNotEqual(manifest.manifest_sha256(), code_changed.manifest_sha256())
        self.assertNotEqual(manifest.manifest_sha256(), lock_changed.manifest_sha256())

    def test_current_generator_pin_uses_portable_complete_python_inventory(
        self,
    ) -> None:
        inventory = ingest_mod._generator_tree_inventory()
        paths = tuple(str(item["path"]) for item in inventory)
        self.assertEqual(paths, tuple(sorted(paths)))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(paths.count("tools/wikidbs_family_map.py"), 1)
        self.assertFalse(
            any(path.startswith("elt_taskgen/_resources/") for path in paths)
        )

        package_root = Path(ingest_mod.__file__).resolve().parent
        expected_package_paths = {
            f"elt_taskgen/{path.relative_to(package_root).as_posix()}"
            for path in package_root.rglob("*.py")
            if path.relative_to(package_root).parts[0] != "_resources"
            and path.is_file()
            and not path.is_symlink()
        }
        self.assertEqual(
            set(paths),
            expected_package_paths | {"tools/wikidbs_family_map.py"},
        )
        pin = ingest_mod.current_generator_pin()
        self.assertEqual(pin.version, __version__)
        self.assertEqual(
            pin.sha256,
            sha256_hex(canonical_json(inventory)),
        )
        self.assertEqual(
            pin.lock_sha256,
            sha256_file(ingest_mod.resource_path("uv.lock")),
        )

    def test_generator_package_inventory_refuses_symlinked_source_or_directory(
        self,
    ) -> None:
        outside_file = self._file("outside/generator.py", "VALUE = 1\n")
        outside_directory = self.root / "outside-package"
        outside_directory.mkdir()
        (outside_directory / "hidden.py").write_text("VALUE = 2\n", encoding="utf-8")

        file_package = self.root / "file-package"
        file_package.mkdir()
        (file_package / "regular.py").write_text("VALUE = 0\n", encoding="utf-8")
        (file_package / "linked.py").symlink_to(outside_file)
        with self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError,
            "generator source must be a regular non-symlink file",
        ):
            ingest_mod._package_python_inventory(file_package)

        directory_package = self.root / "directory-package"
        directory_package.mkdir()
        (directory_package / "regular.py").write_text(
            "VALUE = 0\n", encoding="utf-8"
        )
        (directory_package / "linked").symlink_to(
            outside_directory, target_is_directory=True
        )
        with self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError,
            "generator package directory must be a regular non-symlink directory",
        ):
            ingest_mod._package_python_inventory(directory_package)

    def test_v2_generator_is_rechecked_under_lock_before_task_write(self) -> None:
        self._write_manifest(self._batch_manifest_doc())
        current = ingest_mod.current_generator_pin()
        changed = current.model_copy(
            update={
                "sha256": "0" * 64 if current.sha256 != "0" * 64 else "1" * 64
            }
        )
        with (
            self._batch_builders(),
            mock.patch.object(
                ingest_mod,
                "current_generator_pin",
                side_effect=(current, changed),
            ) as calculate_pin,
            self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError, "live generator digest mismatch"
            ),
        ):
            ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(calculate_pin.call_count, 2)
        tasks = self.workspace / "tasks"
        self.assertTrue(tasks.is_dir())
        self.assertEqual(tuple(tasks.iterdir()), ())

    def test_v1_omitted_dlt_strictness_retains_legacy_manifest_identity(self) -> None:
        document = self._manifest_doc()
        self._write_manifest(document)
        manifest = ingest_mod.load_five_source_manifest(self.manifest)
        self.assertIsNone(manifest.sources.dlt.require_embedded_provenance)
        self.assertEqual(
            manifest.manifest_sha256(),
            sha256_hex(canonical_json(document)),
        )

    def test_v2_dry_run_builds_fifty_with_task_specific_provenance(self) -> None:
        document = self._batch_manifest_doc()
        self._write_manifest(document)
        with self._batch_builders():
            result = ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace, dry_run=True
            )
        self.assertEqual(len(result.tasks), 50)
        self.assertEqual(len(set(result.task_ids)), 50)
        self.assertFalse(self.workspace.exists())

        loaded = ingest_mod.load_five_source_batch_manifest(self.manifest)
        original = loaded.sources.synsql[0]
        unrelated = loaded.sources.wikidbs[0].model_copy(
            update={"upstream_revision": "revision-2"}
        )
        moved = loaded.model_copy(
            update={
                "sources": loaded.sources.model_copy(
                    update={
                        "wikidbs": (unrelated, *loaded.sources.wikidbs[1:])
                    }
                )
            }
        )
        self.assertEqual(
            loaded.source_entry_sha256(original),
            moved.source_entry_sha256(original),
        )
        syn = next(
            item for item in result.tasks if item.task_id == original.expected_task_id
        )
        self.assertEqual(syn.origin, Origin.SYNSQL)
        with self._batch_builders():
            prepared = ingest_mod.prepare_five_sources(
                loaded,
                manifest_path=self.manifest,
                workspace=self.workspace,
            )
        evidence = next(
            item.provenance
            for item in prepared
            if item.task.task_id == original.expected_task_id
        )
        self.assertEqual(
            evidence.source.selection_inputs["ingest_manifest"].locator,
            "manifest-entry:five-source-ingest-v2:synsql:"
            + original.expected_task_id,
        )
        self.assertEqual(
            evidence.source.selection_inputs["generator_code"].digest,
            loaded.generator.sha256,
        )
        self.assertEqual(
            evidence.source.selection_inputs["dependency_lock"].digest,
            loaded.generator.lock_sha256,
        )

    def test_v2_registers_replays_and_publishes_exact_receipt(self) -> None:
        self._write_manifest(self._batch_manifest_doc())
        with self._batch_builders():
            first = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace
            )
            replay = ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(first.receipt, replay.receipt)
        self.assertEqual(len(first.tasks), 50)
        assert first.receipt is not None
        receipt = ingest_mod.FiveSourceBatchIngestReceipt.model_validate_json(
            first.receipt.read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt.generator,
            ingest_mod.load_five_source_batch_manifest(self.manifest).generator,
        )
        self.assertEqual(receipt.expected_task_count, 50)
        self.assertEqual(
            receipt.origin_counts,
            {origin: 10 for origin in ingest_mod.FIVE_ORIGINS},
        )
        self.assertEqual(len(list((self.workspace / "tasks").iterdir())), 50)
        for task in first.tasks:
            provenance = load_current(self.workspace / "tasks" / task.task_id)
            assert provenance is not None
            self.assertEqual(provenance.task_id, task.task_id)
            self.assertEqual(
                provenance.source.selection_inputs["generator_code"].digest,
                receipt.generator.sha256,
            )
            self.assertEqual(
                provenance.source.selection_inputs["dependency_lock"].digest,
                receipt.generator.lock_sha256,
            )

    def test_v2_crash_prefix_is_completed_by_replay(self) -> None:
        self._write_manifest(self._batch_manifest_doc())
        real_register = Engine.register
        calls = 0

        def crash_on_seventeenth(engine, task, *, allow_overwrite=False):
            nonlocal calls
            calls += 1
            if calls == 17:
                raise RuntimeError("simulated ingest crash")
            return real_register(
                engine, task, allow_overwrite=allow_overwrite
            )

        with (
            self._batch_builders(),
            mock.patch.object(
                Engine,
                "register",
                autospec=True,
                side_effect=crash_on_seventeenth,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated ingest crash"),
        ):
            ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(len(list((self.workspace / "tasks").iterdir())), 16)
        self.assertEqual(
            list(
                (self.workspace / "state" / ingest_mod.INGEST_RECEIPT_DIRNAME).glob(
                    "*.json"
                )
            ),
            [],
        )

        with self._batch_builders():
            completed = ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(len(completed.tasks), 50)
        self.assertIsNotNone(completed.receipt)

    def test_v2_claim_only_crash_is_completed_by_exact_manifest_replay(self) -> None:
        manifest_doc = self._batch_manifest_doc()
        self._write_manifest(manifest_doc)
        real_publish = provenance_mod._publish_immutable_file
        calls = 0

        def crash_between_claim_and_record(store, destination, payload, *, conflict):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated crash after provenance claim")
            return real_publish(store, destination, payload, conflict=conflict)

        with (
            self._batch_builders(),
            mock.patch.object(
                provenance_mod,
                "_publish_immutable_file",
                side_effect=crash_between_claim_and_record,
            ),
            self.assertRaisesRegex(OSError, "simulated crash"),
        ):
            ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )

        first_task_dir = self.workspace / "tasks" / "dbt__batch_00"
        store = first_task_dir / provenance_mod.INGEST_PROVENANCE_DIRNAME
        self.assertEqual(len(tuple(store.glob("*.claim"))), 1)
        self.assertEqual(len(tuple(store.glob("*.json"))), 0)
        receipt_store = (
            self.workspace / "state" / ingest_mod.INGEST_RECEIPT_DIRNAME
        )
        self.assertEqual(tuple(receipt_store.glob("*.json")), ())

        conflicting_doc = json.loads(json.dumps(manifest_doc))
        conflicting_doc["sources"]["dbt"][0][
            "upstream_revision"
        ] = "revision-conflict"
        self._write_manifest(conflicting_doc)
        with self._batch_builders(), self.assertRaisesRegex(
            ValueError, r"claim\(s\) without records"
        ):
            ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(len(tuple(store.glob("*.json"))), 0)

        self._write_manifest(manifest_doc)
        with self._batch_builders():
            completed = ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace
            )

        self.assertEqual(len(completed.tasks), 50)
        self.assertIsNotNone(completed.receipt)
        self.assertEqual(len(tuple(store.glob("*.claim"))), 1)
        self.assertEqual(len(tuple(store.glob("*.json"))), 1)
        for task in completed.tasks:
            self.assertIsNotNone(
                load_current(self.workspace / "tasks" / task.task_id)
            )

    def test_v2_reuses_common_hashes_within_each_phase(self) -> None:
        self._write_manifest(self._batch_manifest_doc())
        real_digest = ingest_mod._artifact_digest
        real_adapter = ingest_mod._verify_adapter
        real_inventory = ingest_mod.wikidbs_node_inventory_sha256
        artifact_paths: list[Path] = []
        adapter_pools: list[str] = []
        inventory_roots: list[Path] = []

        def digest(path, pin):
            artifact_paths.append(Path(path).absolute())
            return real_digest(path, pin)

        def adapter(entry):
            adapter_pools.append(entry.pool)
            return real_adapter(entry)

        def inventory(root):
            inventory_roots.append(Path(root).absolute())
            return real_inventory(root)

        with (
            self._batch_builders(),
            mock.patch.object(ingest_mod, "_artifact_digest", side_effect=digest),
            mock.patch.object(ingest_mod, "_verify_adapter", side_effect=adapter),
            mock.patch.object(
                ingest_mod,
                "wikidbs_node_inventory_sha256",
                side_effect=inventory,
            ),
        ):
            ingest_mod.ingest_five_source_batch(
                self.manifest, workspace=self.workspace, dry_run=True
            )

        # Dry-run has two intentionally separate verification phases. Every
        # repeated pool artifact is hashed once in each, never once per entry.
        self.assertEqual(artifact_paths.count(self.synsql_tables.absolute()), 2)
        self.assertEqual(artifact_paths.count(self.schemapile_source.absolute()), 2)
        self.assertEqual(adapter_pools.count("synsql"), 2)
        self.assertEqual(adapter_pools.count("schemapile"), 2)
        self.assertEqual(inventory_roots, [self.wikidbs_root.absolute()] * 2)

    def test_v2_duplicate_independence_unit_fails_before_workspace(self) -> None:
        batch = self._batch_manifest_doc(
            {origin: 1 for origin in ingest_mod.FIVE_ORIGINS}
        )
        batch["sources"]["synsql"].append(
            {
                **batch["sources"]["synsql"][0],
                "expected_task_id": "synsql__batch_01",
                "selector": "synsql_selector_01",
            }
        )
        batch["expected_task_count"] = 6
        self._write_manifest(batch)

        def duplicate_cluster(entry, paths, catalog):
            task = self._batch_task(entry, paths, catalog)
            if entry.pool == "synsql":
                return task.model_copy(update={"cluster_id": "synsql__same_cluster"})
            return task

        with self._batch_builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod,
                    "_build_synsql_task",
                    side_effect=duplicate_cluster,
                )
            )
            with self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError, "repeats cluster_id"
            ):
                ingest_mod.ingest_five_source_batch(
                    self.manifest, workspace=self.workspace
                )
        self.assertFalse(self.workspace.exists())

    def test_manifest_requires_exact_versioned_five_source_shape(self) -> None:
        loaded = ingest_mod.load_five_source_manifest(self.manifest)
        self.assertEqual(
            tuple(Origin(entry.pool) for entry in loaded.ordered_entries()),
            ingest_mod.FIVE_ORIGINS,
        )

        incomplete = self._manifest_doc()
        del incomplete["sources"]["wikidbs"]
        self._write_manifest(incomplete)
        with self.assertRaisesRegex(ingest_mod.FiveSourceIngestError, "wikidbs"):
            ingest_mod.load_five_source_manifest(self.manifest)

        unsupported = self._manifest_doc()
        unsupported["schema_version"] = "five-source-ingest-v2"
        self._write_manifest(unsupported)
        with self.assertRaisesRegex(ingest_mod.FiveSourceIngestError, "schema_version"):
            ingest_mod.load_five_source_manifest(self.manifest)

        unverified = self._manifest_doc()
        unverified["sources"]["wikidbs"]["verify_nodes"] = False
        self._write_manifest(unverified)
        with self.assertRaisesRegex(ingest_mod.FiveSourceIngestError, "verify_nodes"):
            ingest_mod.load_five_source_manifest(self.manifest)

    def test_duplicate_yaml_key_is_refused(self) -> None:
        self.manifest.write_text(
            "schema_version: five-source-ingest-v1\n"
            "schema_version: five-source-ingest-v1\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ingest_mod.FiveSourceIngestError, "duplicate key"):
            ingest_mod.load_five_source_manifest(self.manifest)

    def test_digest_failure_and_late_adapter_failure_leave_target_untouched(self) -> None:
        self.dbt_manifest.write_text("changed", encoding="utf-8")
        with self._builders(), self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError, "dbt.manifest digest mismatch"
        ):
            ingest_mod.ingest_five_sources(self.manifest, workspace=self.workspace)
        self.assertFalse(self.workspace.exists())

        self._write_manifest()
        with self._builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod,
                    "_build_schemapile_task",
                    side_effect=ValueError("late schema refusal"),
                )
            )
            with self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError, "late schema refusal"
            ):
                ingest_mod.ingest_five_sources(self.manifest, workspace=self.workspace)
        self.assertFalse(self.workspace.exists())

    def test_population_coverage_failure_leaves_target_untouched(self) -> None:
        def invalid_population_task(entry, paths, catalog):
            task = self._task(entry, paths, catalog)
            populations = tuple(
                population
                for population in task.populations
                if population.name is not PopulationName.STRESS
            )
            return task.model_copy(update={"populations": populations})

        with self._builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod,
                    "_build_wikidbs_task",
                    side_effect=invalid_population_task,
                )
            )
            with self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError,
                "population coverage: missing population: stress",
            ):
                ingest_mod.ingest_five_sources(
                    self.manifest, workspace=self.workspace
                )
        self.assertFalse(self.workspace.exists())

    def test_adapters_read_digest_bound_file_and_tree_snapshots(self) -> None:
        """A change/read/restore attack cannot influence the built TaskIR."""

        original_file = self.synsql_tables.read_bytes()
        original_tree_file = (self.wikidbs_database / "schema.json").read_bytes()
        observations: dict[str, object] = {}

        def attack_file(entry, paths, catalog):
            snapshot = paths["tables"]
            observations["file_path"] = snapshot
            self.synsql_tables.write_text('[{"attacker": true}]', encoding="utf-8")
            try:
                observations["file_bytes"] = snapshot.read_bytes()
            finally:
                self.synsql_tables.write_bytes(original_file)
            return self._task(entry, paths, catalog)

        def attack_tree(entry, paths, catalog):
            snapshot = paths["database"]
            observations["tree_path"] = snapshot
            live_schema = self.wikidbs_database / "schema.json"
            live_schema.write_text('{"attacker": true}', encoding="utf-8")
            try:
                observations["tree_bytes"] = (snapshot / "schema.json").read_bytes()
                observations["tree_name"] = snapshot.name
            finally:
                live_schema.write_bytes(original_tree_file)
            return self._task(entry, paths, catalog)

        with self._builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod, "_build_synsql_task", side_effect=attack_file
                )
            )
            stack.enter_context(
                mock.patch.object(
                    ingest_mod, "_build_wikidbs_task", side_effect=attack_tree
                )
            )
            result = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace, dry_run=True
            )

        self.assertEqual(len(result.tasks), 5)
        self.assertNotEqual(observations["file_path"], self.synsql_tables)
        self.assertEqual(observations["file_bytes"], original_file)
        self.assertNotEqual(observations["tree_path"], self.wikidbs_database)
        self.assertEqual(observations["tree_bytes"], original_tree_file)
        self.assertEqual(observations["tree_name"], self.wikidbs_database.name)
        self.assertEqual(self.synsql_tables.read_bytes(), original_file)
        self.assertEqual(
            (self.wikidbs_database / "schema.json").read_bytes(),
            original_tree_file,
        )
        self.assertFalse(self.workspace.exists())

    def test_wikidbs_builder_never_reopens_the_mutable_pool_root(self) -> None:
        observed: dict[str, object] = {}
        real_builder = ingest_mod._build_wikidbs_task

        def intercepted_adapter(db_dir, **kwargs):
            observed["database"] = Path(db_dir)
            observed["family_map"] = Path(kwargs["family_map_path"])
            observed["root"] = kwargs["wikidbs_root"]
            entry = ingest_mod.load_five_source_manifest(
                self.manifest
            ).sources.wikidbs
            return self._task(entry, {}, kwargs["catalog"])

        with self._builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod,
                    "_build_wikidbs_task",
                    wraps=real_builder,
                )
            )
            from elt_taskgen.adapters import wikidbs as wikidbs_adapter

            stack.enter_context(
                mock.patch.object(
                    wikidbs_adapter,
                    "to_task_ir",
                    side_effect=intercepted_adapter,
                )
            )
            ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace, dry_run=True
            )

        self.assertNotEqual(observed["database"], self.wikidbs_database)
        self.assertEqual(Path(observed["database"]).name, self.wikidbs_database.name)
        self.assertNotEqual(observed["family_map"], self.family_map)
        self.assertIsNone(observed["root"])
        self.assertFalse(self.workspace.exists())

    def test_schemapile_stale_index_is_rejected_before_workspace_write(self) -> None:
        real_builder = ingest_mod._build_schemapile_task
        key = self._common(Origin.SCHEMAPILE)["selector"]
        record = {
            "INFO": {
                "URL": "https://github.com/example/project/blob/rev/schema.sql",
                "LICENSE": "Test-1.0",
                "PERMISSIVE": True,
            },
            "TABLES": {},
        }
        self.schemapile_source.write_text(
            json.dumps({key: record}), encoding="utf-8"
        )
        indexed = schemapile_adapter.IndexedRecord(
            key=key,
            url=record["INFO"]["URL"],
            license="Test-1.0",
            permissive=True,
            repo=schemapile_adapter.origin_repo(record["INFO"]["URL"], key=key),
            # Intentionally stale while every other projection matches.
            shape="0" * 16,
            cluster="c_fixture",
            metrics=schemapile_adapter.record_metrics(record),
        )
        index = schemapile_adapter.SchemaPileIndex(
            records=(indexed,),
            clusters=(
                schemapile_adapter.RecordCluster(
                    cluster_id="c_fixture",
                    repos=(indexed.repo,),
                    members=(key,),
                ),
            ),
        )
        schemapile_adapter.write_index(index, self.schemapile_index)
        self._write_manifest()

        with self._builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod,
                    "_build_schemapile_task",
                    wraps=real_builder,
                )
            )
            with self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError,
                r"SchemaPile index is stale.*shape",
            ):
                ingest_mod.ingest_five_sources(
                    self.manifest, workspace=self.workspace
                )
        self.assertFalse(self.workspace.exists())

    def test_dbt_builder_binds_intrinsic_identity_and_pinned_attribution(self) -> None:
        from elt_taskgen.adapters import dbt as dbt_adapter

        old_revision = "48f695b9704ee0ee13a26ed50b09f805d0ae4bc3"
        pinned_revision = "b610ac7661d788a5395520487925d9331603d254"
        upstream = "https://github.com/fivetran/dbt_twitter"
        provenance = self._file(
            "dbt-provenance.json",
            json.dumps(
                {
                    "sources": [
                        {
                            "name": "dbt_twitter",
                            "upstream": upstream,
                            "commit": old_revision,
                        }
                    ]
                }
            ),
        )
        catalog_doc = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog_doc["pools"]["dbt"]["provenance_manifest"] = str(provenance)
        self.catalog.write_text(json.dumps(catalog_doc), encoding="utf-8")
        manifest_doc = self._manifest_doc()
        manifest_doc["sources"]["dbt"].update(
            {
                "selector": "dbt_twitter",
                "family": "twitter_ads",
                "upstream_url": upstream,
                "upstream_revision": pinned_revision,
            }
        )
        self._write_manifest(manifest_doc)
        manifest = ingest_mod.load_five_source_manifest(self.manifest)
        entry = manifest.sources.dbt
        catalog = ingest_mod.load_source_catalog(self.catalog)
        spec = dbt_adapter.CandidateSpec(
            package_name="twitter",
            sources=(
                dbt_adapter.DbtNode(
                    unique_id="source.twitter_ads.account_history",
                    name="account_history",
                    package_name="twitter_ads",
                    resource_type="source",
                ),
            ),
            models=(
                dbt_adapter.DbtNode(
                    unique_id="model.twitter_ads.account_report",
                    name="account_report",
                    package_name="twitter_ads",
                    resource_type="model",
                ),
            ),
        )
        candidate = self._task(entry, {}, catalog).model_copy(
            update={
                "family_id": "dbt__twitter_ads",
                "cluster_id": "dbt__twitter_ads",
            }
        )

        with (
            mock.patch.object(dbt_adapter, "load_manifest", return_value=spec),
            mock.patch.object(
                dbt_adapter,
                "extract_candidates",
                return_value=dbt_adapter.ExtractionResult(tasks=(candidate,)),
            ) as extract,
        ):
            task = ingest_mod._build_dbt_task(
                entry, {"manifest": self.dbt_manifest}, catalog
            )

        self.assertEqual(
            task.attribution,
            f"test dbt: dbt_twitter ({upstream} @ {pinned_revision})",
        )
        self.assertNotIn(old_revision[:12], task.attribution)
        built_spec = extract.call_args.args[0]
        self.assertEqual(built_spec.package_name, "twitter_ads")
        identity = ingest_mod._identity(
            entry,
            task,
            adapter_name="elt_taskgen.adapters.dbt",
            manifest=manifest,
        )
        self.assertEqual(identity.upstream_url, upstream)
        self.assertEqual(identity.upstream_revision, pinned_revision)
        self.assertIn(identity.upstream_url, task.attribution)
        self.assertIn(identity.upstream_revision, task.attribution)

    def test_dbt_builder_refuses_selector_or_family_relabeling(self) -> None:
        from elt_taskgen.adapters import dbt as dbt_adapter

        manifest_doc = self._manifest_doc()
        manifest_doc["sources"]["dbt"].update(
            {"selector": "dbt_twitter", "family": "twitter_ads"}
        )
        self._write_manifest(manifest_doc)
        entry = ingest_mod.load_five_source_manifest(self.manifest).sources.dbt
        catalog = ingest_mod.load_source_catalog(self.catalog)

        cases = (
            (
                "selector",
                dbt_adapter.CandidateSpec(
                    package_name="unrelated",
                    sources=(
                        dbt_adapter.DbtNode(
                            unique_id="source.twitter_ads.table",
                            name="table",
                            package_name="twitter_ads",
                            resource_type="source",
                        ),
                    ),
                ),
                "does not match pinned selector",
            ),
            (
                "family",
                dbt_adapter.CandidateSpec(
                    package_name="twitter",
                    sources=(
                        dbt_adapter.DbtNode(
                            unique_id="source.unrelated.table",
                            name="table",
                            package_name="unrelated",
                            resource_type="source",
                        ),
                    ),
                ),
                "do not exactly match pinned family",
            ),
        )
        for label, spec, message in cases:
            with self.subTest(label=label), mock.patch.object(
                dbt_adapter, "load_manifest", return_value=spec
            ), mock.patch.object(dbt_adapter, "extract_candidates") as extract:
                with self.assertRaisesRegex(
                    ingest_mod.FiveSourceIngestError, message
                ):
                    ingest_mod._build_dbt_task(
                        entry, {"manifest": self.dbt_manifest}, catalog
                    )
                extract.assert_not_called()

    def test_dlt_builder_refuses_embedded_upstream_or_commit_mismatch(self) -> None:
        from elt_taskgen.adapters import dlt as dlt_adapter

        entry = ingest_mod.load_five_source_manifest(self.manifest).sources.dlt
        catalog = ingest_mod.load_source_catalog(self.catalog)
        base = dlt_adapter.DltManifest(
            connector=entry.connector,
            record=entry.selector,
            license=entry.license,
            upstream=entry.upstream_url,
            commit=entry.upstream_revision,
            endpoints=(
                dlt_adapter.DltEndpoint(
                    name="items",
                    path="/items",
                    primary_key=("id",),
                    write_disposition="merge",
                ),
            ),
        )
        cases = (
            (
                "upstream",
                base.model_copy(update={"upstream": "https://example.test/other"}),
                "manifest upstream .* != pinned upstream_url",
            ),
            (
                "commit",
                base.model_copy(update={"commit": "revision-other"}),
                "manifest commit .* != pinned upstream_revision",
            ),
        )
        for label, connector, message in cases:
            with self.subTest(label=label), mock.patch.object(
                dlt_adapter, "load_connector", return_value=connector
            ), mock.patch.object(dlt_adapter, "to_task_ir") as to_task:
                with self.assertRaisesRegex(
                    ingest_mod.FiveSourceIngestError, message
                ):
                    ingest_mod._build_dlt_task(
                        entry, {"manifest": self.dlt_manifest}, catalog
                    )
                to_task.assert_not_called()

    def test_v2_dlt_builder_requires_intrinsic_provenance_and_pins_attribution(
        self,
    ) -> None:
        from elt_taskgen.adapters import dlt as dlt_adapter

        legacy_entry = ingest_mod.load_five_source_manifest(self.manifest).sources.dlt
        entry = legacy_entry.model_copy(
            update={"require_embedded_provenance": True}
        )
        catalog = ingest_mod.load_source_catalog(self.catalog)
        endpoint = dlt_adapter.DltEndpoint(
            name="items",
            path="/items",
            primary_key=("id",),
            write_disposition="merge",
        )
        missing = dlt_adapter.DltManifest(
            connector=entry.connector,
            record=entry.selector,
            license=entry.license,
            endpoints=(endpoint,),
        )
        with mock.patch.object(
            dlt_adapter, "load_connector", return_value=missing
        ), mock.patch.object(dlt_adapter, "to_task_ir") as to_task:
            with self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError,
                "must embed both upstream and commit",
            ):
                ingest_mod._build_dlt_task(
                    entry, {"manifest": self.dlt_manifest}, catalog
                )
            to_task.assert_not_called()

        stale = missing.model_copy(
            update={
                "upstream": entry.upstream_url,
                "commit": entry.upstream_revision,
                "attribution": "stale unpinned attribution",
            }
        )
        expected_task = demo_task()
        with mock.patch.object(
            dlt_adapter, "load_connector", return_value=stale
        ), mock.patch.object(
            dlt_adapter, "to_task_ir", return_value=expected_task
        ) as to_task:
            observed = ingest_mod._build_dlt_task(
                entry, {"manifest": self.dlt_manifest}, catalog
            )
        self.assertIs(observed, expected_task)
        selection = to_task.call_args.kwargs["selection"]
        self.assertEqual(
            selection.attribution,
            ingest_mod._pinned_attribution(catalog.pool("dlt"), entry),
        )
        self.assertNotIn("stale unpinned attribution", selection.attribution)

    def test_dangling_workspace_roots_fail_closed(self) -> None:
        (self.workspace / "state").mkdir(parents=True)
        contamination = self.workspace / "state" / "contamination"
        contamination.symlink_to(self.root / "missing-contamination", target_is_directory=True)
        with self._builders(), self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError,
            "contamination index must be a non-symlink directory",
        ):
            ingest_mod.ingest_five_sources(self.manifest, workspace=self.workspace)
        self.assertFalse((self.workspace / "tasks").exists())

        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        (other_workspace / "tasks").symlink_to(
            self.root / "missing-tasks", target_is_directory=True
        )
        with self._builders(), self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError,
            "tasks root must be a non-symlink directory",
        ):
            ingest_mod.ingest_five_sources(self.manifest, workspace=other_workspace)

    def test_planted_receipt_symlink_fails_before_task_write(self) -> None:
        loaded = ingest_mod.load_five_source_manifest(self.manifest)
        store = (
            self.workspace
            / "state"
            / ingest_mod.INGEST_RECEIPT_DIRNAME
        )
        store.mkdir(parents=True)
        destination = store / f"{loaded.manifest_sha256()}.json"
        destination.symlink_to(self.root / "missing-receipt")

        with self._builders(), self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError,
            "receipt destination is not a regular file",
        ):
            ingest_mod.ingest_five_sources(self.manifest, workspace=self.workspace)
        self.assertFalse((self.workspace / "tasks").exists())
        self.assertTrue(destination.is_symlink())

    def test_adapter_digest_is_rechecked_after_build_and_before_write(self) -> None:
        real_verify = ingest_mod._verify_adapter
        calls = 0

        def changes_after_build(entry):
            nonlocal calls
            calls += 1
            if calls == 6:
                raise ingest_mod.FiveSourceIngestError("adapter changed after build")
            return real_verify(entry)

        with (
            self._builders(),
            mock.patch.object(
                ingest_mod, "_verify_adapter", side_effect=changes_after_build
            ),
            self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError, "adapter changed after build"
            ),
        ):
            ingest_mod.ingest_five_sources(self.manifest, workspace=self.workspace)
        self.assertFalse(self.workspace.exists())

        live_workspace = self.root / "live-workspace"
        calls = 0

        def changes_before_write(entry):
            nonlocal calls
            calls += 1
            if calls == 11:
                raise ingest_mod.FiveSourceIngestError("adapter changed before write")
            return real_verify(entry)

        with (
            self._builders(),
            mock.patch.object(
                ingest_mod, "_verify_adapter", side_effect=changes_before_write
            ),
            self.assertRaisesRegex(
                ingest_mod.FiveSourceIngestError, "adapter changed before write"
            ),
        ):
            ingest_mod.ingest_five_sources(
                self.manifest, workspace=live_workspace
            )
        self.assertEqual(tuple((live_workspace / "tasks").iterdir()), ())

    def test_valid_manifest_registers_exact_roster_and_is_resume_safe(self) -> None:
        with self._builders():
            result = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(tuple(task.origin for task in result.tasks), ingest_mod.FIVE_ORIGINS)
        self.assertIsNotNone(result.receipt)
        assert result.receipt is not None
        self.assertTrue(result.receipt.is_file())
        self.assertEqual(
            cli_mod._registered_task_ids(self.workspace, None),
            tuple(sorted(result.task_ids)),
        )
        for task in result.tasks:
            evidence = load_current(self.workspace / "tasks" / task.task_id)
            assert evidence is not None
            self.assertEqual(
                evidence.source.selector,
                self._manifest_doc()["sources"][task.origin.value]["selector"],
            )
            self.assertEqual(evidence.task_content_hash, task.revisions[0].content_hash)
            self.assertEqual(
                evidence.source.selection_inputs["ingest_manifest"].locator,
                f"manifest-entry:five-source-ingest-v1:{task.origin.value}",
            )
            self.assertIn("source_catalog", evidence.source.selection_inputs)
            if task.origin is Origin.SCHEMAPILE:
                self.assertIn("schemapile_index", evidence.source.selection_inputs)
            if task.origin is Origin.WIKIDBS:
                self.assertEqual(
                    set(evidence.source.selection_inputs),
                    {
                        "ingest_manifest",
                        "source_catalog",
                        "wikidbs_family_map",
                        "wikidbs_node_inventory",
                    },
                )

        # Advancing a task after intake must not make a replay roll it back.
        engine = Engine(self.workspace)
        try:
            old = engine.load_task("dlt__fixture")
            authored = old.model_copy(update={"title": "Authored title"}).with_revision(
                route=RepairRoute.SPECIFICATION,
                reason="test semantic authoring",
            )
            engine.save_task(authored)
        finally:
            engine.close()
        with self._builders():
            repeated = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace
            )
        self.assertEqual(result.receipt, repeated.receipt)
        engine = Engine(self.workspace)
        try:
            self.assertEqual(engine.load_task("dlt__fixture").title, "Authored title")
        finally:
            engine.close()

    def test_one_source_upgrade_preserves_other_provenance_bytes(self) -> None:
        with self._builders():
            first = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace
            )
        initial = {
            task.origin: load_current(self.workspace / "tasks" / task.task_id)
            for task in first.tasks
        }
        self.synsql_tables.write_text('[{"changed": true}]', encoding="utf-8")
        updated = self._manifest_doc()
        self._write_manifest(updated)

        def changed_synsql(entry, paths, catalog):
            return self._task(entry, paths, catalog).model_copy(
                update={"title": "new SynSQL source semantics"}
            )

        with self._builders() as stack:
            stack.enter_context(
                mock.patch.object(
                    ingest_mod,
                    "_build_synsql_task",
                    side_effect=changed_synsql,
                )
            )
            second = ingest_mod.ingest_five_sources(
                self.manifest,
                workspace=self.workspace,
                reingest=True,
            )

        self.assertNotEqual(first.receipt, second.receipt)
        for task in second.tasks:
            current = load_current(self.workspace / "tasks" / task.task_id)
            assert current is not None
            if task.origin is Origin.SYNSQL:
                self.assertNotEqual(current, initial[task.origin])
            else:
                self.assertEqual(
                    current.deterministic_bytes(),
                    initial[task.origin].deterministic_bytes(),
                )

    def test_requested_lineage_conflict_is_read_only_before_reingest(self) -> None:
        with self._builders():
            first = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace
            )
        dbt_task = next(task for task in first.tasks if task.origin is Origin.DBT)
        old_evidence = load_current(
            self.workspace / "tasks" / dbt_task.task_id
        )
        assert old_evidence is not None

        engine = Engine(self.workspace)
        try:
            replacement = dbt_task.model_copy(
                update={"title": "different current lineage", "revisions": ()}
            )
            engine.register(replacement, allow_overwrite=True)
            stored = engine.load_task(dbt_task.task_id)
            replacement_evidence = IngestProvenance(
                task_id=stored.task_id,
                task_content_hash=stored.revisions[0].content_hash,
                source=old_evidence.source,
            )
            publish_or_confirm(engine.task_dir(stored.task_id), replacement_evidence)
        finally:
            engine.close()

        task_path = self.workspace / "tasks" / dbt_task.task_id / "task_ir.json"
        before = task_path.read_bytes()
        conflicting = self._manifest_doc()
        conflicting["sources"]["dbt"]["upstream_revision"] = "revision-conflict"
        self._write_manifest(conflicting)
        with self._builders(), self.assertRaisesRegex(
            ingest_mod.FiveSourceIngestError,
            "cannot relabel an unchanged lineage",
        ):
            ingest_mod.ingest_five_sources(
                self.manifest,
                workspace=self.workspace,
                reingest=True,
            )
        self.assertEqual(task_path.read_bytes(), before)

    def test_dry_run_builds_all_five_without_creating_workspace(self) -> None:
        with self._builders():
            result = ingest_mod.ingest_five_sources(
                self.manifest, workspace=self.workspace, dry_run=True
            )
        self.assertTrue(result.dry_run)
        self.assertEqual(len(result.tasks), 5)
        self.assertFalse(self.workspace.exists())

    def test_pipeline_ingest_happens_before_roster_and_provider_work(self) -> None:
        invalid_args = cli_mod.build_parser().parse_args(
            [
                "pipeline",
                "--workspace",
                str(self.workspace),
                "--ingest-manifest",
                str(self.manifest),
                "--workers",
                "0",
            ]
        )
        with (
            mock.patch.object(cli_mod, "_active_campaign_fingerprint") as fingerprint,
            mock.patch.object(ingest_mod, "ingest_five_sources") as ingest,
            self.assertRaisesRegex(cli_mod.CliUsageError, "workers"),
        ):
            cli_mod.cmd_pipeline(invalid_args)
        fingerprint.assert_not_called()
        ingest.assert_not_called()
        self.assertFalse(self.workspace.exists())

        invalid = self.root / "invalid.yaml"
        invalid.write_text("schema_version: wrong\n", encoding="utf-8")
        args = cli_mod.build_parser().parse_args(
            [
                "pipeline",
                "--workspace",
                str(self.workspace),
                "--ingest-manifest",
                str(invalid),
            ]
        )
        with mock.patch.object(cli_mod, "_registered_task_ids") as roster:
            with self.assertRaisesRegex(cli_mod.CliUsageError, "ingest preflight"):
                cli_mod.cmd_pipeline(args)
        roster.assert_not_called()
        self.assertFalse(self.workspace.exists())

        args = cli_mod.build_parser().parse_args(
            [
                "pipeline",
                "--workspace",
                str(self.workspace),
                "--ingest-manifest",
                str(self.manifest),
                "--workers",
                "1",
            ]
        )
        with (
            self._builders(),
            mock.patch.object(cli_mod, "_active_campaign_fingerprint", return_value="f" * 64),
            mock.patch.object(
                cli_mod,
                "_pipeline_worker_payload",
                side_effect=RuntimeError("reached worker boundary"),
            ),
            self.assertRaisesRegex(RuntimeError, "reached worker boundary"),
        ):
            cli_mod.cmd_pipeline(args)
        engine = Engine(self.workspace)
        try:
            self.assertEqual(
                {
                    engine.load_task(task_id).origin
                    for task_id in cli_mod._registered_task_ids(self.workspace, None)
                },
                set(ingest_mod.FIVE_ORIGINS),
            )
        finally:
            engine.close()

    def test_package_uri_resolves_through_installed_resource_boundary(self) -> None:
        resolved = ingest_mod._resolve_path(
            "package://config/sources.yaml",
            manifest_path=self.manifest,
            catalog=None,
            expected_pool=None,
        )
        self.assertTrue(resolved.is_file())
        self.assertEqual(sha256_file(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest())

        example = ingest_mod.load_five_source_manifest(
            ingest_mod.resource_path("config/five_source_ingest.example.yaml")
        )
        self.assertEqual(
            example.sources.dbt.upstream_revision,
            "b610ac7661d788a5395520487925d9331603d254",
        )
        self.assertEqual(
            example.sources.dbt.manifest.sha256,
            "f467ddab9e8518bfe51e86827df5e4cbcdb01fd95cf91c5c8bab3f03ebf0dfd4",
        )

        original = example.sources.dbt
        relocated = original.model_copy(
            update={
                "manifest": original.manifest.model_copy(
                    update={"path": "/another-host/artifacts/manifest.json"}
                )
            }
        )
        moved_manifest = example.model_copy(
            update={"sources": example.sources.model_copy(update={"dbt": relocated})}
        )
        self.assertEqual(
            example.source_entry_sha256(original),
            moved_manifest.source_entry_sha256(relocated),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

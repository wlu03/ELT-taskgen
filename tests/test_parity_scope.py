"""TaskIR-derived required parity case manifests."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.destinations import Destination
from elt_taskgen.verification.parity_scope import (
    PARITY_SCOPE_POLICY_VERSION,
    ParityCaseManifest,
    derive_task_parity_manifest,
)


class ParityScopeTests(unittest.TestCase):
    def test_demo_scope_uses_only_its_backends_and_types(self) -> None:
        task = demo_task()
        manifest = derive_task_parity_manifest(task, Destination.SNOWFLAKE)
        cases = set(manifest.case_ids)

        self.assertEqual(manifest.policy_version, PARITY_SCOPE_POLICY_VERSION)
        self.assertIn("el.source.files.records", cases)
        self.assertIn("el.source.postgres.records", cases)
        self.assertIn("el.source.mongodb.records", cases)
        self.assertNotIn("el.source.rest.pagination", cases)
        self.assertNotIn("el.source.s3.multipart", cases)
        self.assertIn("warehouse.integer_width", cases)
        self.assertIn("warehouse.decimal_38_9", cases)
        self.assertIn("warehouse.text_unicode_whitespace", cases)
        self.assertIn("warehouse.sql_null", cases)
        self.assertNotIn("warehouse.json_scalar", cases)
        self.assertNotIn("warehouse.timestamp_utc_microsecond", cases)
        self.assertIn("snowflake.warehouse_lifecycle", cases)
        self.assertNotIn("snowflake.variant_object_array", cases)

    def test_destination_risks_change_scope_without_changing_task_identity(self) -> None:
        task = demo_task()
        manifests = {
            destination: derive_task_parity_manifest(task, destination)
            for destination in Destination
        }
        self.assertEqual(
            {manifest.task_content_hash for manifest in manifests.values()},
            {task.content_hash()},
        )
        self.assertEqual(len({item.manifest_digest for item in manifests.values()}), 3)
        self.assertIn(
            "databricks.unity_catalog_volume_permissions",
            manifests[Destination.DATABRICKS].case_ids,
        )
        self.assertIn(
            "redshift.s3_copy_cleanup",
            manifests[Destination.REDSHIFT].case_ids,
        )

    def test_scope_is_deterministic_and_tamper_evident(self) -> None:
        first = derive_task_parity_manifest(demo_task(), Destination.REDSHIFT)
        second = derive_task_parity_manifest(demo_task(), "redshift")
        self.assertEqual(first, second)

        payload = first.model_dump(mode="python")
        with self.assertRaises(ValidationError):
            ParityCaseManifest(**(payload | {"case_set_digest": "0" * 64}))
        with self.assertRaises(ValidationError):
            ParityCaseManifest(**(payload | {"task_id": "another-task"}))
        with self.assertRaises(ValidationError):
            ParityCaseManifest(
                **(payload | {"case_ids": tuple(reversed(first.case_ids))})
            )


if __name__ == "__main__":
    unittest.main()

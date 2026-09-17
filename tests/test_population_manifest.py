"""Durable evidence for deterministic source-population materialization."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import __version__, demo_fixture
from elt_taskgen.generation import source_data
from elt_taskgen.models import PopulationName, canonical_json


class PopulationMaterializationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = demo_fixture.demo_task()
        self.population = PopulationName.DEVELOPMENT

    def _materialize(self, root: Path) -> tuple[Path, dict]:
        pop_dir = root / self.population.value
        source_data.materialize_population(self.task, self.population, pop_dir)
        path = pop_dir / source_data.POPULATION_MANIFEST_FILENAME
        return pop_dir, json.loads(path.read_text(encoding="utf-8"))

    def test_manifest_binds_identity_seed_generator_rows_and_every_data_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pop_dir, manifest = self._materialize(Path(tmp))

            self.assertEqual(
                manifest["schema_version"],
                source_data.POPULATION_MANIFEST_SCHEMA_VERSION,
            )
            self.assertEqual(manifest["task_id"], self.task.task_id)
            self.assertEqual(manifest["task_content_hash"], self.task.content_hash())
            self.assertEqual(manifest["population_name"], self.population.value)
            spec = self.task.population(self.population)
            self.assertEqual(manifest["population_spec_seed"], spec.seed)
            self.assertEqual(manifest["population_spec_hash"], spec.content_hash())

            generator = manifest["generator"]
            self.assertEqual(generator["package"], "elt-taskgen")
            self.assertEqual(generator["package_version"], __version__)
            self.assertEqual(
                generator["policy_version"], source_data.GENERATION_POLICY_VERSION
            )
            self.assertEqual(
                generator["implementation_sha256"],
                hashlib.sha256(Path(source_data.__file__).read_bytes()).hexdigest(),
            )

            expected_files = {
                path.relative_to(pop_dir).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for dirname in (
                    source_data.POPULATION_ROWS_DIR,
                    source_data.POPULATION_RENDERED_DIR,
                )
                for path in sorted((pop_dir / dirname).rglob("*"))
                if path.is_file()
            }
            self.assertEqual(manifest["files"], expected_files)
            self.assertNotIn(source_data.POPULATION_MANIFEST_FILENAME, manifest["files"])

            rows = source_data.generate_rows(self.task, self.population)
            self.assertEqual(set(manifest["logical_rows"]), set(rows))
            for table, table_rows in rows.items():
                recorded = manifest["logical_rows"][table]
                self.assertEqual(recorded["row_count"], len(table_rows))
                self.assertEqual(
                    recorded["sha256"],
                    hashlib.sha256(
                        canonical_json(table_rows).encode("utf-8")
                    ).hexdigest(),
                )

    def test_manifest_bytes_are_deterministic_and_part_of_the_tree_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _ = self._materialize(root / "one")
            second, _ = self._materialize(root / "two")
            first_path = first / source_data.POPULATION_MANIFEST_FILENAME
            second_path = second / source_data.POPULATION_MANIFEST_FILENAME
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            self.assertEqual(
                source_data.tree_digest(first)[source_data.POPULATION_MANIFEST_FILENAME],
                hashlib.sha256(first_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                source_data.population_drift(self.task, self.population, first), ()
            )

    def test_full_materialization_is_deterministic_in_fresh_processes(self) -> None:
        script = """
import hashlib
import json
import sys
from pathlib import Path
from elt_taskgen import demo_fixture
from elt_taskgen.generation import source_data
from elt_taskgen.models import PopulationName

root = Path(sys.argv[1])
source_data.materialize_population(
    demo_fixture.demo_task(), PopulationName.DEVELOPMENT, root
)
manifest = json.loads(
    (root / source_data.POPULATION_MANIFEST_FILENAME).read_text(encoding="utf-8")
)
print(json.dumps({
    "manifest_sha256": hashlib.sha256(
        (root / source_data.POPULATION_MANIFEST_FILENAME).read_bytes()
    ).hexdigest(),
    "logical_rows": manifest["logical_rows"],
    "files": manifest["files"],
}, sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = []
            for name in ("fresh-one", "fresh-two"):
                process = subprocess.run(
                    [sys.executable, "-c", script, str(root / name)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PYTHONHASHSEED": "random"},
                )
                outputs.append(process.stdout.strip())
            self.assertEqual(outputs[0], outputs[1])

    def test_manifest_tampering_and_semantic_identity_changes_are_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pop_dir, manifest = self._materialize(Path(tmp))
            manifest_path = pop_dir / source_data.POPULATION_MANIFEST_FILENAME
            manifest["population_spec_seed"] += 1
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            self.assertEqual(
                source_data.population_drift(self.task, self.population, pop_dir),
                (f"{source_data.POPULATION_MANIFEST_FILENAME}: differs",),
            )

            # Rematerialize, then move a semantic field which does not affect row
            # synthesis.  Only the evidence sidecar should move, proving the
            # population is bound to the TaskIR identity rather than just bytes.
            source_data.materialize_population(self.task, self.population, pop_dir)
            moved = self.task.model_copy(update={"title": self.task.title + " revised"})
            self.assertNotEqual(moved.content_hash(), self.task.content_hash())
            self.assertEqual(
                source_data.population_drift(moved, self.population, pop_dir),
                (f"{source_data.POPULATION_MANIFEST_FILENAME}: differs",),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Byte contracts captured before consolidation of plans and recipe admission.

Changing these requires reviewing generated task semantics, SQL, and public
specifications; moving implementation between modules must not change them.
"""

import hashlib
import json
from pathlib import Path
import unittest

from elt_taskgen.export.eltbench import build_data_model, public_documentation
from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY
from elt_taskgen.reference.solution import build_reference
from tests.test_plan_library import SHAPES, EVIDENCE, build_task
from tests.test_recipe_registry import recipe_source


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class GenerationContractTests(unittest.TestCase):
    def test_existing_recipe_and_shape_outputs_are_unchanged(self):
        expected = json.loads((Path(__file__).parent / "fixtures/generation_contracts.json").read_text())
        factories = {name: lambda name=name, recipe=recipe: recipe.apply(recipe_source(name))
                     for name, recipe in RECIPE_REGISTRY.items()}
        factories.update({"shape:" + name: lambda name=name, builder=builder: build_task(
            builder(EVIDENCE, mart=name + "_mart"), "proof__" + name)
            for name, builder in SHAPES})
        self.assertEqual(set(factories), set(expected))
        for name, build in factories.items():
            with self.subTest(recipe=name):
                task = build()
                actual = {
                    "task": digest(task.model_dump(mode="json")),
                    "sql": digest(build_reference(task).sql_by_mart),
                    "data_model": digest(build_data_model(task)),
                    "documentation": digest(public_documentation(task)),
                }
                self.assertEqual(actual, expected[name])

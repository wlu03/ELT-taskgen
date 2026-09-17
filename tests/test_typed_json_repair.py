"""Strict typed-JSON anchors for route-scoped repair patches."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from elt_taskgen import repair
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import (
    MAX_REPLACE_JSON_EDITS,
    MAX_REPLACE_JSON_TEXT_BYTES,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    canonical_json,
    task_to_json,
)
from elt_taskgen.review import repair_proposer as rp


def typed_edit(locator: str, old: object, new: object) -> RepairEdit:
    return RepairEdit(
        op=RepairEditOp.REPLACE_JSON,
        locator=locator,
        old=canonical_json(old),
        new=canonical_json(new),
    )


def population_patch(edit: RepairEdit) -> RepairPatch:
    return RepairPatch(
        route=RepairRoute.POPULATION,
        artifact="task_ir.json",
        edits=(edit,),
        rationale="plant the nullable-label discriminator",
        proposer_role="human_adjudicator",
    )


class ReplaceJsonModelTest(unittest.TestCase):
    def test_accepts_distinct_canonical_typed_values(self) -> None:
        edit = typed_edit("populations.3.literal_rows.landings.0.label", "beta", None)
        self.assertEqual((edit.old, edit.new), ('"beta"', "null"))

    def test_rejects_malformed_or_noncanonical_anchors(self) -> None:
        for field, value in (
            ("old", "NULL"),
            ("old", '"beta" '),
            ("old", '{"z":1,"a":2}'),
            ("old", '{"a":1,"a":1}'),
            ("new", "NaN"),
        ):
            with self.subTest(field=field, value=value):
                args = {"old": '"beta"', "new": "null"}
                args[field] = value
                with self.assertRaises(ValidationError):
                    RepairEdit(
                        op=RepairEditOp.REPLACE_JSON,
                        locator="populations.3.literal_rows.landings.0.label",
                        **args,
                    )

    def test_rejects_typed_noop_including_signed_zero(self) -> None:
        for old, new in (
            ("null", "null"),
            ('"x"', '"x"'),
            ("-0.0", "0.0"),
            ('[{"x":1}]', '[{"x":1}]'),
        ):
            with self.subTest(old=old, new=new):
                with self.assertRaises(ValidationError):
                    RepairEdit(
                        op=RepairEditOp.REPLACE_JSON,
                        locator="value",
                        old=old,
                        new=new,
                    )

    def test_integer_and_float_are_distinct_typed_json_values(self) -> None:
        edit = RepairEdit(
            op=RepairEditOp.REPLACE_JSON,
            locator="value",
            old="1",
            new="1.0",
        )
        self.assertEqual(edit.op, RepairEditOp.REPLACE_JSON)

    def test_accepts_bounded_canonical_containers(self) -> None:
        edit = typed_edit(
            "populations.3.literal_rows.jobs",
            [{"job_id": 1, "label": "old"}],
            [{"job_id": 1, "label": None}],
        )
        self.assertEqual(edit.op, RepairEditOp.REPLACE_JSON)

    def test_rejects_oversized_anchor_before_application(self) -> None:
        oversized = canonical_json("x" * MAX_REPLACE_JSON_TEXT_BYTES)
        with self.assertRaises(ValidationError):
            RepairEdit(
                op=RepairEditOp.REPLACE_JSON,
                locator="populations.3.literal_rows.jobs",
                old=oversized,
                new="null",
            )

    def test_patch_bounds_the_number_of_container_rewrites(self) -> None:
        edit = typed_edit("populations.3.literal_rows.jobs", [], [{"job_id": 1}])
        with self.assertRaises(ValidationError):
            RepairPatch(
                route=RepairRoute.POPULATION,
                artifact="task_ir.json",
                edits=tuple(edit for _ in range(MAX_REPLACE_JSON_EDITS + 1)),
                rationale="too many whole-container rewrites",
                proposer_role="human_adjudicator",
            )


class ReplaceJsonApplicationTest(unittest.TestCase):
    def test_replaces_one_resolved_string_leaf_with_json_null(self) -> None:
        patch = population_patch(typed_edit("rows.0.label", "beta", None))
        updated = rp.apply_patch_text('{"rows":[{"label":"beta"}]}', patch)
        self.assertEqual(json.loads(updated), {"rows": [{"label": None}]})

    def test_replaces_one_existing_array_with_exact_typed_contents(self) -> None:
        old = [{"job_id": 1, "stage": "screen"}]
        new = [
            {"job_id": 1, "stage": "screen"},
            {"job_id": 2, "stage": None},
        ]
        patch = population_patch(typed_edit("literal_rows.jobs_stages", old, new))
        updated = rp.apply_patch_text(
            canonical_json({"literal_rows": {"jobs_stages": old}}), patch
        )
        self.assertEqual(
            json.loads(updated), {"literal_rows": {"jobs_stages": new}}
        )

    def test_anchor_comparison_is_type_exact(self) -> None:
        patch = population_patch(typed_edit("value", 1.0, None))
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text('{"value":1}', patch)
        self.assertEqual(ctx.exception.code, "patch_anchor_not_found")

    def test_missing_locator_and_non_json_artifact_fail_closed(self) -> None:
        missing = population_patch(typed_edit("rows.1.label", "beta", None))
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text('{"rows":[{"label":"beta"}]}', missing)
        self.assertEqual(ctx.exception.code, "patch_anchor_not_found")

        non_json = population_patch(typed_edit("whole", "beta", None)).model_copy(
            update={"artifact": "notes.txt"}
        )
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text("beta", non_json)
        self.assertEqual(ctx.exception.code, "patch_anchor_not_found")

    def test_defensive_application_rejects_bypassed_malformed_and_noop_models(self) -> None:
        malformed = RepairEdit.model_construct(
            op=RepairEditOp.REPLACE_JSON,
            locator="value",
            old=" null ",
            new='"x"',
        )
        noop = RepairEdit.model_construct(
            op=RepairEditOp.REPLACE_JSON,
            locator="value",
            old="null",
            new="null",
        )
        for edit, code in ((malformed, "patch_anchor_not_found"), (noop, "patch_noop")):
            with self.subTest(code=code):
                patch = RepairPatch.model_construct(
                    route=RepairRoute.POPULATION,
                    artifact="task_ir.json",
                    edits=(edit,),
                    rationale="bypassed fixture",
                    proposer_role="test",
                )
                with self.assertRaises(rp.PatchApplicationError) as ctx:
                    rp.apply_patch_text('{"value":null}', patch)
                self.assertEqual(ctx.exception.code, code)

        one = typed_edit("value", None, "x")
        over_limit = RepairPatch.model_construct(
            route=RepairRoute.POPULATION,
            artifact="task_ir.json",
            edits=tuple(one for _ in range(MAX_REPLACE_JSON_EDITS + 1)),
            rationale="bypassed fixture",
            proposer_role="test",
        )
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text('{"value":null}', over_limit)
        self.assertEqual(ctx.exception.code, "patch_anchor_not_found")


class ReplaceJsonScopeTest(unittest.TestCase):
    def test_literal_row_typed_edit_routes_only_as_population(self) -> None:
        task = demo_task()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "tasks" / task.task_id / "task_ir.json"
            target.parent.mkdir(parents=True)
            target.write_text(task_to_json(task), encoding="utf-8")
            before_ir = target.read_text(encoding="utf-8")
            before = repair.snapshot(workspace, task.task_id)
            locator = "populations.3.literal_rows.customers.0.customer_name"
            patch = population_patch(typed_edit(locator, "C10", None))
            target.write_text(rp.apply_patch_text(before_ir, patch), encoding="utf-8")
            after = repair.snapshot(workspace, task.task_id)

            diff = rp.validate_scope(
                workspace,
                task.task_id,
                patch,
                before,
                after,
                before_ir=before_ir,
            )
            self.assertEqual(
                diff.changed,
                frozenset({f"tasks/{task.task_id}/task_ir.json"}),
            )

            wrong_claim = patch.model_copy(update={"route": RepairRoute.SPECIFICATION})
            with self.assertRaises(rp.ScopeViolation) as ctx:
                rp.validate_scope(
                    workspace,
                    task.task_id,
                    wrong_claim,
                    before,
                    after,
                    before_ir=before_ir,
                )
            self.assertEqual(ctx.exception.code, "scope_route_mismatch")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The projection layer and role-scoped tool registry — roadmap Phase 0.B.

WHY THIS EXISTS
Before any observation channel to a model may loop, every diagnostic string
must cross ONE typed boundary: `review/tools/projection.py`. These tests pin
the contract the trust-boundary design fixes (Output 6 section 6.7 and Output
5): a `Diagnostic` has no numeric or free-text field and names only public
identifiers; the projector (D3, value-aware) and the gatekeeper (D1, no gold)
are two halves of one sanitizer; a trip is `DiagnosticTripwire`, a harness
fault that delivers nothing; and each enabled role receives only its declared
tool surface while registry context and path policy refuse `runs/` and
credential-shaped files.

Everything here is offline: no provider, no network, no live drive.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from elt_taskgen import demo_fixture
from elt_taskgen import engine as engine_mod
from elt_taskgen.models import AttackCase, RepairRoute, TaskIR, canonical_json
from elt_taskgen.review import declarative_prose, prose_fidelity
from elt_taskgen.review import providers as P
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG
from elt_taskgen.training.contract import WorkspaceFailureClass
from elt_taskgen.training.models import _CODE_RE
from elt_taskgen.workspace import repo_root

MART = demo_fixture.MART_NAME

#: Every role the SoT names (T1), so a registry test covers all of them.
ROLES = (
    "semantic_author",
    "ambiguity_critic",
    "population_adversary",
    "shortcut_attacker",
    "feasibility_reviewer",
    "independent_loader",
    "independent_implementer",
    "repair_proposer",
    "audit_triage",
)


class FakeGold:
    """A gold bundle in the shape the projector reads (never a real one)."""

    def __init__(self, stage1, stage2_csv):
        self.stage1 = stage1
        self.stage2_csv = stage2_csv


def synthetic_gold():
    return FakeGold(
        stage1={
            "development": {"customers": 2, "orders": 4, "order_items": 8},
            "primary": {"customers": 4711, "orders": 9130, "order_items": 27390},
            "stress": {"customers": 200, "orders": 20000, "order_items": 60000},
        },
        stage2_csv={
            "primary": {MART: "customer_id,total_spend\n" + "\n".join(f"{i},1.0" for i in range(37))},
        },
    )


def wire(diag, task, package=None) -> bytes:
    return PJ.serialize_for_transport(diag, task=task, package=package).encode("utf-8")


#: The 50-task batch's evidence (READ-ONLY; tests that need it skip when absent).
BATCH_TASKS = repo_root() / "runs" / "authorized_batch_50_20260908" / "workspace-final" / "tasks"

#: Public identifiers with digits in every position the identifier grammar
#: (`_IDENT_RE`: letters, digits, `_`, `.`, `-`) admits, including the two
#: separators after which a digit run looks like a standalone number.
DIGIT_BEARING_TABLES = ("sales-2020", "covid_19_cases", "v1.2", "t2")
DIGIT_BEARING_COLUMNS = ("q1.2", "md5", "area_km2", "part-00000")
PROPOSED_CASE = "proposed__shortcut_attacker-00-e1417a5a"


def digit_bearing_task() -> TaskIR:
    """The demo task plus tables, columns and a proposed attack case whose
    PUBLIC names carry digits (a synthetic IR of the batch D5 class)."""
    doc = demo_fixture.demo_task().model_dump(mode="json")
    columns = [
        {"name": name, "type": "integer", "description": f"column {name}"}
        for name in DIGIT_BEARING_COLUMNS
    ]
    for index, name in enumerate(DIGIT_BEARING_TABLES):
        doc["tables"].append({
            "name": name, "description": f"table {name}",
            "columns": [columns[index % len(columns)], columns[(index + 1) % len(columns)]],
            "primary_key": [columns[index % len(columns)]["name"]],
        })
        doc["backends"].append({"table": name, "backend": "s3", "options": {}})
    proposed = dict(doc["attack_cases"][0])
    proposed["name"] = PROPOSED_CASE
    doc["attack_cases"].append(proposed)
    return TaskIR.model_validate(doc)


def forged(kind: str, **body) -> bytes:
    """Bytes a LEAKY producer might emit, bypassing the model constructors."""
    return canonical_json(
        {"kind": kind, "diagnostics_version": PJ.DIAGNOSTICS_VERSION, **body}
    ).encode("utf-8")


def text_payload(text: str, **overrides) -> bytes:
    body = {
        "source": "prose",
        "code": "not_represented",
        "subject": MART,
        "names": [],
        "text": text,
    }
    body.update(overrides)
    return forged("diagnostic_text", **body)


def _schema_types(node, out):
    if isinstance(node, dict):
        if "type" in node:
            types = node["type"] if isinstance(node["type"], list) else [node["type"]]
            out.extend(types)
        for value in node.values():
            _schema_types(value, out)
    elif isinstance(node, list):
        for value in node:
            _schema_types(value, out)
    return out


class DiagnosticSchemaTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_diagnostic_schema_has_no_numeric_fields(self):
        for model in (PJ.Diagnostic, PJ.DiagnosticText):
            types = _schema_types(model.model_json_schema(), [])
            self.assertNotIn("integer", types, model.__name__)
            self.assertNotIn("number", types, model.__name__)
            for name, info in model.model_fields.items():
                self.assertNotIn(info.annotation, (int, float), f"{model.__name__}.{name}")
        # Strict booleans: a number cannot sneak in as a flag or as `ok`.
        with self.assertRaises(ValidationError):
            PJ.Diagnostic(source="gate", ok=True, code="ok", flags={"leak": 1})
        with self.assertRaises(ValidationError):
            PJ.Diagnostic(source="gate", ok=1, code="ok")
        # extra="forbid": a producer cannot add a count field.
        with self.assertRaises(ValidationError):
            PJ.Diagnostic(source="gate", ok=True, code="ok", count=4711)
        # Frozen.
        diag = PJ.Diagnostic(source="gate", ok=True, code="ok", subject="determinism")
        with self.assertRaises(ValidationError):
            diag.code = "failed"

    def test_projection_models_have_no_free_text_fields(self):
        # Every string field of Diagnostic is code- or identifier-constrained.
        for bad_subject in ("has space", "/tmp/x", "42", "orders=4711", "a;b", "(1, 2)"):
            with self.assertRaises(ValidationError, msg=bad_subject):
                PJ.Diagnostic(source="gate", ok=True, code="ok", subject=bad_subject)
            with self.assertRaises(ValidationError, msg=bad_subject):
                PJ.Diagnostic(source="gate", ok=True, code="ok", names=(bad_subject,))
        for bad_code in ("Failed", "gate crashed", "ok!", "not_in_vocabulary"):
            with self.assertRaises(ValidationError, msg=bad_code):
                PJ.Diagnostic(source="gate", ok=False, code=bad_code)
        with self.assertRaises(ValidationError):
            PJ.Diagnostic(source="gate", ok=True, code="ok", flags={"Bad Key": True})
        # DevRows columns are identifiers too.
        with self.assertRaises(ValidationError):
            PJ.DevRows(columns=("customer id",), rows=())
        # The one text field lives on DiagnosticText and only text-allowlisted
        # sources may construct one; the allowlist is exactly the producers
        # whose inputs are pinned public below.
        text_fields = [
            name for name, info in PJ.DiagnosticText.model_fields.items()
            if info.annotation is str and name not in ("code", "subject")
        ]
        self.assertEqual(text_fields, ["text"])
        with self.assertRaises(ValidationError):
            PJ.DiagnosticText(source="gate", code="ok", text="gate says hi")
        self.assertEqual(
            set(PJ.TEXT_ALLOWLISTED_SOURCES), set(PJ.TEXT_PRODUCER_PUBLIC_FIELDS)
        )
        with self.assertRaises(ValidationError):
            PJ.DiagnosticText(source="prose", code="not_represented", text="two\nlines")

    def test_projection_identifiers_subset_of_public(self):
        public = PJ.PublicIdentifierSet(self.task)
        for name in (MART, "customers", "orders", "customer_id", "determinism",
                     "required-mutants", "development", "inner_join", "review"):
            self.assertIn(name, public, name)
        # Hidden populations are NOT public identifiers.
        for hidden in ("primary", "resampled", "counterfactual", "stress"):
            self.assertNotIn(hidden, public, hidden)
        # A projection naming a non-public identifier trips in BOTH halves.
        diag = PJ.Diagnostic(source="gate", ok=False, code="failed", subject="primary")
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.serialize_for_transport(diag, task=self.task)
        self.assertEqual((ctx.exception.detector, ctx.exception.code), ("schema", "identifier_not_public"))
        payload = forged(
            "diagnostic", source="gate", ok=False, code="failed", subject="determinism",
            flags={}, names=["stress"],
        )
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.assert_value_free(payload, task=self.task, route=None)
        self.assertEqual(ctx.exception.code, "identifier_not_public")
        # And a projection naming public identifiers passes both halves.
        ok = PJ.Diagnostic(
            source="gate", ok=True, code="ok", subject="referential-integrity",
            names=("orders", "customers"),
        )
        PJ.assert_value_free(wire(ok, self.task), task=self.task, route=None)

    def test_projection_codes_match_code_re(self):
        seen = 0
        for source in PJ.DiagnosticSource:
            codes = PJ.codes_for(source)
            self.assertTrue(codes, source)
            for code in codes:
                seen += 1
                self.assertIsNotNone(_CODE_RE.fullmatch(code), f"{source.value}:{code}")
                self.assertNotIn(":", code)
        for member in PJ.RejectionCode:
            self.assertIsNotNone(_CODE_RE.fullmatch(member.value), member)
        self.assertGreater(seen, 20)
        # The row-13 vocabulary is present by name.
        for name in (
            "scope_route_mismatch", "scope_path_outside_allowlist",
            "scope_field_outside_allowlist", "scope_path_escape",
            "patch_artifact_missing", "patch_anchor_not_found",
            "patch_anchor_ambiguous", "patch_noop", "discrimination_weakened",
            "revalidation_red_review", "revalidation_red_reference",
        ):
            self.assertIn(name, PJ.codes_for("rejection"), name)
        # Every DIAGNOSTICS_VERSION is recorded on the wire, not hashed.
        text = PJ.serialize_for_transport(
            PJ.Diagnostic(source="gate", ok=True, code="ok", subject="determinism"),
            task=self.task,
        )
        self.assertEqual(json.loads(text)["diagnostics_version"], PJ.DIAGNOSTICS_VERSION)

    def test_projection_sha256_matches_transport_bytes_and_render_is_fixed_template(self):
        """S2's `sha256` / `render()` on the one type (SoT T7): the digest IS the
        digest of the transport bytes (so `observation_sha256` and a tripwire's
        `payload_sha256` agree), and `render()` is a fixed template over the
        validated fields that adds nothing the wire bytes do not carry."""
        diag = PJ.Diagnostic(
            source="gate", ok=False, code="failed", subject="referential-integrity",
            names=("orders", "customers"), flags={"dangling": True},
        )
        text = PJ.serialize_for_transport(diag, task=self.task)
        self.assertEqual(diag.sha256, hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(diag.sha256, PJ.transport_sha256(diag))
        leaky = PJ.Diagnostic(source="gate", ok=False, code="failed", subject="primary")
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.serialize_for_transport(leaky, task=self.task)
        self.assertEqual(ctx.exception.payload_sha256, leaky.sha256)
        self.assertNotEqual(leaky.sha256, diag.sha256)
        self.assertEqual(
            diag.render(),
            "[gate] failed subject=referential-integrity ok=false dangling=true names=orders,customers",
        )
        self.assertEqual(PJ.Diagnostic(source="gate", ok=True, code="ok").render(), "[gate] ok ok=true")
        sentence = f"mart '{MART}': output column 'total_spend' never mentioned"
        rendered = PJ.DiagnosticText(
            source="prose", code="missing_object", subject=MART, names=("total_spend",), text=sentence,
        ).render()
        self.assertEqual(rendered, f"[prose] missing_object subject={MART} names=total_spend: {sentence}")
        rows = PJ.DevRows(
            columns=("customer_id", "total_spend"), rows=((7, 30.0), ("8", None)), truncated=True,
        )
        self.assertEqual(
            rows.render(), '[dev_rows] customer_id,total_spend\n7|30.0\n"8"|null\n(truncated)'
        )
        self.assertEqual(rows.sha256, hashlib.sha256(wire(rows, self.task)).hexdigest())
        # The template carries only what the schema allows: every token of a
        # Diagnostic's rendering is a code, a boolean or a public identifier.
        public = PJ.PublicIdentifierSet(self.task)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*", diag.render()):
            self.assertTrue(
                token in public or token in PJ.codes_for("gate") or token in ("gate", "subject", "ok", "true", "false", "names", "dangling"),
                token,
            )


class SanitizerTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _trip(self, payload: bytes, *, route=None) -> PJ.DiagnosticTripwire:
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.assert_value_free(payload, task=self.task, route=route)
        return ctx.exception

    def test_projector_and_gatekeeper_run_in_different_processes(self):
        raw = {"gate": "required-mutants", "passed": False,
               "details": "required mutant matrix not reproduced: inner_join: "
                          "LEAK — must lose reward on primary, got 1.0"}
        result = PJ.project_in_worker("gate", raw, task=self.task, package=None)
        self.assertNotEqual(result.pid, os.getpid())
        # The gatekeeper runs HERE, in the broker, over bytes only.
        payload = result.payload.encode("utf-8")
        PJ.assert_value_free(payload, task=self.task, route=None)
        doc = json.loads(result.payload)
        self.assertEqual((doc["subject"], doc["code"], doc["ok"]), ("required-mutants", "failed", False))
        self.assertNotIn("1.0", result.payload)
        self.assertNotIn("primary", result.payload)
        # A trip in the child is re-raised in the parent as the same tripwire.
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.project_in_worker("gate", {"gate": "primary=1", "passed": True}, task=self.task)
        self.assertEqual(ctx.exception.detector, "schema")

    def test_gold_count_canary_trips_on_any_frozen_count(self):
        gold = synthetic_gold()
        scalars = PJ.private_scalars(self.task, gold)
        for count in ("4711", "9130", "27390", "37", "3000", "20000"):
            self.assertIn(count, scalars, count)
        tripped = 0
        for scalar in sorted(scalars, key=int):
            diag = PJ.DiagnosticText(
                source="prose", code="not_represented", subject=MART,
                text=f"mart '{MART}': output column 'total_spend' description mentions {scalar} somewhere",
            )
            with self.assertRaises(PJ.DiagnosticTripwire, msg=scalar) as ctx:
                PJ.serialize_for_transport(diag, task=self.task, package=gold)
            self.assertEqual((ctx.exception.detector, ctx.exception.code), ("canary", "private_scalar"))
            self.assertNotIn(scalar, str(ctx.exception))
            tripped += 1
        self.assertGreater(tripped, 5)
        # A plan-rule ordinal is public and does not trip even when it equals a
        # DEVELOPMENT count; a number that is no frozen count passes the projector.
        PJ.serialize_for_transport(
            PJ.DiagnosticText(source="prose", code="not_represented", subject=MART,
                              text=f"mart '{MART}': rule 2 [filter] ('keep completed') not represented"),
            task=self.task, package=gold,
        )
        PJ.serialize_for_transport(
            PJ.DiagnosticText(source="prose", code="not_represented", subject=MART,
                              text=f"mart '{MART}': rule mentions 13 days"),
            task=self.task, package=gold,
        )
        # A code-only Diagnostic never carries a number at all, frozen or not.
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.serialize_for_transport(
                PJ.Diagnostic(source="gate", ok=False, code="failed", subject="determinism", names=("x13",)),
                task=self.task, package=gold,
            )
        self.assertIn(ctx.exception.code, ("numeric_value", "identifier_not_public"))

    def test_sanitizer_rejects_measured_reward_json(self):
        exc = self._trip(text_payload('measured {"primary": 1.0, "counterfactual": 0.0}'))
        self.assertEqual((exc.detector, exc.code), ("private_material", "measured_value"))
        exc = self._trip(text_payload("inner_join: LEAK — must lose reward on primary, got 1.0"))
        self.assertEqual(exc.code, "measured_value")
        exc = self._trip(text_payload("dual build agreement 0.75 on two populations"))
        self.assertEqual(exc.code, "measured_value")
        exc = self._trip(text_payload("development=1.000000,primary=0.250000"))
        self.assertEqual(exc.detector, "private_material")
        # A reward as a numeric field is a schema violation before anything else.
        exc = self._trip(forged("diagnostic", source="gate", ok=False, code="failed",
                                subject="required-mutants", flags={}, names=[], reward=0.25))
        self.assertEqual((exc.detector, exc.code), ("schema", "invalid_shape"))
        # And the message of a trip carries the detector, never the value.
        self.assertNotIn("0.25", str(exc))

    def test_sanitizer_rejects_gold_count_vectors(self):
        exc = self._trip(text_payload("stage-1 counts customers=4711,orders=9130"))
        self.assertEqual((exc.detector, exc.code), ("numbers", "count_vector"))
        exc = self._trip(text_payload("key ('c_9001', 3) has no parent row"))
        self.assertEqual((exc.detector, exc.code), ("numbers", "key_tuple"))
        exc = self._trip(text_payload("expected 4711 rows, got 4710"))
        self.assertEqual(exc.code, "measured_value")
        # A Diagnostic with any digit run outside a public identifier trips by
        # SHAPE: the gatekeeper holds no gold and needs none. (A flag key is a
        # code, not an identifier, so it reaches the numbers detector; a
        # non-public name is stopped by the schema detector first.)
        exc = self._trip(forged("diagnostic", source="gate", ok=False, code="failed",
                                subject="determinism", flags={"n4711": True}, names=[]))
        self.assertEqual((exc.detector, exc.code), ("numbers", "numeric_value"))
        exc = self._trip(forged("diagnostic", source="gate", ok=False, code="failed",
                                subject="determinism", flags={}, names=["c4711"]))
        self.assertEqual((exc.detector, exc.code), ("schema", "identifier_not_public"))
        exc = self._trip(forged("diagnostic", source="gate", ok=False, code="failed",
                                subject="4711", flags={}, names=[]))
        self.assertEqual(exc.detector, "schema")

    def test_closed_diagnostic_code_may_contain_a_digit(self):
        """A trusted enum label is not a measured value.

        ``s3_part_file`` is a real loader correction code.  Its ``3`` must not
        turn the correction into a session-ending leak tripwire in either the
        D3 projector or the independent D1 gatekeeper.
        """
        diag = PJ.project_load_plan(code="s3_part_file", table="customers")
        payload = wire(diag, self.task)
        PJ.assert_value_free(payload, task=self.task, route=None)
        self.assertEqual(json.loads(payload)["code"], "s3_part_file")

        # Only the closed code gets this exemption.  An arbitrary numeric flag
        # remains model-influenced material and is still refused.
        exc = self._trip(
            forged(
                "diagnostic", source="load_plan", ok=False,
                code="s3_part_file", subject="customers",
                flags={"n4711": True}, names=[],
            )
        )
        self.assertEqual((exc.detector, exc.code), ("numbers", "numeric_value"))

    def test_public_identifier_with_digits_is_a_name_in_both_halves(self):
        """Batch D5 class (reports 338/339): a public identifier carrying
        digits is recognized through `PublicIdentifierSet` by BOTH halves of
        the sanitizer, wherever the digits sit in it.

        `_digit_runs_outside_public` always skipped a public word, but the
        gatekeeper also searched the WHOLE value for the standalone-number
        shape, so a public name whose digits follow a `-` or `.` the
        identifier grammar admits (`sales-2020`, `v1.2`, and every proposed
        attack case `proposed__shortcut_attacker-00-<hash>`) passed the
        projector and then tripped D1 with `numbers/numeric_value`. The
        detector is not weakened for non-identifier material: a flag key, a
        non-public token or a bare number still trips by shape."""
        task = digit_bearing_task()
        public = PJ.PublicIdentifierSet(task)
        for name in DIGIT_BEARING_TABLES:
            self.assertIn(name, public)
            for code in PJ.LOAD_PLAN_CODES:
                if code == "ok":
                    continue
                diag = PJ.project_load_plan(code=code, table=name)
                payload = wire(diag, task)
                PJ.assert_value_free(payload, task=task, route=None)
                self.assertEqual(json.loads(payload)["subject"], name)
        for column in DIGIT_BEARING_COLUMNS:
            diag = PJ.project_bind(
                binds=False, error_class="missing_column", columns_match=False,
                missing_columns=[column], mart=MART,
            )
            PJ.assert_value_free(wire(diag, task), task=task, route=None)
        # A proposed attack case named in a required-mutants row: the exact
        # shape `_required_mutant_rows` emits for the batch's case names.
        rows = PJ._required_mutant_rows(
            f"required mutant matrix not reproduced: {PROPOSED_CASE}: LEAK — "
            "must lose reward on primary, got 1.0",
            code="failed",
        )
        self.assertEqual([r.names for r in rows], [(PROPOSED_CASE,)])
        PJ.assert_value_free(wire(rows[0], task), task=task, route=None)
        listed = PJ.project_list_schemas([t.name for t in task.tables])
        PJ.assert_value_free(wire(listed, task), task=task, route=None)
        # NOT weakened: the same digits outside a public identifier.
        with mock.patch.object(self, "task", task):
            exc = self._trip(forged("diagnostic", source="load_plan", ok=False, code="table_uncovered",
                                    subject="sales-2021", flags={}, names=[]))
            self.assertEqual((exc.detector, exc.code), ("schema", "identifier_not_public"))
            exc = self._trip(forged("diagnostic", source="load_plan", ok=False, code="table_uncovered",
                                    subject="sales-2020", flags={"q1-2020": True}, names=[]))
            self.assertEqual(exc.detector, "schema")  # a flag key is a code, never a name
            exc = self._trip(forged("diagnostic", source="load_plan", ok=False, code="table_uncovered",
                                    subject="sales-2020", flags={"n2020": True}, names=[]))
            self.assertEqual((exc.detector, exc.code), ("numbers", "numeric_value"))
            exc = self._trip(forged("diagnostic", source="gate", ok=False, code="failed",
                                    subject="required-mutants", flags={},
                                    names=["proposed__shortcut_attacker-01-e1417a5a"]))
            self.assertEqual((exc.detector, exc.code), ("schema", "identifier_not_public"))
        # The masker consults the public set only: a non-public word keeps
        # its digits, a public one loses them, a bare number is untouched.
        masked = PJ._mask_public_identifiers("sales-2020 sales-2021 4711", public)
        self.assertEqual(masked, "xxxxxxxxxx sales-2021 4711")
        self.assertIsNotNone(PJ._STANDALONE_NUMBER_RE.search(masked))

    def test_batch_attack_case_names_pass_both_halves(self):
        """Batch evidence (runs/authorized_batch_50_20260908, read-only): every
        attack case name of every task IR — 32 of them carry the standalone
        digit shape `-NN-` — travels in a required-mutants row through the
        projector and the gatekeeper. Skipped when the batch is not on disk."""
        irs = sorted(BATCH_TASKS.glob("*/task_ir.json")) if BATCH_TASKS.is_dir() else []
        if not irs:
            self.skipTest("batch evidence not on disk")
        shaped = 0
        for path in irs:
            task = TaskIR.model_validate(json.loads(path.read_bytes()))
            for case in task.attack_cases:
                if PJ._STANDALONE_NUMBER_RE.search(case.name):
                    shaped += 1
                diag = PJ.Diagnostic(
                    source=PJ.DiagnosticSource.GATE, ok=False, code="failed",
                    subject="required-mutants", names=(case.name,),
                    flags={"leaked_somewhere": True},
                )
                payload = wire(diag, task)
                PJ.assert_value_free(payload, task=task, route=None)
                self.assertIn(case.name, json.loads(payload)["names"])
        self.assertGreater(shaped, 0, "the batch's proposed cases carry the -NN- shape")

    def test_sanitizer_rejects_duckdb_error_text_and_paths(self):
        exc = self._trip(text_payload("Binder Error: Referenced column 'x' not found in FROM clause"))
        self.assertEqual((exc.detector, exc.code), ("paths_and_secrets", "executor_text"))
        exc = self._trip(text_payload("see /Users/nobody/runs/ws/tasks/t/answer_key/gold/primary.duckdb"))
        self.assertEqual((exc.detector, exc.code), ("paths_and_secrets", "path"))
        exc = self._trip(text_payload("populations/counterfactual/rows/orders.jsonl is missing"))
        self.assertEqual(exc.code, "path")
        exc = self._trip(text_payload("key AKIAABCDEFGHIJKLMNOP leaked"))
        self.assertEqual(exc.code, "secret_literal")
        exc = self._trip(text_payload("token sk-ant-api03-abcdefghijklmnop"))
        self.assertEqual(exc.code, "secret_literal")
        # Unknown kinds, versions and non-canonical bytes are schema trips.
        self.assertEqual(self._trip(b"not json").code, "not_json")
        self.assertEqual(self._trip(forged("tool_output", source="gate")).code, "unknown_kind")
        payload = canonical_json({"kind": "diagnostic", "diagnostics_version": "0",
                                  "source": "gate", "ok": True, "code": "ok",
                                  "subject": "", "flags": {}, "names": []}).encode()
        self.assertEqual(self._trip(payload).code, "diagnostics_version_mismatch")
        # The CURRENT version (the literal moved with the Phase 3 bump, SoT
        # T7): a version match reaches the canonical-bytes check.
        spaced = (
            b'{"kind": "diagnostic", "diagnostics_version": "' + PJ.DIAGNOSTICS_VERSION.encode()
            + b'", "source": "gate", "ok": true, "code": "ok", "subject": "", "flags": {}, "names": []}'
        )
        self.assertEqual(self._trip(spaced).code, "not_canonical")
        # Private SQL through the council's definition: a reference shingle trips
        # on the specification route and is the legitimate subject on REFERENCE.
        reference_sql = next(iter(self.task.reference.sql_by_mart.values()))
        shingle = " ".join(reference_sql.split()[:12])
        exc = self._trip(text_payload(f"prose contains {shingle}"), route=RepairRoute.SPECIFICATION)
        self.assertEqual(exc.detector, "private_material")

    def test_dev_rows_exempt_from_numbers_but_not_paths(self):
        rows = PJ.DevRows(columns=("customer_id", "total_spend"), rows=((7, 30.0), ("8", "42")))
        payload = wire(rows, self.task)
        PJ.assert_value_free(payload, task=self.task, route=None)
        leaky = PJ.DevRows(columns=("customer_id",), rows=(("/Users/nobody/answer_key/gold.csv",),))
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.serialize_for_transport(leaky, task=self.task)
        self.assertEqual(ctx.exception.code, "path")
        with self.assertRaises(ValidationError):
            PJ.DevRows(columns=("a",), rows=tuple((i,) for i in range(PJ.MAX_DEV_ROWS + 1)))

    def test_dev_rows_exempt_from_count_canary_but_not_secret_or_path_detectors(self):
        """A23 / OQ-23 option C: `DevRows` are the ONE projection exempt from
        the gold-count canary (detector 3) — a cell equal to a hidden-population
        frozen count or a DEVELOPMENT count passes, because the DEVELOPMENT rows
        are solver-visible by construction — but a path or a secret in a DEV
        cell still trips the path and secret detectors."""
        gold = synthetic_gold()
        scalars = PJ.private_scalars(self.task, gold)
        # A hidden-population frozen count (4711) as a DEV cell is NOT a canary.
        self.assertIn("4711", scalars)
        rows = PJ.DevRows(columns=("customer_id", "total_spend"), rows=((4711, 30.0), (2, 8.0)))
        PJ.assert_value_free(wire(rows, self.task, gold), task=self.task, route=None)
        # The DEVELOPMENT stage-1 counts (2/4/8) are not private scalars at all.
        for dev_count in gold.stage1["development"].values():
            dev_rows = PJ.DevRows(columns=("completed_order_count",), rows=((dev_count,),))
            PJ.assert_value_free(wire(dev_rows, self.task, gold), task=self.task, route=None)
        # A path or a secret in a DEV cell trips (the projector and gatekeeper).
        for cell, code in (
            ("see /Users/x/answer_key/gold/primary.csv", "path"),
            ("AKIAABCDEFGHIJKLMNOP", "secret_literal"),
        ):
            leaky = PJ.DevRows(columns=("customer_id",), rows=((cell,),))
            with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
                PJ.serialize_for_transport(leaky, task=self.task, package=gold)
            self.assertEqual(ctx.exception.code, code)
            forged_bytes = forged("dev_rows", columns=["customer_id"], rows=[[cell]], truncated=False)
            self.assertEqual(self._trip(forged_bytes).code, code)

    def test_private_scalars_are_route_aware_on_population(self):
        """ONE definition of private for the projector and the views (Phase 0
        review finding 3): on the POPULATION route the DECLARED scale hints
        (`populations.*.scale`, editable IR fields the view prints as
        `table~scale`) are permitted; realized counts (literal constructed
        rows), frozen stage-1 counts and gold mart row counts stay private on
        every route, and a code-only Diagnostic never carries a number on
        any route (the gatekeeper holds no scalar set and refuses by shape)."""
        from elt_taskgen.models import PopulationName

        gold = synthetic_gold()
        hidden = [p for p in self.task.populations if p.name is not PopulationName.DEVELOPMENT]
        declared = {str(int(v)) for p in hidden for v in p.scale.values()}
        literal = {str(len(rows)) for p in hidden for rows in p.literal_rows.values()}
        # OQ-23 option C re-pin: the DEVELOPMENT stage-1 counts are NOT private
        # scalars (solver-visible by construction). `frozen` is the HIDDEN
        # populations' stage-1 counts plus the gold mart row count (37); the
        # DEVELOPMENT stage-1 counts (2/4/8 in synthetic_gold) are excluded.
        frozen = {
            str(v)
            for pop, counts in gold.stage1.items()
            if pop != PopulationName.DEVELOPMENT.value
            for v in counts.values()
        } | {"37"}
        self.assertTrue(declared and literal)
        self.assertEqual(PJ.private_scalars(self.task), frozenset(declared | literal))
        self.assertEqual(PJ.private_scalars(self.task, gold), frozenset(declared | literal | frozen))
        for route in (None, RepairRoute.SPECIFICATION, RepairRoute.REFERENCE):
            self.assertEqual(
                PJ.private_scalars(self.task, gold, route=route),
                frozenset(declared | literal | frozen),
                str(route),
            )
        self.assertEqual(
            PJ.private_scalars(self.task, gold, route=RepairRoute.POPULATION),
            frozenset(literal | frozen),
        )
        self.assertEqual(PJ.private_scalars(self.task, route=RepairRoute.POPULATION), frozenset(literal))

        # A declared scale that is no frozen count passes the projector's canary
        # on the POPULATION route only; every realized or frozen number trips
        # there too.
        editable = sorted(declared - frozen - literal, key=int)
        self.assertTrue(editable)
        scale = editable[0]

        def text(number):
            return PJ.DiagnosticText(
                source="prose", code="not_represented", subject=MART,
                text=f"mart '{MART}': description mentions {number} somewhere",
            )

        PJ.serialize_for_transport(text(scale), task=self.task, package=gold, route=RepairRoute.POPULATION)
        for route in (None, RepairRoute.SPECIFICATION, RepairRoute.REFERENCE):
            with self.assertRaises(PJ.DiagnosticTripwire, msg=str(route)) as ctx:
                PJ.serialize_for_transport(text(scale), task=self.task, package=gold, route=route)
            self.assertEqual(ctx.exception.code, "private_scalar")
        for private in sorted(frozen | literal, key=int):
            with self.assertRaises(PJ.DiagnosticTripwire, msg=private) as ctx:
                PJ.serialize_for_transport(
                    text(private), task=self.task, package=gold, route=RepairRoute.POPULATION
                )
            self.assertEqual(ctx.exception.code, "private_scalar")
        # A code-only Diagnostic never carries a number on ANY route, and the
        # gatekeeper refuses it by shape on every route, scalar set or not.
        for route in (None, RepairRoute.POPULATION):
            with self.assertRaises(PJ.DiagnosticTripwire):
                PJ.serialize_for_transport(
                    PJ.Diagnostic(source="gate", ok=False, code="failed", subject="determinism",
                                  flags={f"n{scale}": True}),
                    task=self.task, package=gold, route=route,
                )
            exc = self._trip(
                forged("diagnostic", source="gate", ok=False, code="failed",
                       subject="determinism", flags={f"n{scale}": True}, names=[]),
                route=route,
            )
            self.assertEqual((exc.detector, exc.code), ("numbers", "numeric_value"))

    def test_tripwire_is_a_harness_fault_class(self):
        exc = PJ.DiagnosticTripwire("canary", "private_scalar", source="prose",
                                    payload_sha256="ab" * 32, quarantined=b"4711")
        self.assertEqual(exc.boundary, "sanitizer")
        self.assertIs(exc.failure_class, WorkspaceFailureClass.HARNESS_DEFECT)
        self.assertFalse(exc.failure_class.label_eligible)  # reward None, never 0.0
        self.assertEqual(engine_mod._infra_marker_for(exc), "DiagnosticTripwire")
        self.assertNotIn("4711", str(exc))
        self.assertEqual(exc.quarantined, b"4711")
        self.assertIn("harness fault", str(exc))


class ProjectorTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_text_allowlisted_producers_have_public_inputs(self):
        seen: set[str] = set()
        task = self.task.model_copy(update={"solver_prompt": ""})

        class Recorder:
            def __getattr__(self, name):
                seen.add(name)
                return getattr(task, name)

        problems = prose_fidelity.check_prose_fidelity(Recorder())
        self.assertTrue(problems)
        declarative_prose.check_declarative_prose(Recorder())
        allowed = PJ.TEXT_PRODUCER_PUBLIC_FIELDS[PJ.DiagnosticSource.PROSE]
        self.assertTrue(seen)
        self.assertLessEqual(seen, allowed, seen - allowed)
        # The public fields never include a private one.
        for private in ("reference", "attack_cases", "populations", "relationships"):
            self.assertNotIn(private, allowed)
        # ... and the projection of those problems passes both halves.
        diags = PJ.project_prose_problems(problems, task=task)
        self.assertEqual(len(diags), len(problems))
        for diag in diags:
            self.assertEqual(diag.code, "empty_prose")
            self.assertEqual(diag.subject, MART)
            PJ.assert_value_free(wire(diag, task), task=task, route=None)

    def test_project_prose_problems_carries_public_identifiers_only(self):
        good = PJ.project_prose_problems(
            [f"mart '{MART}': output column 'total_spend' never mentioned"], task=self.task
        )
        self.assertEqual((good[0].code, good[0].subject, good[0].names), ("missing_object", MART, ("total_spend",)))
        PJ.assert_value_free(wire(good[0], self.task), task=self.task, route=None)
        reference_sql = next(iter(self.task.reference.sql_by_mart.values()))
        leak = PJ.project_prose_problems(
            [f"mart '{MART}': prose restates {' '.join(reference_sql.split()[:14])}"], task=self.task
        )
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.serialize_for_transport(leak[0], task=self.task)
        self.assertEqual(ctx.exception.detector, "private_material")
        counts = PJ.project_prose_problems([f"mart '{MART}': expected 3000 rows, got 2999"], task=self.task)
        with self.assertRaises(PJ.DiagnosticTripwire):
            PJ.serialize_for_transport(counts[0], task=self.task)
        # An identifier-looking token that is not a public name (a CTE, a
        # hidden table, a private field path) trips in BOTH halves, while the
        # public op-kind and column-type vocabulary in a rule label passes.
        private_name = PJ.project_prose_problems(
            [f"mart '{MART}': rule 2 [tie_break] mentions completed_orders_cte"], task=self.task
        )
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.serialize_for_transport(private_name[0], task=self.task)
        self.assertEqual((ctx.exception.detector, ctx.exception.code), ("schema", "identifier_not_public"))
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            PJ.assert_value_free(
                text_payload(f"mart '{MART}': rule 2 [tie_break] mentions completed_orders_cte"),
                task=self.task, route=None,
            )
        self.assertEqual(ctx.exception.code, "identifier_not_public")
        fine = PJ.project_prose_problems(
            [f"mart '{MART}': rule 8 [tie_break] ('sort by customer_id') not represented "
             "(closest passage is missing: order, integer)"],
            task=self.task,
        )
        PJ.assert_value_free(wire(fine[0], self.task), task=self.task, route=None)

    def test_project_compile_never_carries_the_mutation(self):
        case = self.task.attack_cases[0]
        diag = PJ.project_compile((case,))
        self.assertTrue(diag.ok)
        self.assertEqual((diag.code, diag.subject), ("compiles", case.kind.value))
        self.assertIn("is_directive", diag.flags)
        text = PJ.serialize_for_transport(diag, task=self.task)
        self.assertNotIn(case.mutation.strip()[:20], text)
        PJ.assert_value_free(text.encode("utf-8"), task=self.task, route=None)
        none = PJ.project_compile(())
        self.assertEqual((none.ok, none.code, none.subject), (False, "uncompilable", ""))
        directive = case.model_copy(
            update={"name": "probe_directive", "mutation": "directive: skip_extraction", "required": False}
        )
        self.assertIsInstance(directive, AttackCase)
        self.assertTrue(PJ.project_compile((directive,)).flags["is_directive"])

    def test_project_promotion_withholds_rewards_and_reasons(self):
        base = {
            "finding_id": "pop-1", "case_name": "proposal__pop-1", "kind": "inner_join",
            "predicted": {"development": True, "primary": False, "counterfactual": False},
            "measured": {"development": 1.0, "primary": 1.0, "counterfactual": 0.0},
            "measured_pass": {"development": True, "primary": True, "counterfactual": False},
            "fidelity": {"passed": True, "checks": ["x"]},
        }
        record = PJ.project_promotion({**base, "promoted": False,
                                       "reason": "measured reward matrix does not match the proposed expectation on 1 population(s)",
                                       "mismatches": ("primary: predicted fail, measured pass",)})
        self.assertEqual(record["code"], "mismatch")
        self.assertEqual(record["per_population"]["primary"], {"predicted": False, "measured_pass": True})
        self.assertTrue(record["fidelity_ok"])
        text = canonical_json(record)
        for withheld in ("1.0", "0.0", "reason", "mismatches", "measured\"", "checks"):
            self.assertNotIn(withheld, text, withheld)
        cases = {
            "proposal could not be executed: InertAstMutationError: mutant changed nothing on customer_summary": "inert",
            "proposal could not be executed: InapplicableLoadMutationError: no surface": "inapplicable",
            "proposal could not be executed: MutationFidelityError: claim": "fidelity_failed",
            "proposal could not be executed: ValueError: proposal param 'x' unknown": "uncompilable",
            "proposal could not be executed: SomethingElse: ?": "unknown_kind",
            "proposal was confirmed to keep FULL combined reward on all five populations": "no_kill_predicted",
        }
        for reason, code in cases.items():
            got = PJ.project_promotion({**base, "promoted": False, "reason": reason})
            self.assertEqual(got["code"], code, reason)
        self.assertEqual(PJ.project_promotion({**base, "promoted": True, "reason": "ok"})["code"], "promoted")
        diag = PJ.project("promotion", {**base, "promoted": False, "reason": "x"}, task=self.task)
        PJ.assert_value_free(wire(diag, self.task), task=self.task, route=None)

    def test_project_gate_battery_and_dual_build_are_code_only(self):
        rows = PJ.project_gate_battery({"gates": [
            {"gate": "determinism", "passed": True, "details": "3/3 identical"},
            {"gate": "info-content", "passed": False, "details": "gate crashed (fail closed): BinderError('x')"},
            {"gate": "referential-integrity", "passed": False, "details": "key ('c_9001', 3) has no parent row"},
        ]})
        self.assertEqual(rows, [
            {"gate": "determinism", "passed": True, "code": "ok"},
            {"gate": "info-content", "passed": False, "code": "gate_crashed"},
            {"gate": "referential-integrity", "passed": False, "code": "failed"},
        ])
        self.assertEqual(PJ.project_gate_battery(None), [])
        self.assertEqual(PJ.project_gate_battery({"detail": "two gates red"}), [])
        with self.assertRaises(PJ.DiagnosticTripwire):
            PJ.project_gate_battery({"gates": [{"gate": "primary=1.0", "passed": False}]})
        adj = PJ.project_dual_build({"status": "needs_adjudication",
                                     "detail": "primary: expected 3000 rows, got 2999",
                                     "agreement": {"primary": 0.0}})
        self.assertEqual((adj.ok, adj.code, adj.subject), (False, "mismatch", ""))
        self.assertEqual(PJ.project_dual_build(None).code, "agreed")
        self.assertEqual(PJ.project_dual_build({"status": "x", "detail": "BinderError raised"}).code, "execution_error")
        self.assertEqual(PJ.project_dual_build({"status": "x", "detail": "cannot parse"}).code, "parse_error")
        self.assertEqual(PJ.project_dual_build({"status": "x", "detail": ""}).code, "unclassified")


class CertifyProjectionTest(unittest.TestCase):
    """`project_certify` (certify addendum §3.1; roadmap Phase 1): green or the
    first red provider-free STAGE, two booleans, nothing else."""

    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_certify_projection_returns_green_and_stage_only(self):
        from elt_taskgen.engine import Engine, StageOutcome, StagePayload, VERDICT_FAIL, VERDICT_PASS
        from elt_taskgen.review.tools import certify as C
        from elt_taskgen.verification import gates as gates_mod

        self.assertEqual(
            PJ.CERTIFY_CODES,
            (
                "certify_green",
                "certify_red_generate",
                "certify_red_reference",
                "certify_refused_no_provider_free_stage",
                "certify_refused_unchanged_trial",
                # The Phase 3 re-check verdict's PAID resource-budget outcome
                # (docs/plans/bounded_agents_phase3.md §6).
                "certify_refused_resource_budget",
            ),
        )
        self.assertEqual(PJ.codes_for("certify"), frozenset(PJ.CERTIFY_CODES))
        self.assertEqual(PJ.CERTIFY_FLAGS, ("model_stages_deferred", "discrimination_weakened"))
        # Phase 3 members are NOT in the Phase 1 vocabulary.
        for phase_3 in ("certify_red_attack", "certify_refused_review_view_changed"):
            self.assertNotIn(phase_3, PJ.CERTIFY_CODES)
            with self.assertRaises(ValueError):
                PJ.project_certify(phase_3, model_stages_deferred=True)
        gate_names = set(gates_mod.GATE_NAMES) | {
            n for roster in gates_mod.VARIANT_GATE_NAMES.values() for n in roster
        }
        for code in PJ.CERTIFY_CODES:
            for deferred in (False, True):
                with self.subTest(code=code, deferred=deferred):
                    diag = PJ.project_certify(code, model_stages_deferred=deferred)
                    self.assertEqual(diag.source, PJ.DiagnosticSource.CERTIFY)
                    self.assertEqual(diag.code, code)
                    self.assertEqual(diag.ok, code == "certify_green")
                    self.assertEqual(diag.subject, "")
                    self.assertEqual(diag.names, ())
                    self.assertEqual(
                        dict(diag.flags),
                        {"model_stages_deferred": deferred, "discrimination_weakened": False},
                    )
                    text = diag.render()
                    self.assertFalse(gate_names & set(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*", text)), text)
                    self.assertIsNone(re.search(r"\d", text))
                    PJ.assert_value_free(wire(diag, self.task), task=self.task, route=None)
                    PJ.assert_value_free(
                        wire(diag, self.task), task=self.task, route=RepairRoute.REFERENCE
                    )
        # Phase 1 pins the guard's bit to False: asking for True is refused.
        with self.assertRaises(ValueError):
            PJ.project_certify("certify_green", model_stages_deferred=False, discrimination_weakened=True)
        # `project` dispatches the source too.
        via_project = PJ.project(
            "certify", {"code": "certify_red_generate", "model_stages_deferred": True}, task=self.task
        )
        self.assertEqual((via_project.code, via_project.ok, via_project.flags["model_stages_deferred"]), ("certify_red_generate", False, True))

        # A REAL red run: a `reference` runner whose payload is full of gate
        # names and numbers projects to the STAGE code and nothing of the payload.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            engine = Engine(workspace)
            try:
                engine.register(self.task)
            finally:
                engine.close()
            leaky = " ".join(
                f"{gate} failed: primary=0.25 expected 4711 rows, got 4710" for gate in sorted(gate_names)[:3]
            )

            def red_reference(engine, task):
                return StageOutcome(VERDICT_FAIL, StagePayload(error=leaky, data={"agreement": "0.5"}))

            def green(engine, task):
                return StageOutcome(VERDICT_PASS, StagePayload(detail="generate ok"))

            red = C.run_provider_free_stages(
                workspace, self.task.task_id, ("generate", "reference"),
                runners={"generate": green, "reference": red_reference}, model_stages_deferred=True,
            )
            self.assertEqual((red.code, red.ok), ("certify_red_reference", False))
            self.assertEqual(dict(red.flags), {"model_stages_deferred": True, "discrimination_weakened": False})
            self.assertEqual((red.subject, red.names), ("", ()))
            rendered = red.render()
            for gate in gate_names:
                self.assertNotIn(gate, rendered)
            self.assertNotIn("4711", rendered)
            PJ.assert_value_free(wire(red, self.task), task=self.task, route=RepairRoute.REFERENCE)
            # ... and the copy's ledger carries the rows the members wrote.
            ledger = Engine(workspace)
            try:
                self.assertEqual(ledger.latest_report(self.task.task_id, "generate").verdict, VERDICT_PASS)
                self.assertEqual(ledger.latest_report(self.task.task_id, "reference").verdict, VERDICT_FAIL)
            finally:
                ledger.close()


@dataclass
class StubTool:
    name: str = "check_scope"
    description: str = "dry-run validate_scope"
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}, "required": [], "additionalProperties": False})
    cost: RG.ToolCost = field(default_factory=RG.ToolCost)
    permitted_roles: frozenset = frozenset({"repair_proposer"})

    def run(self, ctx, args):
        return PJ.Diagnostic(source="gate", ok=True, code="ok", subject="determinism")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "attempt"
        self.root.mkdir()
        self.ctx = RG.ToolContext(root=self.root, task_id=self.task.task_id, role="repair_proposer")

    def test_registry_matches_shipped_role_defaults(self):
        expected = {
            "semantic_author": (
                "abort", "check_prose", "contamination_precheck", "replace_prose", "submit_prose",
            ),
            "population_adversary": ("compile_proposal",),
            "shortcut_attacker": ("compile_probe",),
            "independent_loader": (
                "abort", "check_load_plan", "replace_load_plan", "submit_load_plan",
            ),
            "independent_implementer": (
                "abort", "dev_query", "dry_run_sql", "list_schemas", "run_mart_sql_dev",
                "submit_sql_by_mart",
            ),
            "repair_proposer": (
                "abort", "apply_edit_trial", "certify", "check_cheap", "check_scope",
                "read_field", "read_view", "submit_patch",
            ),
        }
        expected_wire = {
            "semantic_author": ("abort", "submit_prose"),
            "independent_loader": ("abort", "replace_load_plan", "submit_load_plan"),
            "independent_implementer": expected["independent_implementer"],
            "repair_proposer": expected["repair_proposer"],
        }
        for role in ROLES:
            registry = RG.ToolRegistry.for_role(role)
            self.assertEqual(registry.names, expected.get(role, ()), role)
            self.assertEqual(
                tuple(tool["name"] for tool in registry.wire_tools()),
                expected_wire.get(role, ()),
                role,
            )
            self.assertEqual(len(registry.manifest_sha256()), 64)
            with self.assertRaises(RG.ToolLookupError) as ctx:
                # A verb NO role registers is `unknown_tool` (a declared verb of
                # another role — `dev_query` since Phase 2 — is `tool_not_permitted`).
                registry.dispatch(
                    RG.ToolContext(root=self.root, task_id="t", role=role), "no_such_verb", {}
                )
            self.assertEqual(ctx.exception.kind, "unknown_tool")
        # Two disabled roles with the same empty manifest still digest per role.
        self.assertNotEqual(
            RG.ToolRegistry.for_role("ambiguity_critic").manifest_sha256(),
            RG.ToolRegistry.for_role("feasibility_reviewer").manifest_sha256(),
        )

    def test_context_free_stub_result_passes_assert_value_free_on_demo_task(self):
        # Enabled tools are session-bound and are exercised through their
        # real lifecycle in the role-specific validator suites. Disabled
        # one-shot roles expose no accidentally dispatchable tool here.
        checked = 0
        for role in ("ambiguity_critic", "feasibility_reviewer", "audit_triage"):
            registry = RG.ToolRegistry.for_role(role)
            for name in registry.names:
                result = registry.dispatch(
                    RG.ToolContext(root=self.root, task_id=self.task.task_id, role=role), name, {}
                )
                PJ.assert_value_free(wire(result, self.task), task=self.task, route=None)
                checked += 1
        self.assertEqual(checked, 0)
        # A context-free stub exercises the projection boundary itself.
        registry = RG.ToolRegistry("repair_proposer", [StubTool()])
        result = registry.dispatch(self.ctx, "check_scope", {})
        PJ.assert_value_free(wire(result, self.task), task=self.task, route=None)

    def test_no_tool_accepts_a_population_argument(self):
        self.assertEqual(RG.ToolContext.population, "development")
        self.assertNotIn("population", inspect.signature(RG.ToolContext).parameters)
        for role in ROLES:
            for name in RG.ToolRegistry.for_role(role).names:
                schema = RG.ToolRegistry.for_role(role).get(name).input_schema
                self.assertNotIn("population", schema.get("properties", {}), f"{role}:{name}")

    def test_no_tool_has_free_path_or_url_argument(self):
        banned = {"path", "url", "uri", "file", "filename", "href"}
        for role in ROLES:
            for name in RG.ToolRegistry.for_role(role).names:
                schema = RG.ToolRegistry.for_role(role).get(name).input_schema
                self.assertFalse(banned & set(schema.get("properties", {})), f"{role}:{name}")

    def test_wire_tools_match_the_payload_tool_shape(self):
        registry = RG.ToolRegistry("repair_proposer", [StubTool()])
        wire_tool = registry.wire_tools()[0]
        payload = P.AnthropicBackend._payload(
            model="claude-opus-5", prompt="p", max_tokens=16, effort=None,
            role_name="ambiguity_critic", schema_mode=True,
        )
        self.assertEqual(list(wire_tool), list(payload["tools"][0]))
        self.assertEqual(wire_tool["strict"], True)
        self.assertNotEqual(registry.manifest_sha256(), RG.ToolRegistry.for_role("repair_proposer").manifest_sha256())
        with self.assertRaises(ValueError):
            RG.ToolRegistry("semantic_author", [StubTool()])  # not permitted for that role
        with self.assertRaises(RG.ToolLookupError) as ctx:
            registry.dispatch(RG.ToolContext(root=self.root, task_id="t", role="semantic_author"), "check_scope", {})
        self.assertEqual(ctx.exception.kind, "role_mismatch")

    def test_tool_context_root_never_under_runs(self):
        """A22: the root may never be, or lie under, the REPOSITORY's `runs/`
        (`workspace.repo_root() / DEFAULT_WORKSPACE.parent`, by resolved
        path). The anchor is the checkout, not a path component: an unrelated
        `/home/ci/runs/attempt` is an ordinary root. A credential-shaped root
        and a release root are refused everywhere."""
        from elt_taskgen import workspace as workspace_mod

        self.assertEqual(workspace_mod.DEFAULT_WORKSPACE.parent, Path("runs"))
        self.assertEqual(RG.repository_runs_root(), (repo_root() / "runs").resolve())
        for root in (repo_root() / "runs" / "dbt_elt", repo_root() / "runs"):
            with self.assertRaises(RG.ToolPathDenied, msg=str(root)):
                RG.ToolContext(root=root, task_id="t", role="r")
        # The temporary tree is unrelated to the checkout, so its `runs` is
        # an ordinary root ... until the temporary tree IS the checkout.
        unrelated = self.root / "runs" / "ws"
        self.assertEqual(
            RG.ToolContext(root=unrelated, task_id="t", role="r").root, unrelated.resolve()
        )
        with mock.patch.object(workspace_mod, "repo_root", lambda: self.root):
            self.assertEqual(RG.repository_runs_root(), (self.root / "runs").resolve())
            with self.assertRaises(RG.ToolPathDenied):
                RG.ToolContext(root=unrelated, task_id="t", role="r")
            with self.assertRaises(RG.ToolPathDenied):
                RG.ToolContext(root=self.root / "runs", task_id="t", role="r")
            elsewhere = self.root / "home" / "ci" / "runs" / "attempt"
            self.assertEqual(
                RG.ToolContext(root=elsewhere, task_id="t", role="r").root, elsewhere.resolve()
            )
            self.assertFalse(RG.path_under_runs(elsewhere))
            # Resolved-path equality, not string prefixes or components.
            self.assertTrue(RG.path_under_runs(self.root / "runs" / "x" / ".." / "y"))
            self.assertFalse(RG.path_under_runs(self.root / "runs" / ".."))
            self.assertFalse(RG.path_under_runs(self.root / "runs2" / "ws"))
        with self.assertRaises(RG.ToolPathDenied):
            RG.ToolContext(root=self.root / "snowflake_credential.json", task_id="t", role="r")
        release = self.root / "release"
        (release / "public").mkdir(parents=True)
        (release / "release_manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(RG.ToolPathDenied):
            RG.ToolContext(root=release / "public", task_id="t", role="r")
        ctx = RG.ToolContext(root=self.root, task_id="t", role="r")
        self.assertTrue(ctx.root.is_absolute())
        self.assertNotIn("runs", ctx.root.parts)

    def test_tool_path_policy_denies_runs_and_credential_files(self):
        for denied in (
            "runs/ws/tasks/t/task_ir.json",
            "task/snowflake_credential.json",
            "databricks_credential.json",
            "task/answer_key/gold/primary.csv",
            "private/t/semantic/task_ir.json",
            "populations/primary/rows/orders.jsonl",
            "attacks/inner_join/rewards.json",
            "oracle/primary.duckdb",
            "task/.workspace-runtime/raw/attempt.duckdb",
            "task/elt/terraform.tfstate",
            "task/elt/profiles.yml",
            "../outside.txt",
            "/etc/passwd",
            "~/.ssh/id_rsa",
            "",
        ):
            with self.assertRaises(RG.ToolPathDenied, msg=denied):
                RG.check_tool_path(self.ctx, denied)
        allowed = RG.check_tool_path(self.ctx, "task/elt/main.tf")
        self.assertEqual(allowed, (self.root / "task" / "elt" / "main.tf").resolve())
        # The context root is RESOLVED (macOS temp dirs live under /private/var).
        self.assertIn(self.ctx.root, allowed.parents)
        # A symlink that escapes the root is refused by RESOLVED-path equality.
        (self.root / "task").mkdir(exist_ok=True)
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        (self.root / "task" / "link").symlink_to(outside)
        with self.assertRaises(RG.ToolPathDenied):
            RG.check_tool_path(self.ctx, "task/link/x.sql")
        # Every denied token of the policy is a lowercase stable component.
        for component in RG.DENIED_PATH_COMPONENTS:
            self.assertFalse(re.search(r"\s|/", component), component)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

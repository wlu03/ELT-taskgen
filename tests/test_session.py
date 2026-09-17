"""Tests for review/session.py — the bounded-session fault taxonomy (Phase 0.C).

WHY THIS EXISTS
C7: a harness fault must never become a task verdict. Before this taxonomy a
validator crash, a deadline, a sanitizer trip or a policy violation inside a
proposal was wrapped into a marker-less FAIL row and SPENT A REPAIR ROUND — a
transient fault could reject a task. These tests pin, at the class level and
through the engine, that every harness-side fault halts with exit 2 / reward
`None` / no round / no rejection, that every model-caused fault is the
label-eligible family and never infrastructure, and that a limit-stopped
session without a green draft WAITS (`VERDICT_BLOCKED` with a salt, at most
two salted re-runs) instead of failing. Names follow the SoT (T4, T6) exactly.
"""

import contextlib
import io
import json
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from elt_taskgen import cli
from elt_taskgen import engine as engine_mod
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    FINAL_IN_PROGRESS,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    Engine,
    InfrastructureFailure,
    StageOutcome,
    StagePayload,
)
from elt_taskgen.models import RepairRoute, TaskStatus, canonical_json
from elt_taskgen.review import providers as providers_mod
from elt_taskgen.review import repair_proposer as rp
from elt_taskgen.review import session as S
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG
from elt_taskgen.training.contract import WorkspaceFailureClass as WFC
from elt_taskgen.training.models import _CODE_RE

#: What a leaky validator would say: executor text, a gold count, a private path.
SENTINEL = "Binder Error: column 4711 not found in /Users/nobody/answer_key/gold.csv"
SENTINEL_TOKENS = ("4711", "Binder", "answer_key", "gold.csv")

PROSE = (
    "Build the customer_summary mart: one row per customer, with the number of "
    "completed orders and the total completed order value."
)


class _StallOnUnpickle:
    """Pickles as a call that stalls the CHILD while it rebuilds its arguments,
    so the projector worker never answers: the parent's deadline is the only
    thing that can end it."""

    def __reduce__(self):
        return (time.sleep, (30.0,))


class RecordingProvider:
    """Records every call; optionally raises INSTEAD of answering (what the D1
    gatekeeper inside a transport does when it refuses to send)."""

    def __init__(self, responses=(), raise_exc=None):
        self._responses = list(responses)
        self.raise_exc = raise_exc
        self.calls = []

    def complete(self, role, prompt: str) -> str:
        self.calls.append((role, prompt))
        if self.raise_exc is not None:
            raise self.raise_exc
        if not self._responses:
            raise AssertionError("RecordingProvider ran out of responses")
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return str(item)


class BlockedProposer:
    """A bounded proposer whose session stopped at a limit without a
    validator-green draft: `disposition == "blocked_limit"`."""

    def __init__(self, limit: str = "turns", scope: str = ""):
        self.limit = limit
        self.scope = scope
        self.calls = []

    def repair(self, engine, task, stage, route, failure):
        # A bounded proposer reads its salt off the ledger (the count of
        # salted re-runs already scheduled at this identity).
        self.calls.append((stage, engine.session_limit_reruns(task.task_id, stage)))
        record = rp.RepairAttemptRecord(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            stage=stage,
            route=RepairRoute(route).value,
            status=rp.DISPOSITION_BLOCKED_LIMIT,
            committed=False,
            detail=f"session stopped at max_{self.limit} without a green draft",
        )
        return rp.RepairOutcome(
            record=record,
            disposition=rp.DISPOSITION_BLOCKED_LIMIT,
            limit=self.limit,
            limit_scope=self.scope,
        )


class RaisingProposer:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def repair(self, engine, task, stage, route, failure):
        self.calls += 1
        raise self.exc


@dataclass
class CrashingTool:
    name: str = "check_scope"
    description: str = "a validator that crashes with executor text"
    input_schema: dict = field(
        default_factory=lambda: {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False
        }
    )
    cost: RG.ToolCost = field(default_factory=RG.ToolCost)
    permitted_roles: frozenset = frozenset({"repair_proposer"})

    def run(self, ctx, args):
        raise RuntimeError(SENTINEL)


class RawTool(CrashingTool):
    def run(self, ctx, args):
        return {"rows": [(4711, "c_9001")]}


class PolicyTool(CrashingTool):
    def run(self, ctx, args):
        raise S.ForbiddenArgument(tool=self.name, detail="ATTACH")


class EngineCase(unittest.TestCase):
    """An engine whose `review` stage fails on the demo task (an undefined
    tie-break) so `_handle_failure` reaches the proposer, as in
    tests/test_repair_proposer.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "taskgen-workspace"
        self.task = demo_task().model_copy(update={"solver_prompt": PROSE})
        self.task_id = self.task.task_id
        self.review_calls = 0

    def review_fails(self, engine, task):
        self.review_calls += 1
        return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

    def make_engine(self, *, review=None, proposer=None, **kwargs) -> Engine:
        ok = lambda e, t: StageOutcome(VERDICT_PASS, StagePayload(detail="ok"))  # noqa: E731
        runners = {
            "contamination_pre": ok,
            "generate": ok,
            "reference": ok,
            "author": ok,
            "review": review or self.review_fails,
        }
        engine = Engine(
            self.workspace, stage_runners=runners, repair_proposer=proposer, **kwargs
        )
        self.addCleanup(engine.close)
        engine.register(self.task)
        ref = engine.task_dir(self.task_id) / "answer_key" / "reference"
        ref.mkdir(parents=True, exist_ok=True)
        (ref / "solution.sql").write_text("SELECT 1 AS placeholder_reference;\n", encoding="utf-8")
        return engine

    def review_rows(self, engine):
        return engine._con.execute(
            "SELECT verdict, payload_json FROM reports WHERE task_id=? AND stage='review'"
            " ORDER BY id",
            (self.task_id,),
        ).fetchall()

    def assert_halted_as_infrastructure(self, engine, marker: str, *, absent=()):
        """The C7 disposition at the engine: no round, no rejection, the
        marker on the FAIL row, and nothing quarantined in the ledger."""
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        rows = self.review_rows(engine)
        self.assertNotIn(VERDICT_FATAL, [verdict for verdict, _ in rows])
        verdict, payload_json = rows[-1]
        self.assertEqual(verdict, VERDICT_FAIL)
        payload = json.loads(payload_json)
        self.assertEqual(payload["infrastructure"], marker)
        for token in absent:
            self.assertNotIn(token, payload_json)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))


# ---------------------------------------------------------------------------
# The taxonomy itself
# ---------------------------------------------------------------------------

class TaxonomyTest(unittest.TestCase):
    def test_taxonomy_names_match_the_sot(self):
        self.assertEqual(
            S.INFRASTRUCTURE_FAULT_NAMES,
            {
                "SessionFault", "ProviderFault", "ToolHarnessFault",
                "ToolDeadlineExceeded", "SandboxFault", "DiagnosticTripwire",
                "TaskDefectFault",
            },
        )
        # Exactly the seven names are in the engine's set; `LeakTripwire` is
        # the same class and adds nothing; no policy name is ever there.
        self.assertTrue(S.INFRASTRUCTURE_FAULT_NAMES <= engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertNotIn("LeakTripwire", engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertFalse(S.POLICY_FAULT_NAMES & engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertEqual(S.SESSION_FAULT_BOUNDARIES, {"provider", "tool", "sandbox", "sanitizer", "task"})

        expected = {
            "ProviderFault": ("provider", WFC.TRANSIENT_INFRASTRUCTURE, "PROVIDER_FAULT"),
            "ToolHarnessFault": ("tool", WFC.HARNESS_DEFECT, "HARNESS_FAULT"),
            "ToolDeadlineExceeded": ("tool", WFC.TRANSIENT_INFRASTRUCTURE, "HARNESS_FAULT"),
            "SandboxFault": ("sandbox", WFC.TRANSIENT_INFRASTRUCTURE, "HARNESS_FAULT"),
            "DiagnosticTripwire": ("sanitizer", WFC.HARNESS_DEFECT, "LEAK_TRIPWIRE"),
            "TaskDefectFault": ("task", WFC.TASK_DEFECT, "HARNESS_FAULT"),
        }
        instances = {
            "ProviderFault": S.ProviderFault("transport after 4 retries", code="transport"),
            "ToolHarnessFault": S.ToolHarnessFault("check_scope"),
            "ToolDeadlineExceeded": S.ToolDeadlineExceeded("dev_query", deadline_s=10),
            "SandboxFault": S.SandboxFault("worker died", code="oom_killed"),
            "DiagnosticTripwire": S.DiagnosticTripwire("canary", "private_scalar"),
            "TaskDefectFault": S.TaskDefectFault("gold disagrees with the package"),
        }
        for name, (boundary, failure_class, terminal) in expected.items():
            cls = getattr(S, name)
            exc = instances[name]
            self.assertTrue(issubclass(cls, S.SessionFault), name)
            self.assertTrue(issubclass(cls, RuntimeError), name)
            self.assertFalse(issubclass(cls, S.PolicyFault), name)
            self.assertEqual(exc.boundary, boundary, name)
            self.assertIn(exc.boundary, S.SESSION_FAULT_BOUNDARIES)
            self.assertIs(exc.failure_class, failure_class, name)
            self.assertEqual(exc.terminal, terminal, name)
            self.assertFalse(exc.label_eligible, name)  # reward None, never 0.0
            # Typed by class, through the MRO: the engine needs no import.
            self.assertEqual(engine_mod._infra_marker_for(exc), name)
        # The base takes the boundary as its argument and refuses any other.
        self.assertEqual(S.SessionFault("x", boundary="task").boundary, "task")
        with self.assertRaises(ValueError):
            S.SessionFault("no boundary")
        with self.assertRaises(ValueError):
            S.SessionFault("x", boundary="model")

        # The model-caused family: one fixed code each, label-eligible, never
        # infrastructure.
        policy = {
            "ToolProtocolFault": S.ToolProtocolFault("unknown_tool", tool="dev_querry"),
            "ToolNotPermitted": S.ToolNotPermitted("dev_query"),
            "OracleCapExceeded": S.OracleCapExceeded(tool="certify"),
            "WriteOutsideSurface": S.WriteOutsideSurface(tool="apply_edit_trial"),
            "ForbiddenArgument": S.ForbiddenArgument(tool="dev_query", detail="ATTACH"),
        }
        self.assertEqual(set(policy) | {"PolicyFault"}, set(S.POLICY_FAULT_NAMES))
        for name, exc in policy.items():
            self.assertIsInstance(exc, S.PolicyFault)
            self.assertNotIsInstance(exc, S.SessionFault)
            self.assertTrue(exc.label_eligible, name)
            self.assertIsNotNone(_CODE_RE.fullmatch(exc.code), name)
            self.assertEqual(engine_mod._infra_marker_for(exc), "", name)
            self.assertEqual(rp.halting_marker(exc), "", name)
        self.assertFalse(policy["ToolProtocolFault"].ends_session)
        self.assertEqual(policy["ToolProtocolFault"].terminal, "PROTOCOL_EXHAUSTED")
        self.assertIs(policy["ToolProtocolFault"].failure_class, WFC.POLICY_FAILURE)
        self.assertEqual(S.PROTOCOL_FAULT_LIMIT, 3)
        self.assertEqual(
            S.PROTOCOL_FAULT_CODES,
            {"no_tool_call", "unknown_tool", "invalid_arguments", "multiple_tool_use"},
        )
        for name in ("ToolNotPermitted", "WriteOutsideSurface", "ForbiddenArgument"):
            self.assertEqual(policy[name].terminal, "POLICY_VIOLATION", name)
            self.assertIs(policy[name].failure_class, WFC.POLICY_VIOLATION, name)
            self.assertTrue(policy[name].security_event, name)
            self.assertTrue(policy[name].ends_session, name)
        self.assertEqual(policy["ToolNotPermitted"].code, "tool_not_permitted")
        self.assertEqual(policy["WriteOutsideSurface"].code, "write_outside_surface")
        self.assertEqual(policy["ForbiddenArgument"].code, "forbidden_argument")
        self.assertEqual(policy["OracleCapExceeded"].terminal, "LIMIT_ORACLE")
        self.assertFalse(policy["OracleCapExceeded"].security_event)
        self.assertEqual(policy["OracleCapExceeded"].code, "oracle_cap_exceeded")
        with self.assertRaises(ValueError):
            S.ToolProtocolFault("bogus_code")
        with self.assertRaises(ValueError):
            S.PolicyFault("Not A Code")
        with self.assertRaises(ValueError):  # a fixed code cannot be overridden
            S.PolicyFault.__init__(S.ToolNotPermitted("dev_query"), "other_code")

    def test_tripwire_is_a_session_fault_and_leak_tripwire_is_an_alias(self):
        self.assertIs(S.LeakTripwire, PJ.DiagnosticTripwire)
        self.assertIs(PJ.LeakTripwire, PJ.DiagnosticTripwire)
        self.assertIs(S.DiagnosticTripwire, PJ.DiagnosticTripwire)
        self.assertIn("LeakTripwire", S.__all__)
        self.assertIn("LeakTripwire", PJ.__all__)
        self.assertTrue(issubclass(PJ.DiagnosticTripwire, S.SessionFault))
        exc = PJ.DiagnosticTripwire(
            "canary", "private_scalar", source="prose", payload_sha256="ab" * 32,
            quarantined=b"4711",
        )
        self.assertEqual(exc.boundary, "sanitizer")
        self.assertEqual(exc.terminal, "LEAK_TRIPWIRE")
        self.assertEqual(exc.code, "private_scalar")
        self.assertEqual(exc.detector, "canary")
        self.assertIs(exc.failure_class, WFC.HARNESS_DEFECT)
        self.assertFalse(exc.label_eligible)
        self.assertEqual(exc.quarantined, b"4711")
        self.assertNotIn("4711", str(exc))
        self.assertEqual(engine_mod._infra_marker_for(exc), "DiagnosticTripwire")
        with self.assertRaises(AttributeError):
            S.NoSuchFault  # noqa: B018 - the lazy resolver only knows the alias

    def test_session_wrappers_are_provider_protocol_errors(self):
        exhausted = S.SessionProtocolError("repair_proposer", last_code="invalid_arguments")
        self.assertIsInstance(exhausted, ProviderProtocolError)
        self.assertEqual(exhausted.terminal, "PROTOCOL_EXHAUSTED")
        self.assertEqual(exhausted.faults, S.PROTOCOL_FAULT_LIMIT)
        self.assertEqual(engine_mod._infra_marker_for(exhausted), "ProviderProtocolError")
        with self.assertRaises(ValueError):
            S.SessionProtocolError("repair_proposer", faults=2)

        violation = S.SessionPolicyViolation(S.ToolNotPermitted("dev_query"), role="semantic_author")
        self.assertIsInstance(violation, ProviderProtocolError)
        self.assertEqual(violation.terminal, "POLICY_VIOLATION")
        self.assertTrue(violation.security_event)
        self.assertEqual(violation.code, "tool_not_permitted")
        self.assertEqual(engine_mod._infra_marker_for(violation), "ProviderProtocolError")
        # A limit is not a violation and a correction is not a violation.
        with self.assertRaises(TypeError):
            S.SessionPolicyViolation(S.OracleCapExceeded())
        with self.assertRaises(TypeError):
            S.SessionPolicyViolation(S.ToolProtocolFault("no_tool_call"))
        with self.assertRaises(TypeError):
            S.SessionPolicyViolation(RuntimeError("not a policy fault"))

        # Inside the proposer: the wrappers halt, a malformed PATCH does not
        # (that stays the schema retry's job — today's one-shot behaviour).
        self.assertEqual(rp.halting_marker(exhausted), "ProviderProtocolError")
        self.assertEqual(rp.halting_marker(violation), "ProviderProtocolError")
        self.assertEqual(rp.halting_marker(ProviderProtocolError("bad patch")), "")
        self.assertEqual(rp.halting_marker(S.ToolHarnessFault("t")), "ToolHarnessFault")
        self.assertEqual(rp.halting_marker(S.LeakTripwire("schema", "invalid_shape")), "DiagnosticTripwire")
        self.assertEqual(rp.halting_marker(providers_mod.BudgetExceededError("$")), "BudgetExceededError")
        self.assertEqual(rp.halting_marker(ValueError("a task defect")), "")
        self.assertEqual(
            rp.halting_marker(InfrastructureFailure("t", "review", "sandboxfault")), "sandboxfault"
        )


# ---------------------------------------------------------------------------
# Validator boundaries: crash, deadline, tripwire
# ---------------------------------------------------------------------------

class ValidatorBoundaryTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self._tmp.name) / "attempt"
        self.root.mkdir()
        self.ctx = RG.ToolContext(root=self.root, task_id=self.task_id, role="repair_proposer")

    def test_validator_crash_is_tool_harness_fault_and_output_is_not_sent(self):
        registry = RG.ToolRegistry("repair_proposer", [CrashingTool()])
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            registry.dispatch(self.ctx, "check_scope", {})
        fault = ctx.exception
        self.assertIsInstance(fault, S.SessionFault)
        self.assertNotIsInstance(fault, S.PolicyFault)
        self.assertEqual((fault.boundary, fault.tool, fault.code), ("tool", "check_scope", "harness_exception"))
        self.assertEqual(fault.cause_type, "RuntimeError")  # the CLASS, never the text
        for token in SENTINEL_TOKENS:
            self.assertNotIn(token, str(fault))
        self.assertIs(fault.failure_class, WFC.HARNESS_DEFECT)
        self.assertFalse(fault.label_eligible)
        self.assertEqual(engine_mod._infra_marker_for(fault), "ToolHarnessFault")
        # What a runner would record: the class name leads and nothing leaks.
        payload = StagePayload(error=f"{type(fault).__name__}: {fault}")
        self.assertEqual(engine_mod._infrastructure_failure(payload), "toolharnessfault")
        self.assertEqual(cli._transport_marker([payload.error]), "ToolHarnessFault")

        # A tool handing back a raw object is withheld the same way.
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            RG.ToolRegistry("repair_proposer", [RawTool()]).dispatch(self.ctx, "check_scope", {})
        self.assertEqual(ctx.exception.code, "non_projection_result")
        self.assertEqual(ctx.exception.cause_type, "dict")
        self.assertNotIn("4711", str(ctx.exception))
        # A fault a tool already typed by its own boundary passes through.
        with self.assertRaises(S.ForbiddenArgument):
            RG.ToolRegistry("repair_proposer", [PolicyTool()]).dispatch(self.ctx, "check_scope", {})
        # ... and the dispatcher's own refusals map onto the policy classes.
        registry = RG.ToolRegistry("repair_proposer", [CrashingTool()])
        # A name no role registers is a correctable unknown-tool protocol fault.
        with self.assertRaises(RG.ToolLookupError) as ctx:
            registry.dispatch(self.ctx, "no_such_tool_xyz", {})
        self.assertIsInstance(ctx.exception.as_policy_fault(), S.ToolProtocolFault)
        self.assertEqual(ctx.exception.as_policy_fault().code, "unknown_tool")
        # A name ANOTHER role registers (Phase 2 declares `dev_query` for the
        # implementer) is a terminal `tool_not_permitted`, not `unknown_tool`.
        with self.assertRaises(RG.ToolLookupError) as ctx:
            registry.dispatch(self.ctx, "dev_query", {})
        self.assertIsInstance(ctx.exception.as_policy_fault(), S.ToolNotPermitted)
        self.assertEqual(ctx.exception.kind, "tool_not_permitted")

        # The projector worker (D3): a crash in the child reports the
        # exception CLASS only — a str is not a gate, so `project` raises.
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            PJ.project_in_worker("gate", SENTINEL, task=self.task)
        worker_fault = ctx.exception
        self.assertEqual((worker_fault.tool, worker_fault.code), ("projector", "projector_exception"))
        self.assertEqual(worker_fault.cause_type, "TypeError")
        for token in SENTINEL_TOKENS:
            self.assertNotIn(token, str(worker_fault))

        # Through the engine: a harness fault, exit 2, no round, no rejection,
        # and the withheld output is nowhere in the ledger.
        def review(engine, task):
            self.review_calls += 1
            raise fault

        engine = self.make_engine(review=review)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "toolharnessfault")
        self.assertEqual(self.review_calls, 1)
        self.assert_halted_as_infrastructure(engine, "toolharnessfault", absent=SENTINEL_TOKENS)

    def test_validator_deadline_is_harness_fault_not_policy(self):
        with self.assertRaises(S.ToolDeadlineExceeded) as ctx:
            PJ.project_in_worker("gate", _StallOnUnpickle(), task=self.task, timeout_s=0.5)
        exc = ctx.exception
        self.assertIsInstance(exc, S.SessionFault)
        self.assertNotIsInstance(exc, S.PolicyFault)
        self.assertNotIsInstance(exc, TimeoutError)  # typed by boundary, not by builtin
        self.assertEqual((exc.boundary, exc.tool, exc.deadline_s), ("tool", "projector", 0.5))
        self.assertEqual(exc.code, "deadline_exceeded")
        self.assertEqual(exc.terminal, "HARNESS_FAULT")
        self.assertIs(exc.failure_class, WFC.TRANSIENT_INFRASTRUCTURE)
        self.assertFalse(exc.label_eligible)  # never 0.0: the harness, not the model, ran out
        self.assertEqual(engine_mod._infra_marker_for(exc), "ToolDeadlineExceeded")
        self.assertEqual(rp.halting_marker(exc), "ToolDeadlineExceeded")
        self.assertFalse(issubclass(S.ToolDeadlineExceeded, S.PolicyFault))
        self.assertNotIn("ToolDeadlineExceeded", S.POLICY_FAULT_NAMES)
        with self.assertRaises(ValueError):
            S.ToolDeadlineExceeded("dev_query", deadline_s=0)

        # Through the engine, the same disposition as every session fault.
        def review(engine, task):
            self.review_calls += 1
            raise S.ToolDeadlineExceeded("certify", deadline_s=300)

        engine = self.make_engine(review=review)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "tooldeadlineexceeded")
        self.assert_halted_as_infrastructure(engine, "tooldeadlineexceeded")

    def test_leak_tripwire_halts_before_transport(self):
        provider = RecordingProvider(responses=["{}"])

        def controller(source, raw):
            """What a bounded runner does per tool result: project in the D3
            child, gate-keep the bytes in D1, only then hand them to D0."""
            result = PJ.project_in_worker(source, raw, task=self.task)
            PJ.assert_value_free(result.payload.encode("utf-8"), task=self.task, route=None)
            return provider.complete("repair_proposer", result.payload)

        # A gate NAME that is a value trips the projector in the child; the
        # tripwire is re-raised here and the transport is never reached.
        with self.assertRaises(S.LeakTripwire) as ctx:
            controller("gate", {"gate": "primary=1", "passed": True})
        self.assertEqual(provider.calls, [])
        self.assertIsInstance(ctx.exception, S.SessionFault)
        self.assertEqual(ctx.exception.boundary, "sanitizer")
        self.assertEqual(ctx.exception.terminal, "LEAK_TRIPWIRE")
        self.assertEqual(ctx.exception.detector, "schema")

        # The D1 gatekeeper alone, over bytes a leaky producer forged, halts
        # before transport too.
        forged = canonical_json(
            {
                "kind": "dev_rows",
                "diagnostics_version": PJ.DIAGNOSTICS_VERSION,
                "columns": ["customer_id"],
                "rows": [["/Users/nobody/answer_key/gold.csv"]],
            }
        ).encode("utf-8")
        with self.assertRaises(S.LeakTripwire):
            PJ.assert_value_free(forged, task=self.task, route=None)
        self.assertEqual(provider.calls, [])
        # A clean projection passes and only then reaches the provider.
        controller("gate", {"gate": "determinism", "passed": True})
        self.assertEqual(len(provider.calls), 1)

        # Through the engine: exit 2, reward None, no round, nothing rejected,
        # and the quarantined bytes never reach the ledger.
        def review(engine, task):
            self.review_calls += 1
            raise S.LeakTripwire("canary", "private_scalar", source="gate", quarantined=b"4711")

        engine = self.make_engine(review=review)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "diagnostictripwire")
        self.assert_halted_as_infrastructure(engine, "diagnostictripwire", absent=("4711",))


# ---------------------------------------------------------------------------
# Halts and limit stops through the repair-proposer seat
# ---------------------------------------------------------------------------

class ProposerDispositionTest(EngineCase):
    def test_policy_violation_halts_without_spending_a_round_or_rejecting_task(self):
        violation = S.SessionPolicyViolation(
            S.WriteOutsideSurface(tool="apply_edit_trial", detail="answer_key/"),
            role="repair_proposer",
        )
        # 1. Raised out of a council role's stage (the runner's session).
        def review(engine, task):
            self.review_calls += 1
            raise violation

        engine = self.make_engine(review=review)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "providerprotocolerror")
        self.assertEqual(self.review_calls, 1)
        self.assert_halted_as_infrastructure(engine, "providerprotocolerror")

        # 2. Raised out of the repair-proposer seat: `_propose_patch` copies
        # the marker and halts instead of recording a marker-less FAIL row
        # and spending the round.
        proposer = RaisingProposer(violation)
        self.workspace = Path(self._tmp.name) / "taskgen-workspace-2"
        engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "providerprotocolerror")
        self.assertEqual(proposer.calls, 1)
        self.assert_halted_as_infrastructure(engine, "providerprotocolerror")
        payload = json.loads(self.review_rows(engine)[-1][1])
        self.assertEqual(payload["data"]["status"], "halted")
        self.assertIn("security event", payload["error"])
        # Halted means resumable: the next run re-executes the stage.
        self.assertIsNone(engine.blocked_stage(self.task_id))

    def test_blocked_limit_disposition_maps_to_verdict_blocked(self):
        proposer = BlockedProposer("turns")
        engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
        task = engine.run(self.task_id, until="review")
        self.assertEqual(task.task_id, self.task_id)
        self.assertEqual(proposer.calls, [("review", 0)])

        row = engine.latest_report(self.task_id, "review")
        self.assertEqual(row.verdict, VERDICT_BLOCKED)
        payload = json.loads(row.payload_json)
        data = payload["data"]
        self.assertEqual(data[engine_mod.BLOCKED_ON_KEY], "session_limit:turns")
        self.assertEqual(data["session_salt"], "1")
        self.assertEqual(data["rerun"], "1/2")
        self.assertEqual(data["status"], "blocked_limit")
        self.assertEqual(data["limit"], "turns")
        # No round, no fatal row, no status change, nothing queued.
        self.assertEqual([v for v, _ in self.review_rows(engine)], [VERDICT_FAIL, VERDICT_BLOCKED])
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))
        self.assertEqual(engine.blocked_stage(self.task_id).stage, "review")
        self.assertEqual(engine.session_limit_reruns(self.task_id, "review"), 1)
        # The stage ran once: run() returned at the block instead of sweeping again.
        self.assertEqual(self.review_calls, 1)

        # The CLI reports it as WAITING, naming the limit and the resume command.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(cli._report_blocked(engine, self.task_id))
        self.assertIn("BLOCKED at review (waiting on: session_limit:turns)", out.getvalue())
        self.assertIn("elt-taskgen review", out.getvalue())

        # The outcome's own contract.
        record = proposer_record = rp.RepairAttemptRecord(
            task_id=self.task_id, task_content_hash=self.task.content_hash(),
            stage="review", route="specification", status="blocked_limit", committed=False,
        )
        self.assertEqual(rp.REPAIR_DISPOSITIONS, {"committed", "needs_adjudication", "blocked_limit", "halted"})
        blocked = rp.RepairOutcome(record=record, disposition="blocked_limit", limit="tool_calls")
        self.assertFalse(blocked.committed)
        for bad in (
            dict(disposition="blocked_limit"),                         # no limit kind
            dict(disposition="blocked_limit", limit="Max Turns!"),     # not a code
            dict(limit="turns"),                                       # limit without the disposition
            dict(disposition="halted"),                                # no marker
            dict(infrastructure="BudgetExceededError"),                # marker without halted
            dict(disposition="nope"),
            dict(disposition="blocked_limit", limit="turns", adjudication=Path("x")),
            dict(disposition="committed"),                             # record says not committed
            dict(disposition="blocked_limit", limit="usd"),            # usd without its budget scope
            dict(disposition="blocked_limit", limit="usd", limit_scope="seat"),
            dict(disposition="blocked_limit", limit="turns", limit_scope="role"),  # scope on a non-usd kind
            dict(limit_scope="role"),                                  # scope without the disposition
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                rp.RepairOutcome(record=proposer_record, **bad)
        for scope in sorted(rp.BUDGET_LIMIT_SCOPES):
            usd = rp.RepairOutcome(
                record=proposer_record, disposition="blocked_limit", limit="usd", limit_scope=scope
            )
            self.assertEqual((usd.limit, usd.limit_scope), ("usd", scope))
        self.assertEqual(rp.BUDGET_LIMIT_SCOPES, {"role", "task", "total"})
        # Legacy construction derives the disposition, so every existing site holds.
        self.assertEqual(rp.RepairOutcome(record=record).disposition, "needs_adjudication")
        committed_record = record.model_copy(update={"status": "committed", "committed": True})
        with self.assertRaises(ValueError):
            rp.RepairOutcome(record=committed_record)  # a commit carries the task
        self.assertEqual(rp.RepairOutcome(record=committed_record, task=self.task).disposition, "committed")
        halted = rp.RepairOutcome(record=record, disposition="halted", infrastructure="SandboxFault")
        self.assertEqual(halted.infrastructure, "SandboxFault")

    def test_limit_stop_without_green_draft_blocks_with_salt_and_bounded_reruns(self):
        proposer = BlockedProposer("tool_calls")
        engine = self.make_engine(proposer=proposer, max_repair_rounds=3)

        # Run 1: blocked with salt 1. Run 2 (the operator's re-run): the
        # proposer sees one scheduled re-run, stops again, salt 2.
        salts = []
        for expected_reruns in (0, 1):
            engine.run(self.task_id, until="review")
            self.assertEqual(proposer.calls[-1], ("review", expected_reruns))
            row = engine.latest_report(self.task_id, "review")
            self.assertEqual(row.verdict, VERDICT_BLOCKED)
            data = json.loads(row.payload_json)["data"]
            self.assertEqual(data[engine_mod.BLOCKED_ON_KEY], "session_limit:tool_calls")
            salts.append(data["session_salt"])
            self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
            self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(salts, ["1", "2"])
        self.assertEqual(engine.session_limit_reruns(self.task_id, "review"), 2)
        self.assertEqual(engine_mod.MAX_SESSION_LIMIT_RERUNS, 2)

        # Run 3: the bound is reached — the ordinary bounded round is taken
        # (no third BLOCKED row), never a rejection of the limit stop's own.
        engine.run(self.task_id, until="review")
        self.assertEqual(proposer.calls[-1], ("review", 2))
        rows = self.review_rows(engine)
        self.assertEqual([v for v, _ in rows].count(VERDICT_BLOCKED), 2)
        fallback = next(
            json.loads(p) for v, p in rows
            if v == VERDICT_FAIL and json.loads(p)["data"].get("reruns_used") == "2"
        )
        self.assertIn("falling back to the ordinary bounded repair round", fallback["detail"])
        self.assertNotIn(engine_mod.BLOCKED_ON_KEY, fallback["data"])
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        self.assertEqual(len(proposer.calls), 3)
        # The count is scoped to the CURRENT identity: a commit resets it.
        self.assertEqual(engine.session_limit_reruns(self.task_id, "review"), 2)
        self.assertEqual(engine.session_limit_reruns("no-such-task", "review"), 0)

    def test_wall_limit_after_reruns_halts_as_infrastructure_not_a_round(self):
        """SoT T4 LIMIT_WALL: a wall stop includes transport latency the
        policy did not author. After the salted re-runs it HALTS as transient
        infrastructure (marker `ProviderFault`, exit 2, no round, no
        rejection) instead of feeding the round counter, so a slow provider
        can never reject a task; the agent-attributable kinds keep the round
        fallback, and the fallback row is counted so it cannot re-trigger the
        salted sequence at the same identity."""
        proposer = BlockedProposer("wall")
        engine = self.make_engine(proposer=proposer, max_repair_rounds=3)
        for _ in range(engine_mod.MAX_SESSION_LIMIT_RERUNS):
            engine.run(self.task_id, until="review")
            self.assertEqual(engine.latest_report(self.task_id, "review").verdict, VERDICT_BLOCKED)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, engine_mod.SESSION_WALL_HALT_MARKER.lower())
        self.assertEqual(len(proposer.calls), 3)
        self.assert_halted_as_infrastructure(engine, "providerfault")
        payload = json.loads(self.review_rows(engine)[-1][1])
        self.assertEqual(payload["data"]["status"], "halted")
        self.assertIn("session_limit:wall", payload["error"])
        self.assertEqual(engine_mod.SESSION_LIMIT_HALT_KINDS, frozenset({"wall"}))

        proposer = BlockedProposer("turns")
        self.workspace = Path(self._tmp.name) / "taskgen-workspace-turns"
        engine = self.make_engine(proposer=proposer, max_repair_rounds=3)
        for _ in range(engine_mod.MAX_SESSION_LIMIT_RERUNS + 1):
            engine.run(self.task_id, until="review")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        rows = self.review_rows(engine)
        fallback = next(
            json.loads(p) for v, p in rows
            if v == VERDICT_FAIL and json.loads(p)["data"].get(engine_mod.SESSION_LIMIT_FALLBACK_KEY)
        )
        self.assertEqual(fallback["data"][engine_mod.SESSION_LIMIT_FALLBACK_KEY], "turns")
        self.assertEqual(engine.session_limit_reruns(self.task_id, "review"), engine_mod.MAX_SESSION_LIMIT_RERUNS)
        # Even with the BLOCKED rows gone, the fallback row alone keeps the
        # sequence spent at this identity.
        engine._con.execute(
            "DELETE FROM reports WHERE task_id=? AND stage=? AND verdict=?",
            (self.task_id, "review", VERDICT_BLOCKED),
        )
        self.assertEqual(engine.session_limit_reruns(self.task_id, "review"), engine_mod.MAX_SESSION_LIMIT_RERUNS)

    def test_usd_limit_halts_or_rounds_by_budget_scope(self):
        """SoT T4 (Phase 0 review finding 6): only the session's OWN max_usd
        (scope `role`) is the agent-attributable LIMIT_USD — blocked with a
        salt, then the round fallback like `turns`, and the fallback row is
        counted. A task- or total-budget trip is the transport's
        PROVIDER_FAULT: the engine halts at once under `BudgetExceededError`
        (exit 2) with no salted re-run, no round and no rejection (C7)."""
        proposer = BlockedProposer("usd", scope="role")
        engine = self.make_engine(proposer=proposer, max_repair_rounds=3)
        for _ in range(engine_mod.MAX_SESSION_LIMIT_RERUNS):
            engine.run(self.task_id, until="review")
            row = engine.latest_report(self.task_id, "review")
            self.assertEqual(row.verdict, VERDICT_BLOCKED)
            data = json.loads(row.payload_json)["data"]
            self.assertEqual(data[engine_mod.BLOCKED_ON_KEY], "session_limit:usd")
            self.assertEqual(data["limit_scope"], "role")
            self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        engine.run(self.task_id, until="review")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        self.assertEqual(len(proposer.calls), 3)
        fallback = next(
            json.loads(p) for v, p in self.review_rows(engine)
            if v == VERDICT_FAIL and json.loads(p)["data"].get(engine_mod.SESSION_LIMIT_FALLBACK_KEY)
        )
        self.assertEqual(fallback["data"][engine_mod.SESSION_LIMIT_FALLBACK_KEY], "usd")
        self.assertEqual(fallback["data"]["limit_scope"], "role")
        self.assertEqual(
            engine.session_limit_reruns(self.task_id, "review"), engine_mod.MAX_SESSION_LIMIT_RERUNS
        )

        self.assertEqual(engine_mod.SESSION_USD_HALT_SCOPES, frozenset({"task", "total"}))
        self.assertEqual(engine_mod.SESSION_USD_ROLE_SCOPE, "role")
        self.assertEqual(engine_mod.SESSION_USD_HALT_MARKER, "BudgetExceededError")
        for scope in ("task", "total"):
            with self.subTest(scope=scope):
                proposer = BlockedProposer("usd", scope=scope)
                self.workspace = Path(self._tmp.name) / f"taskgen-workspace-{scope}"
                engine = self.make_engine(proposer=proposer, max_repair_rounds=3)
                with self.assertRaises(InfrastructureFailure) as ctx:
                    engine.run(self.task_id, until="review")
                self.assertEqual(ctx.exception.marker, "budgetexceedederror")
                self.assertEqual(len(proposer.calls), 1)
                self.assert_halted_as_infrastructure(engine, "budgetexceedederror")
                rows = self.review_rows(engine)
                self.assertNotIn(VERDICT_BLOCKED, [v for v, _ in rows])
                payload = json.loads(rows[-1][1])
                self.assertEqual(payload["data"]["status"], "halted")
                self.assertEqual(payload["data"]["limit"], "usd")
                self.assertEqual(payload["data"]["limit_scope"], scope)
                self.assertIn("session_limit:usd", payload["error"])
                self.assertIn(scope, payload["error"])
                self.assertEqual(engine.session_limit_reruns(self.task_id, "review"), 0)
                # Halted means resumable: the next run re-executes the stage.
                self.assertIsNone(engine.blocked_stage(self.task_id))

    def test_budget_limit_scope_keys_on_role_cap_name_or_scope_attribute(self):
        """`budget_limit_scope`: `RoleCapExceeded` (providers.py) is the role
        scope by NAME — read through the MRO, so the module imports nothing
        from the provider layer — whatever its `scope` says; any other
        BudgetExceededError answers with its `scope` attribute (an unknown
        one is the transport's); a wrapped breach still answers; no breach
        is ''. `halting_marker` halts on every scope but `role`."""
        B = providers_mod.BudgetExceededError
        self.assertEqual(rp.budget_limit_scope(B("cap", scope="role")), "role")
        self.assertEqual(rp.budget_limit_scope(B("cap", scope="task")), "task")
        self.assertEqual(rp.budget_limit_scope(B("cap", scope="total")), "total")
        self.assertEqual(rp.budget_limit_scope(B("cap", scope="seat")), "task")
        self.assertEqual(rp.budget_limit_scope(RuntimeError("x")), "")
        self.assertEqual(rp.ROLE_CAP_EXCEPTION_NAME, "RoleCapExceeded")
        role_cap = type("RoleCapExceeded", (B,), {})
        self.assertEqual(rp.budget_limit_scope(role_cap("cap", scope="task")), "role")
        real = getattr(providers_mod, "RoleCapExceeded", None)
        if real is not None:
            self.assertTrue(issubclass(real, B))
            self.assertEqual(real.__name__, rp.ROLE_CAP_EXCEPTION_NAME)
        wrapped = RuntimeError("stage wrapper")
        wrapped.__cause__ = B("total budget", scope="total")
        self.assertEqual(rp.budget_limit_scope(wrapped), "total")
        self.assertEqual(rp.halting_marker(B("cap", scope="role")), "")
        self.assertEqual(rp.halting_marker(role_cap("cap", scope="task")), "")
        self.assertEqual(rp.halting_marker(B("cap", scope="task")), "BudgetExceededError")
        self.assertEqual(rp.halting_marker(B("cap", scope="total")), "BudgetExceededError")
        self.assertEqual(rp.halting_marker(wrapped), "BudgetExceededError")
        # On its OWN ladder the engine still names every BudgetExceededError
        # as infrastructure: a critic's cap is not the proposer's LIMIT_USD.
        self.assertEqual(engine_mod._infra_marker_for(B("cap", scope="role")), "BudgetExceededError")

    def test_one_shot_role_cap_trip_is_blocked_limit_usd_not_a_halt(self):
        """The one-shot proposer reports the session's own max_usd trip
        (scope `role`) as `blocked_limit` / `usd` / `role` (SoT T4 LIMIT_USD):
        one call, no marker, nothing queued; through the engine a BLOCKED
        row with a salt. A task-scope breach still halts as before."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        provider = RecordingProvider(raise_exc=providers_mod.BudgetExceededError("role cap", scope="role"))
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual(
            (outcome.disposition, outcome.limit, outcome.limit_scope),
            (rp.DISPOSITION_BLOCKED_LIMIT, "usd", "role"),
        )
        self.assertEqual(outcome.infrastructure, "")
        self.assertEqual(outcome.record.status, rp.DISPOSITION_BLOCKED_LIMIT)
        self.assertEqual(len(outcome.record.attempts), 1)
        self.assertEqual(outcome.record.attempts[0].error_type, "BudgetExceededError")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(outcome.adjudication)
        self.assertIsNone(outcome.task)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))

        provider = RecordingProvider(raise_exc=providers_mod.BudgetExceededError("role cap", scope="role"))
        self.workspace = Path(self._tmp.name) / "taskgen-workspace-rolecap"
        engine = self.make_engine(proposer=rp.RepairProposer(provider, max_attempts=2), max_repair_rounds=2)
        engine.run(self.task_id, until="review")
        row = engine.latest_report(self.task_id, "review")
        self.assertEqual(row.verdict, VERDICT_BLOCKED)
        data = json.loads(row.payload_json)["data"]
        self.assertEqual(data[engine_mod.BLOCKED_ON_KEY], "session_limit:usd")
        self.assertEqual(data["limit_scope"], "role")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertEqual(len(provider.calls), 1)

        provider = RecordingProvider(raise_exc=providers_mod.BudgetExceededError("task budget", scope="task"))
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, engine.load_task(self.task_id), "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual((outcome.disposition, outcome.infrastructure), (rp.DISPOSITION_HALTED, "BudgetExceededError"))

    def test_malformed_repair_outcome_is_a_proposer_contract_fault(self):
        """Phase 0 review finding 8: a ValueError / TypeError escaping
        `repair()` — a RepairOutcome the validator refuses (a limit kind that
        is no code, a halt without its marker, a usd stop without its scope),
        a wrong signature — is the proposer's CONTRACT breaking: a harness
        defect that halts under `proposer_contract` (exit 2, no round, no
        rejection, nothing queued), never a failed-proposal row. Any other
        exception keeps today's failed-proposal disposition."""
        record = rp.RepairAttemptRecord(
            task_id=self.task_id, task_content_hash=self.task.content_hash(),
            stage="review", route="specification", status="blocked_limit", committed=False,
        )

        class Malformed:
            def __init__(self, **bad):
                self.bad = bad
                self.calls = 0

            def repair(self, engine, task, stage, route, failure):
                self.calls += 1
                return rp.RepairOutcome(record=record, **self.bad)

        class WrongSignature:
            def repair(self, engine):  # the engine passes five arguments
                raise AssertionError("never reached")

        self.assertEqual(engine_mod.PROPOSER_CONTRACT_MARKER, "proposer_contract")
        cases = {
            "limit_not_a_code": Malformed(disposition="blocked_limit", limit="LIMIT_WALL"),
            "halt_without_marker": Malformed(disposition="halted"),
            "usd_without_scope": Malformed(disposition="blocked_limit", limit="usd"),
            "wrong_signature": WrongSignature(),
        }
        for label, proposer in cases.items():
            with self.subTest(label=label):
                self.workspace = Path(self._tmp.name) / f"taskgen-workspace-{label}"
                engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
                with self.assertRaises(InfrastructureFailure) as ctx:
                    engine.run(self.task_id, until="review")
                self.assertEqual(ctx.exception.marker, "proposer_contract")
                if isinstance(proposer, Malformed):
                    self.assertEqual(proposer.calls, 1)
                self.assert_halted_as_infrastructure(engine, "proposer_contract")
                payload = json.loads(self.review_rows(engine)[-1][1])
                self.assertEqual(payload["data"]["status"], "halted")
                self.assertNotIn("repair proposer failed", payload["error"])
                self.assertIsNone(engine.blocked_stage(self.task_id))
        # The boundary: a proposer raising anything else is still a failed
        # proposal (a marker-less FAIL row) and the ordinary round runs.
        proposer = RaisingProposer(RuntimeError("proposer blew up"))
        self.workspace = Path(self._tmp.name) / "taskgen-workspace-runtime"
        engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
        engine.run(self.task_id, until="review")
        failed = [
            json.loads(p) for v, p in self.review_rows(engine)
            if v == VERDICT_FAIL and "repair proposer failed" in json.loads(p)["error"]
        ]
        self.assertTrue(failed)
        self.assertEqual(failed[0]["infrastructure"], "")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)

    def test_proposer_halts_on_harness_fault_inside_an_attempt(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        # The transport fails on the first attempt: HALTED, one call, no
        # second attempt spending more of a breached budget, nothing queued.
        provider = RecordingProvider(raise_exc=providers_mod.TranscriptMissingError("replay-only: no transcript"))
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "TranscriptMissingError")
        self.assertEqual(outcome.record.status, rp.STATUS_HALTED)
        self.assertEqual(len(outcome.record.attempts), 1)
        self.assertEqual(outcome.record.attempts[0].error_type, "TranscriptMissingError")
        self.assertIsNone(outcome.adjudication)
        self.assertIsNone(outcome.task)
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))

        # A malformed PATCH is not a halt: retried as today, then abstains.
        provider = RecordingProvider(responses=["not a patch {{{"])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        self.assertEqual(len(outcome.record.attempts), 2)
        self.assertEqual({a.error_type for a in outcome.record.attempts}, {"ProviderProtocolError"})
        self.assertEqual(len(provider.calls), 2)

    def test_proposer_harness_fault_halts_the_engine_without_a_round(self):
        # The D1 gatekeeper inside the transport refuses to send: the proposer
        # reports HALTED with the marker, the engine copies it and halts.
        provider = RecordingProvider(
            raise_exc=PJ.DiagnosticTripwire("canary", "private_scalar", source="rejection", quarantined=b"4711")
        )
        proposer = rp.RepairProposer(provider, max_attempts=2)
        engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "diagnostictripwire")
        self.assertEqual(len(provider.calls), 1)
        self.assert_halted_as_infrastructure(engine, "diagnostictripwire", absent=("4711",))
        payload = json.loads(self.review_rows(engine)[-1][1])
        self.assertEqual(payload["data"]["status"], "halted")
        self.assertEqual(payload["data"]["infrastructure"], "diagnostictripwire")


# ---------------------------------------------------------------------------
# CLI plumbing: producer notes and remedy subcommands
# ---------------------------------------------------------------------------

class AuthorSessionLimitDispositionTest(EngineCase):
    """The AUT seat's limit-stop disposition THROUGH THE ENGINE (SoT T4; C7):
    an author session that stops at a harness-imposed cap with no draft
    raises `council.AuthorSessionLimitStop`, and the author stage's mapping
    (`council.author_session_limit_outcome`) makes the stage WAIT —
    `VERDICT_BLOCKED` with `blocked_on = session_limit:<kind>` and a salt,
    no repair round, no rejection — for at most two salted re-runs, then
    takes today's empty-output route once.

    `cli.make_author_runner` catches the signal ITSELF (the Group A shim:
    `_author_prose_for` salts the author policy with
    `engine.session_limit_reruns(task_id, "author")` so a salted re-run keys
    its own transcript, persists the partial session record, and the stage
    maps the signal through `council.author_session_limit_outcome`); the
    test pins that no wrapper is needed, that each run's policy carries the
    ledger's salt, and that the engine honours the mapped outcome."""

    def test_author_limit_stop_without_draft_blocks_the_stage_without_a_round(self):
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review import council, prompts
        from tests.test_author_session import SessionDouble, _AuthorConfig
        from tests.test_bounded_session import tool_use

        from tests.test_author_session import GREEN_PROSE

        providers: list = []

        def fresh_provider():
            # Every re-run gets a fresh scripted double: three empty submits
            # under max_revisions 1 (max_turns 2) never produce a draft; the
            # session after the fallback round submits a green draft.
            if len(providers) < 3:
                script = [[tool_use("submit_prose", {"text": ""}, f"a{i}")] for i in range(3)]
            else:
                script = [[tool_use("submit_prose", {"text": GREEN_PROSE}, "g")]]
            provider = SessionDouble(script)
            providers.append(provider)
            return provider

        def run_author(engine, task):
            # The CLI's own runner, unwrapped: it must not let the signal escape.
            return cli.make_author_runner(fresh_provider())(engine, task)

        with _AuthorConfig():
            engine = self.make_engine(max_repair_rounds=3)
            engine.set_stage_runner("author", run_author)
            salts = []
            for expected_reruns in (0, 1):
                engine.run(self.task_id, until="author")
                row = engine.latest_report(self.task_id, "author")
                self.assertEqual(row.verdict, VERDICT_BLOCKED)
                data = json.loads(row.payload_json)["data"]
                self.assertEqual(data[engine_mod.BLOCKED_ON_KEY], "session_limit:turns")
                self.assertEqual((data["limit"], data["reruns_used"], data["session_terminal"]),
                                 ("turns", str(expected_reruns), "LIMIT_TURNS"))
                salts.append(data["session_salt"])
                # The session ran under the ledger's salt (0 first, then 1):
                # a salted re-run keys its own transcript.
                self.assertEqual(providers[-1].sessions[-1]["policy"].session_salt, expected_reruns)
                self.assertEqual(
                    providers[-1].sessions[-1]["view"],
                    prompts.semantic_author_session_view(
                        council.render_view(CouncilRole.SEMANTIC_AUTHOR, engine.load_task(self.task_id)),
                        max_revisions=1, session_salt=expected_reruns,
                    ),
                )
                # The partial session record of the limit stop is persisted
                # beside the task's reports like any other.
                reports = engine.workspace / "tasks" / self.task_id / cli.AUTHOR_SESSION_REPORTS_REL
                self.assertEqual(len(list(reports.glob("semantic_author.*.json"))), expected_reruns + 1)
                self.assertEqual(json.loads(sorted(reports.glob("semantic_author.*.json"))[0].read_text())["terminal"], "LIMIT_TURNS")
                self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
                self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
                self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
                self.assertEqual(engine.blocked_stage(self.task_id).stage, "author")
                self.assertEqual(self.review_calls, 0, "the block returns before review runs")
            self.assertEqual(salts, ["1", "2"])
            self.assertEqual(engine.session_limit_reruns(self.task_id, "author"), 2)
            self.assertEqual(len(providers), 2)
            self.assertTrue(all(p.completes == 0 for p in providers))
            self.assertEqual([p.sessions[0]["policy"].session_salt for p in providers], [0, 1])
            # The bound reached: the ordinary route, once — a FAIL row that
            # names the fallback (so it cannot re-trigger the salted
            # sequence) and spends the round the cap was sparing.
            engine.run(self.task_id, until="author")
            rows = engine._con.execute(
                "SELECT verdict, payload_json FROM reports WHERE task_id=? AND stage='author' ORDER BY id",
                (self.task_id,),
            ).fetchall()
            self.assertEqual([v for v, _ in rows].count(VERDICT_BLOCKED), 2)
            fallback = next(
                json.loads(p) for v, p in rows
                if v == VERDICT_FAIL and json.loads(p)["data"].get(engine_mod.SESSION_LIMIT_FALLBACK_KEY) == "turns"
            )
            self.assertNotIn(engine_mod.BLOCKED_ON_KEY, fallback["data"])
            self.assertEqual(fallback["data"]["reruns_used"], "2")
            self.assertEqual(fallback.get("infrastructure") or "", "")
            self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
            self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
            # The session after that round authored a green draft: the stage
            # PASSED at the new revision and the pipeline went on.
            self.assertEqual(len(providers), 4)
            latest = engine.latest_report(self.task_id, "author")
            self.assertEqual(latest.verdict, VERDICT_PASS)
            self.assertEqual(engine.load_task(self.task_id).solver_prompt, GREEN_PROSE)
            self.assertEqual(engine.session_limit_reruns(self.task_id, "author"), 0, "scoped to the new identity")
            # Salts: 0, 1, then 2 for the run at the bound (whose fallback
            # FAIL pins `session_limit_reruns` at the bound for THIS identity:
            # the salted sequence is spent here for good), and still 2 for
            # the run the ordinary round re-issued at the same identity — the
            # PASS is what moved it (the counter reads 0 above).
            self.assertEqual([p.sessions[0]["policy"].session_salt for p in providers], [0, 1, 2, 2])


class CliPlumbingTest(unittest.TestCase):
    def test_transport_marker_lifts_session_fault_names(self):
        for name in sorted(S.INFRASTRUCTURE_FAULT_NAMES):
            self.assertEqual(cli._transport_marker([f"{name}: something happened"]), name)
        # The class that was RAISED leads the note and wins over one it mentions.
        self.assertEqual(
            cli._transport_marker(["ToolHarnessFault: tool 'x' raised SandboxFault"]),
            "ToolHarnessFault",
        )
        self.assertEqual(
            cli._transport_marker(["a note that names nothing", "TranscriptMissingError: gone"]),
            "TranscriptMissingError",
        )
        self.assertEqual(cli._transport_marker([]), "")
        self.assertEqual(cli._transport_marker(["ForbiddenArgument: forbidden_argument"]), "")
        for name in sorted(S.POLICY_FAULT_NAMES):
            self.assertEqual(cli._transport_marker([f"{name}: code"]), "", name)

    def test_stage_subcommands_maps_author_to_review(self):
        import argparse

        self.assertEqual(cli.STAGE_SUBCOMMANDS[engine_mod.StageName.AUTHOR.value], "review")
        parser = cli.build_parser()
        subparsers = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        for stage, subcommand in cli.STAGE_SUBCOMMANDS.items():
            self.assertIn(subcommand, subparsers.choices, stage)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

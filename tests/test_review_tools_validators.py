"""The repair proposer's session tools and its provider-free `certify`
(roadmap Phase 1 item 1.P; certify addendum Design H §2, §3.1, §3.2, §5;
permission matrix Output 5 RPR column; SoT T3).

WHY THIS EXISTS
Eight tools let a model edit ONE artifact on a held trial copy and ask a
provider-free certifier twice whether the route's `generate` / `reference`
stages go green — without ever holding the provider, reading a literal row,
naming a path, seeing a number or moving the live workspace. These tests pin
that contract against the demo task, the real projections and the real
provider-free runners (about two seconds per certify), with fixture runners
where the runner's BEHAVIOUR is the subject (a crash, a stall, a red stage).
Names follow the roadmap's Phase 1 test table exactly.

Everything is offline: no provider, no network, no live drive. The one
provider double here raises on any `complete` to prove it is never reached.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

try:  # `python -m unittest tests.test_review_tools_validators` from the repo root
    from tests import test_bounded_session as BS
except ImportError:  # discovered from inside tests/ (no package on sys.path)
    import test_bounded_session as BS  # type: ignore[no-redef]

from elt_taskgen import cli as cli_mod
from elt_taskgen import demo_fixture
from elt_taskgen import engine as engine_mod
from elt_taskgen import repair
from elt_taskgen.engine import Engine, StageOutcome, StagePayload, VERDICT_FAIL, VERDICT_PASS
from elt_taskgen.models import RepairPatch, RepairRoute, canonical_json
from elt_taskgen.review import providers as P
from elt_taskgen.review import repair_proposer as rp
from elt_taskgen.review import session as S
from elt_taskgen.review.tools import certify as C
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG
from elt_taskgen.review.tools import validators as V
from elt_taskgen.training.contract import WorkspaceFailureClass
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.workspace import repo_root

MART = demo_fixture.MART_NAME
ROLE = V.ROLE
SENTINEL = "Ties are broken by customer_id ascending."

#: Every role the SoT names (T1).
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

#: The RPR rows of the permission matrix (Output 5 §2 and §3) and SoT T1/T3:
#: per-tool ceiling (`certify` <= 2 is the tool's own `per_session`, refused
#: at PERMIT), oracle bits, the no-cost refusal codes the tool answers at
#: PERMIT (charged nothing), the idempotent-read exemption, whether the tool
#: writes the surface, is a validator or a terminal, and its wall.
MATRIX = {
    "read_view": {"bits": 0, "per_session": None, "no_cost": (), "idempotent": True, "write": False, "validator": False, "terminal": False, "wall_s": 10.0},
    "read_field": {"bits": 0, "per_session": None, "no_cost": (), "idempotent": True, "write": False, "validator": False, "terminal": False, "wall_s": 10.0},
    "apply_edit_trial": {"bits": 0, "per_session": 6, "no_cost": (), "idempotent": False, "write": True, "validator": False, "terminal": False, "wall_s": 10.0},
    "check_scope": {"bits": 0, "per_session": 4, "no_cost": (), "idempotent": False, "write": False, "validator": True, "terminal": False, "wall_s": 10.0},
    "check_cheap": {"bits": 0, "per_session": 4, "no_cost": (), "idempotent": False, "write": False, "validator": True, "terminal": False, "wall_s": 10.0},
    "certify": {"bits": 2, "per_session": 2, "no_cost": ("certify_refused_no_provider_free_stage", "certify_refused_unchanged_trial"), "idempotent": False, "write": False, "validator": True, "terminal": False, "wall_s": 300.0},
    "submit_patch": {"bits": 0, "per_session": None, "no_cost": (), "idempotent": False, "write": False, "validator": False, "terminal": True, "wall_s": 10.0},
    "abort": {"bits": 0, "per_session": None, "no_cost": (), "idempotent": False, "write": False, "validator": False, "terminal": True, "wall_s": 10.0},
}


# ---------------------------------------------------------------------------
# Doubles and helpers
# ---------------------------------------------------------------------------

class RaisingProvider:
    """A provider-shaped object that must never be reached."""

    def __init__(self) -> None:
        self.calls: list = []
        self.exchange_evidence: list = []

    def complete(self, role, prompt: str) -> str:
        self.calls.append((role, prompt))
        raise AssertionError("a session tool reached the provider")


def pass_runner(detail: str = "ok"):
    def run(engine, task):
        return StageOutcome(VERDICT_PASS, StagePayload(detail=detail))

    return run


def fail_runner(error: str):
    def run(engine, task):
        return StageOutcome(VERDICT_FAIL, StagePayload(error=error))

    return run


def tree_hash(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(root).rglob("*"))
        if p.is_file()
        and p.relative_to(root).as_posix() != "state/taskgen.sqlite-shm"
    }


def reference_edit_args(task, comment: str = "-- clarified reference") -> dict:
    """An in-scope REFERENCE edit: a leading comment on the reference SQL
    (bytes move, meaning does not, so the real `reference` re-freezes green)."""
    head = task.reference.sql_by_mart[MART].split("\n", 1)[0]
    return {
        "artifact": "task_ir.json",
        "op": "replace",
        "locator": f"reference.sql_by_mart.{MART}",
        "old": head,
        "new": f"{comment}\n{head}",
        "rationale": "document the reference's join rule",
    }


def missing_anchor_args() -> dict:
    """Applies nowhere: the replace anchor does not occur in the reference SQL."""
    return {
        "artifact": "task_ir.json",
        "op": "replace",
        "locator": f"reference.sql_by_mart.{MART}",
        "old": "THIS ANCHOR DOES NOT OCCUR IN THE REFERENCE",
        "new": "-- unreachable",
        "rationale": "a proposal over text it never read",
    }


def spec_edit_args(text: str = SENTINEL) -> dict:
    return {
        "artifact": "task_ir.json",
        "op": "insert",
        "locator": "solver_prompt",
        "old": "",
        "new": " " + text,
        "rationale": "the review stage flagged an undefined tie-break",
    }


def population_edit_args() -> dict:
    return {
        "artifact": "task_ir.json",
        "op": "replace",
        "locator": "populations.1.conditions.1",
        "old": "Cancelled orders are present.",
        "new": "Cancelled orders are present, and some are refunded.",
        "rationale": "state the cancelled-order condition precisely",
    }


class ProposerToolsCase(unittest.TestCase):
    """A live workspace with the demo task registered (and, on request, its
    populations materialised so the real `reference` runner can execute),
    and sessions held open for the test's lifetime."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "taskgen-workspace"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.engine.register(demo_fixture.demo_task())
        self.task = self.engine.load_task(demo_fixture.DEMO_TASK_ID)
        self.task_id = self.task.task_id
        self.provider = RaisingProvider()

    def materialize(self) -> None:
        """What a workspace whose `generate` passed looks like."""
        outcome = cli_mod.run_generate(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_PASS)

    def open_session(self, route, failed_stage, **options) -> V.ProposerSession:
        options.setdefault("failure", "stage failed")
        return self.enterContext(
            V.ProposerSession.open(self.workspace, self.task, route, failed_stage, **options)
        )

    @staticmethod
    def dispatch(session: V.ProposerSession, name: str, **args):
        return V.proposer_registry().dispatch(session.context(), name, args)

    def assert_clean(self, diag, session: V.ProposerSession) -> None:
        payload = PJ.serialize_for_transport(diag, task=session.task, route=session.route)
        PJ.assert_value_free(payload.encode("utf-8"), task=session.task, route=session.route)
        text = diag.render()
        self.assertIsNone(re.search(r"\d", text), text)
        self.assertNotIn("/", text)


# ---------------------------------------------------------------------------
# certify
# ---------------------------------------------------------------------------

class CertifyTest(ProposerToolsCase):
    def test_certify_refused_on_specification_route_costs_no_call_and_no_bits(self):
        """Every SPECIFICATION failure has no provider-free member (its rerun
        set opens at `author`): `certify` is refused at PERMIT as
        `certify_refused_no_provider_free_stage` — a tool call, zero oracle
        bits, no copy, no worker — before and after an edit."""
        self.assertEqual(C.provider_free_stages(RepairRoute.SPECIFICATION, "review"), ())
        self.assertEqual(C.provider_free_stages(RepairRoute.SPECIFICATION, "attack"), ())
        session = self.open_session(RepairRoute.SPECIFICATION, "review")
        certify = V.proposer_tool("certify")
        self.assertEqual(certify.permit(session.context(), {}), C.CODE_REFUSED_NO_PROVIDER_FREE_STAGE)
        with mock.patch.object(C, "certify_disposable_copy", side_effect=AssertionError("worker spawned")) as spawn:
            first = self.dispatch(session, "certify")
            self.dispatch(session, "apply_edit_trial", **spec_edit_args())
            second = self.dispatch(session, "certify")
        spawn.assert_not_called()
        for diag in (first, second):
            self.assertEqual(diag.source, PJ.DiagnosticSource.CERTIFY)
            self.assertEqual(diag.code, C.CODE_REFUSED_NO_PROVIDER_FREE_STAGE)
            self.assertFalse(diag.ok)
            self.assertEqual(dict(diag.flags), {"model_stages_deferred": True, "discrimination_weakened": False})
            self.assert_clean(diag, session)
        # The tool's own accounting: three calls, two refusals, no bit spent.
        self.assertEqual(session.tool_calls, 3)
        self.assertEqual(session.certify_refusals, 2)
        self.assertEqual(session.certify_calls, 0)
        self.assertEqual(session.oracle_bits_used, 0)
        self.assertEqual(session.certify_receipts, [])
        # The disposable-copy seam refuses an empty stage set without a copy either.
        receipt = C.certify_disposable_copy(session.trial, self.task_id, (), runners={})
        self.assertEqual(receipt.diagnostic.code, C.CODE_REFUSED_NO_PROVIDER_FREE_STAGE)
        self.assertEqual(receipt.stages, ())

    def test_certify_refused_when_state_epoch_unchanged(self):
        """`certify` runs only after the trial's bytes moved: before any edit,
        and again after an executed certify with no edit in between, it is
        refused as `certify_refused_unchanged_trial` at no cost."""
        runners = {"generate": pass_runner(), "reference": pass_runner()}
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        self.assertEqual(session.state_epoch, session.certified_epoch)
        refused = self.dispatch(session, "certify")
        self.assertEqual(refused.code, C.CODE_REFUSED_UNCHANGED_TRIAL)
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (0, 0))

        applied = self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        self.assertEqual((applied.code, applied.ok), ("applied", True))
        self.assertEqual(session.state_epoch, 1)
        green = self.dispatch(session, "certify")
        self.assertEqual(green.code, C.CODE_GREEN)
        self.assertEqual((session.certify_calls, session.certified_epoch, session.oracle_bits_used), (1, 1, 2))

        again = self.dispatch(session, "certify")
        self.assertEqual(again.code, C.CODE_REFUSED_UNCHANGED_TRIAL)
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        # An inapplicable edit (a missing anchor) moves nothing: still refused.
        noop = self.dispatch(session, "apply_edit_trial", **missing_anchor_args())
        self.assertEqual((noop.source.value, noop.code), ("rejection", "patch_anchor_not_found"))
        self.assertEqual(session.state_epoch, 1)
        self.assertEqual(self.dispatch(session, "certify").code, C.CODE_REFUSED_UNCHANGED_TRIAL)
        # A second real edit re-arms it.
        second = self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task, "-- second note"))
        self.assertEqual(second.code, "applied")
        self.assertEqual(session.state_epoch, 2)
        self.assertEqual(self.dispatch(session, "certify").code, C.CODE_GREEN)
        self.assertEqual((session.certify_calls, session.certified_epoch), (2, 2))

    def test_certify_worker_receives_no_provider_handle(self):
        """The certify runner dict is built without a provider and its
        closures reach none (`__closure__` inspection, `__wrapped__`
        followed); a dict that does reach one is refused as a harness fault
        before anything runs; and a certify beside a provider that raises on
        any `complete` still returns a verdict from the REAL runners."""
        runners = C.provider_free_stage_runners()
        self.assertEqual(tuple(runners), ("generate", "reference"))
        for stage, runner in runners.items():
            self.assertEqual(C.provider_handles_in(runner), (), stage)
        C.assert_no_provider_handle(runners)  # does not raise
        self.assertIs(runners["generate"], cli_mod.run_generate)

        # Positive controls: a closure over a provider, a bound method of an
        # object holding one, and the REAL wired author runner.
        provider = self.provider

        def nested(engine, task):
            return provider.complete(ROLE, "x")

        class Holder:
            def __init__(self):
                self.provider = provider

            def run(self, engine, task):
                return self.provider.complete(ROLE, "x")

        self.assertEqual(C.provider_handles_in(nested), ("provider",))
        self.assertEqual(C.provider_handles_in(Holder().run), ("__self__.provider",))
        wired = cli_mod.build_stage_runners(provider)
        self.assertTrue(C.provider_handles_in(wired[engine_mod.StageName.AUTHOR]))
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            C.assert_no_provider_handle({"generate": runners["generate"], "reference": nested})
        self.assertEqual(ctx.exception.code, "provider_handle_in_worker")
        self.assertEqual(ctx.exception.stages, ("reference",))
        with self.assertRaises(S.ToolHarnessFault):
            C.run_provider_free_stages(self.workspace, self.task_id, ("reference",), runners={"reference": nested})
        self.assertEqual(provider.calls, [])

        # The real thing: no provider argument exists anywhere on the path.
        self.assertNotIn("provider", inspect.signature(C.run_provider_free_stages).parameters)
        self.assertNotIn("provider", inspect.signature(C.certify_disposable_copy).parameters)
        self.assertNotIn("provider", inspect.signature(V.ProposerSession.__init__).parameters)
        self.materialize()
        session = self.open_session(RepairRoute.REFERENCE, "reference")  # default runners
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        verdict = self.dispatch(session, "certify")
        self.assertEqual(verdict.code, C.CODE_GREEN)
        self.assertEqual(session.certify_receipts[-1].stages, ("reference",))
        self.assertEqual(provider.calls, [])

    def test_certify_runs_on_a_disposable_copy_and_the_held_trial_is_byte_unchanged(self):
        """sha256 of every file under the held trial (and the live workspace)
        is equal before and after a REAL certify; the nested copy is gone."""
        self.materialize()
        session = self.open_session(RepairRoute.REFERENCE, "reference")
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        trial_before = tree_hash(session.trial)
        live_before = tree_hash(self.workspace)
        self.assertIn(f"tasks/{self.task_id}/task_ir.json", trial_before)
        verdict = self.dispatch(session, "certify")
        self.assertEqual(verdict.code, C.CODE_GREEN)
        self.assertEqual(tree_hash(session.trial), trial_before)
        self.assertEqual(tree_hash(self.workspace), live_before)
        receipt = session.certify_receipts[-1]
        self.assertNotEqual(receipt.copy_path.resolve(), session.trial.resolve())
        self.assertFalse(receipt.copy_path.exists())
        self.assertFalse(receipt.copy_path.parent.exists())
        # The reference stage's outputs (gold, determinism evidence) landed on
        # the copy only: the held trial has no answer key from it.
        self.assertFalse((session.trial / "tasks" / self.task_id / "answer_key" / "gold").exists())
        self.assertGreater(receipt.elapsed_s, 0.0)

    def test_certify_runner_exception_is_tool_harness_fault_not_red(self):
        """A `reference` runner that raises `MemoryError` is a
        `ToolHarnessFault` (halt, reward None), never `certify_red_reference`;
        a `SessionFault` or an engine infrastructure class is re-raised as is;
        a stage that could not measure is a harness fault too."""

        def oom(engine, task):
            raise MemoryError("the worker ran out of memory: 4711 rows")

        runners = {"generate": pass_runner(), "reference": oom}
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.dispatch(session, "certify")
        exc = ctx.exception
        self.assertEqual((exc.tool, exc.code, exc.cause_type), ("certify", "harness_exception", "MemoryError"))
        self.assertNotIn("4711", str(exc))
        self.assertIs(S.terminal_for_exception(exc), S.TerminalState.HARNESS_FAULT)
        self.assertEqual(engine_mod._infra_marker_for(exc), "ToolHarnessFault")
        self.assertFalse(exc.label_eligible)
        # The call was SPAWNED, so it is a paid call: charged at spawn, never
        # a free probe (a deadline or a crash still spent an executed certify).
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        self.assertEqual(session.certify_receipts, [])
        # Classification by CLASS (Phase 3 review finding 1-0): a session
        # fault or an engine infrastructure class is itself; an OS-level
        # fault is wrapped; a `ValueError` is a MEASURED defect (None: the
        # caller answers red), never a harness fault.
        sandbox = S.SandboxFault("worker died")
        self.assertIs(C.classify_runner_exception(sandbox), sandbox)
        budget = P.BudgetExceededError("budget", scope="task")
        self.assertIs(C.classify_runner_exception(budget), budget)
        wrapped = C.classify_runner_exception(OSError(28, "No space left on device"))
        self.assertIsInstance(wrapped, S.ToolHarnessFault)
        self.assertEqual(wrapped.cause_type, "OSError")
        self.assertIsNone(C.classify_runner_exception(ValueError("boom")))
        # A stage whose payload carries an infrastructure marker cannot be red.
        def could_not_measure(engine, task):
            return StageOutcome(VERDICT_FAIL, StagePayload(error="transport", infrastructure="TranscriptMissingError"))

        with self.assertRaises(S.ToolHarnessFault) as ctx2:
            C.run_provider_free_stages(
                session.trial, self.task_id, ("reference",), runners={"reference": could_not_measure}
            )
        self.assertEqual(ctx2.exception.code, "stage_could_not_measure")
        # ... whereas an ordinary red verdict IS the code.
        red = C.run_provider_free_stages(
            session.trial, self.task_id, ("reference",), runners={"reference": fail_runner("gold mismatch")}
        )
        self.assertEqual((red.code, red.ok), ("certify_red_reference", False))

    def test_certify_blocked_stage_is_no_measure_not_red(self):
        """A provider-free stage that WAITS (`VERDICT_BLOCKED`: an environment
        or human-approval hold, stale evidence) is a no-measure outcome — the
        same `ToolHarnessFault(stage_could_not_measure)` as a stage that could
        not measure, the reason as `blocked_on:<reason>` — never
        `certify_red_<stage>` steering the model as if its edit were rejected:
        the session halts, the spawned call is charged (an executed certify,
        its bits), no receipt is kept, and no BLOCKED code exists in the
        model's certify vocabulary."""

        def waiting(engine, task):
            return StageOutcome(
                engine_mod.VERDICT_BLOCKED,
                StagePayload(
                    detail="waiting on the warehouse at /Users/x/runs/live",
                    data={engine_mod.BLOCKED_ON_KEY: engine_mod.BLOCKED_ON_ENVIRONMENT},
                ),
            )

        runners = {"generate": pass_runner(), "reference": waiting}
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        held = tree_hash(session.trial)
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.dispatch(session, "certify")
        exc = ctx.exception
        self.assertEqual(
            (exc.tool, exc.code, exc.cause_type),
            ("certify", "stage_could_not_measure", "blocked_on:environment"),
        )
        self.assertEqual(exc.blocked_on, "environment")
        self.assertNotIn("warehouse", str(exc))
        self.assertNotIn("/Users", str(exc))
        self.assertIs(S.terminal_for_exception(exc), S.TerminalState.HARNESS_FAULT)
        self.assertEqual(engine_mod._infra_marker_for(exc), "ToolHarnessFault")
        self.assertFalse(exc.label_eligible)
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        self.assertEqual(session.certify_receipts, [])
        self.assertEqual(session.certified_epoch, session.state_epoch)
        self.assertFalse(any(code.startswith("certify_blocked") for code in PJ.CERTIFY_CODES))
        # Every reason the ledger vocabulary names, and a BLOCKED payload
        # naming none, classify the same way — directly on the seam (on a
        # nested copy: the seam records rows on whatever copy it is given).
        cases = (
            (engine_mod.BLOCKED_ON_HUMAN, "human"),
            (f"{engine_mod.SESSION_LIMIT_BLOCK_PREFIX}turns", "session_limit:turns"),
            ("", engine_mod.BLOCKED_ON_UNKNOWN),
        )
        for reason, expected in cases:
            with self.subTest(blocked_on=reason or "(none)"):
                def blocked(engine, task, reason=reason):
                    data = {engine_mod.BLOCKED_ON_KEY: reason} if reason else {}
                    return StageOutcome(engine_mod.VERDICT_BLOCKED, StagePayload(detail="waiting", data=data))

                with rp.trial_workspace(session.trial) as copy:
                    with self.assertRaises(S.ToolHarnessFault) as ctx2:
                        C.run_provider_free_stages(
                            copy, self.task_id, ("reference",), runners={"reference": blocked}
                        )
                self.assertEqual(
                    (ctx2.exception.code, ctx2.exception.cause_type, ctx2.exception.blocked_on),
                    ("stage_could_not_measure", f"blocked_on:{expected}", expected),
                )
        # The held trial is byte-unchanged by every halted certify.
        self.assertEqual(tree_hash(session.trial), held)

    def test_certify_model_caused_runner_exception_is_red_not_a_halt(self):
        """Phase 3 review finding 1-0 (C7 read the right way round): a runner
        exception the MODEL caused — the REAL reference runner's
        `duckdb.ParserException` on SQL the session broke, a mutant that
        lost its surface, the reference runner's divergence `ValueError` —
        is the scored red verdict the live ladder records as a FAIL row
        (`certify_red_<stage>`, a paid call, the exception text withheld),
        never a `ToolHarnessFault` that halts the session, the certifier and
        the whole repair for free. Only a `SessionFault`, an engine
        infrastructure class (chain-walked) or an OS / storage-engine /
        engine fault (`COULD_NOT_MEASURE_EXCEPTION_NAMES`) halts."""
        import sqlite3

        import duckdb

        self.materialize()
        runners = C.provider_free_stage_runners()  # the REAL generate + reference, provider-free
        sql = self.task.reference.sql_by_mart[MART]
        anchor = "SELECT DISTINCT order_id, customer_id"
        self.assertEqual(sql.count(anchor), 1)
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        applied = self.dispatch(
            session, "apply_edit_trial", artifact="task_ir.json", op="replace",
            locator=f"reference.sql_by_mart.{MART}", old=anchor,
            new="SELEKT DISTINCT order_id, customer_id", rationale="a typo the model made",
        )
        self.assertEqual(applied.code, "applied")
        held = tree_hash(session.trial)
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
            diag = self.dispatch(session, "certify")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual((diag.source, diag.code, diag.ok), (PJ.DiagnosticSource.CERTIFY, "certify_red_reference", False))
        self.assertEqual(dict(diag.flags), {"model_stages_deferred": False, "discrimination_weakened": False})
        self.assert_clean(diag, session)
        for secret in ("SELEKT", "Parser", "syntax", "4711", "/Users"):
            self.assertNotIn(secret, diag.render())
        # A paid, executed call with a receipt — the copy gone, the held
        # trial byte-unchanged — exactly like a runner that RETURNED red.
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        self.assertEqual(len(session.certify_receipts), 1)
        self.assertEqual(session.certify_receipts[-1].stages, ("reference",))  # the route's rerun set opens here
        self.assertFalse(session.certify_receipts[-1].copy_path.exists())
        self.assertEqual(tree_hash(session.trial), held)
        # The runner itself RAISES on the broken bytes (the live ladder
        # records that exception as a keyword-routed FAIL row).
        with rp.trial_workspace(session.trial) as copy:
            copy_engine = engine_mod.Engine(copy, max_repair_rounds=0)
            try:
                broken = copy_engine.load_task(self.task_id)
                with self.assertRaises(duckdb.ParserException):
                    runners["reference"](copy_engine, broken)
            finally:
                copy_engine.close()
        # On the seam, through both workers: red, the text withheld.
        with rp.trial_workspace(session.trial) as copy:
            red = C.run_provider_free_stages(
                copy, self.task_id, ("reference",), runners={"reference": parser_error_reference}
            )
        self.assertEqual((red.code, red.ok), ("certify_red_reference", False))
        receipt = C.certify_disposable_copy(
            self.workspace, self.task_id, ("reference",),
            runners={"reference": parser_error_reference}, worker=C.CERTIFY_WORKER_PROCESS,
        )
        self.assertEqual((receipt.diagnostic.code, receipt.worker), ("certify_red_reference", C.CERTIFY_WORKER_PROCESS))
        self.assertFalse(receipt.copy_path.exists())
        # Classification by CLASS: model-attributable exceptions measure red.
        for exc in (
            duckdb.ParserException("Parser Error: syntax error"),
            duckdb.BinderException("Binder Error: column not found"),
            duckdb.CatalogException("Catalog Error: table not found"),
            ValueError("reference count divergence: expected 4711 rows"),
            RuntimeError("no 'review' report in the ledger (fail closed)"),
            KeyError("status"),
            TypeError("unsupported operand"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(C.runner_exception_could_not_measure(exc))
                self.assertIsNone(C.classify_runner_exception(exc))
        # ... while the OS, the storage engines and the engine could not measure.
        for exc in (
            MemoryError("cannot allocate the mart"),
            OSError(28, "No space left on device"),
            FileNotFoundError(2, "gold.parquet"),
            duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate 512MB"),
            duckdb.IOException("IO Error: could not read block"),
            duckdb.FatalException("FATAL Error"),
            duckdb.InternalException("INTERNAL Error"),
            sqlite3.OperationalError("database is locked"),
            engine_mod.StageNotWiredError("stage 'reference' has no wired runner"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(C.runner_exception_could_not_measure(exc))
                fault = C.classify_runner_exception(exc)
                self.assertIsInstance(fault, S.ToolHarnessFault)
                self.assertEqual(fault.cause_type, type(exc).__name__)
                self.assertNotIn(str(exc), str(fault))
        for name in ("MemoryError", "OSError", "OperationalError", "FatalException", "InternalError", "InterruptException", "EngineError"):
            self.assertIn(name, C.COULD_NOT_MEASURE_EXCEPTION_NAMES)
        # A model-attributable class wrapping a transport fault is that fault
        # (the chain walk), and a session fault is itself.
        chained = ValueError("wrapped")
        chained.__cause__ = P.BudgetExceededError("budget", scope="task")
        self.assertTrue(C.runner_exception_could_not_measure(chained))
        self.assertIs(C.classify_runner_exception(chained), chained)
        deadline = S.ToolDeadlineExceeded("certify", deadline_s=1.0)
        self.assertIs(C.classify_runner_exception(deadline), deadline)

    def test_certify_declares_no_cost_refusal_codes_and_permit_hook(self):
        """`CertifyTool.cost` declares its two no-cost refusal codes
        (`ToolCost.no_cost_codes`) and its per-session ceiling, and
        `certify.permit` answers one of the codes at PERMIT — before any worker
        is spawned or bit charged — through `registry.permit_refusal`: on a
        SPECIFICATION route (no provider-free stage), on an unchanged trial,
        and '' once the trial moved. The hook touches no counter. A tool
        without the hook answers ''; a hook returning an UNDECLARED code or
        crashing is a wiring defect (`ToolHarnessFault`), its text withheld."""
        certify = V.proposer_tool("certify")
        self.assertEqual(
            certify.cost.no_cost_codes,
            frozenset({C.CODE_REFUSED_NO_PROVIDER_FREE_STAGE, C.CODE_REFUSED_UNCHANGED_TRIAL}),
        )
        self.assertEqual(certify.cost.no_cost_codes, C.NO_COST_REFUSAL_CODES)
        self.assertTrue(C.NO_COST_REFUSAL_CODES <= frozenset(PJ.CERTIFY_CODES))
        self.assertEqual(certify.cost.per_session, C.MAX_CERTIFY_PER_SESSION)
        self.assertEqual(RG.ToolCost().no_cost_codes, frozenset())
        with self.assertRaises(ValueError):
            RG.ToolCost(no_cost_codes=frozenset({"Not A Code"}))

        spec = self.open_session(RepairRoute.SPECIFICATION, "review")
        runners = {"generate": pass_runner(), "reference": pass_runner()}
        ref = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        with mock.patch.object(C, "certify_disposable_copy", side_effect=AssertionError("worker spawned")):
            self.assertEqual(
                RG.permit_refusal(certify, spec.context(), {}), C.CODE_REFUSED_NO_PROVIDER_FREE_STAGE
            )
            self.assertEqual(RG.permit_refusal(certify, ref.context(), {}), C.CODE_REFUSED_UNCHANGED_TRIAL)
        for session in (spec, ref):
            self.assertEqual(
                (session.tool_calls, session.certify_calls, session.certify_refusals, session.oracle_bits_used),
                (0, 0, 0, 0),
            )
        self.dispatch(ref, "apply_edit_trial", **reference_edit_args(self.task))
        self.assertEqual(RG.permit_refusal(certify, ref.context(), {}), "")
        self.assertEqual(ref.tool_calls, 1)
        # Every other proposer tool: no hook, no no-cost code, answers ''.
        for tool in V.PROPOSER_TOOLS:
            if tool.name == certify.name:
                continue
            with self.subTest(tool=tool.name):
                self.assertFalse(callable(getattr(tool, "permit", None)))
                self.assertEqual(tool.cost.no_cost_codes, frozenset())
                self.assertEqual(RG.permit_refusal(tool, ref.context(), {}), "")

        class Rogue:
            name = "rogue"
            cost = RG.ToolCost()

            def permit(self, ctx, args):
                return "rogue_refusal"

        with self.assertRaises(S.ToolHarnessFault) as undeclared:
            RG.permit_refusal(Rogue(), ref.context(), {})
        self.assertEqual((undeclared.exception.tool, undeclared.exception.code), ("rogue", "undeclared_refusal_code"))

        class Crashing:
            name = "crashing"
            cost = RG.ToolCost(no_cost_codes=frozenset({"crashing_refused"}))

            def permit(self, ctx, args):
                raise KeyError("secret 4711 at /Users/x/answer_key")

        with self.assertRaises(S.ToolHarnessFault) as crashed:
            RG.permit_refusal(Crashing(), ref.context(), {})
        self.assertEqual(
            (crashed.exception.tool, crashed.exception.code, crashed.exception.cause_type),
            ("crashing", "permit_hook_failed", "KeyError"),
        )
        self.assertNotIn("4711", str(crashed.exception))
        self.assertNotIn("answer_key", str(crashed.exception))

    def test_certify_deadline_is_harness_fault_not_policy(self):
        """An injected clock past the 300 s deadline: `ToolDeadlineExceeded`
        (a `SessionFault`, terminal HARNESS_FAULT, reward None), never a
        `PolicyFault` — for a deadline on a stage the edit did NOT feed (a
        direct call names no touched stage; `generate` on the REFERENCE
        route) or one that fired before any stage began. Re-pinned by the
        Phase 3 re-check verdict (docs/plans/bounded_agents_phase3.md §6): a
        deadline on a stage the edit FEEDS is neither a harness fault nor a
        policy fault but the PAID `certify_refused_resource_budget`
        (`test_certify_resource_budget_exhausted_by_the_edit_is_a_paid_outcome_not_a_halt`)."""
        release = threading.Event()
        self.addCleanup(release.set)
        armed = _ArmedClock()

        def stalled(engine, task):
            armed.expire()  # the deadline passes only once the stage is RUNNING
            release.wait(30.0)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="late"))

        class JumpingClock:
            def __init__(self):
                self.now = 1000.0

            def __call__(self):
                self.now += 200.0
                return self.now

        runners = {"generate": pass_runner(), "reference": stalled}
        with self.assertRaises(S.ToolDeadlineExceeded) as ctx:
            C.certify_disposable_copy(
                self.workspace, self.task_id, ("reference",), runners=runners,
                deadline_s=C.CERTIFY_DEADLINE_S, clock=JumpingClock(),
            )
        exc = ctx.exception
        self.assertEqual((exc.tool, exc.deadline_s), ("certify", 300.0))
        self.assertIsInstance(exc, S.SessionFault)
        self.assertNotIsInstance(exc, S.PolicyFault)
        self.assertIs(S.terminal_for_exception(exc), S.TerminalState.HARNESS_FAULT)
        self.assertIs(exc.failure_class, WorkspaceFailureClass.TRANSIENT_INFRASTRUCTURE)
        self.assertFalse(exc.label_eligible)
        self.assertEqual(engine_mod._infra_marker_for(exc), "ToolDeadlineExceeded")
        release.set()
        # A deadline on a stage the edit did NOT feed — `generate` stalls
        # while the REFERENCE edit feeds `reference` — is the same harness
        # fault, the worker cancelled and the copy reaped as always.
        release.clear()
        armed = _ArmedClock()
        with self.assertRaises(S.ToolDeadlineExceeded) as untouched:
            C.certify_disposable_copy(
                self.workspace, self.task_id, ("generate", "reference"),
                runners={"generate": stalled, "reference": pass_runner()},
                deadline_s=C.CERTIFY_DEADLINE_S, clock=armed, touched_stages=("reference", "attack"),
            )
        self.assertEqual(untouched.exception.deadline_s, 300.0)
        release.set()
        untouched.exception.worker.join(10.0)
        self.assertFalse(untouched.exception.worker.is_alive())
        # Through the tool, with the session's own deadline and clock: the
        # stalled stage IS the one the REFERENCE edit feeds, so the deadline
        # is the model's own exhaustion — a PAID receipt, never a fault.
        release.clear()
        armed = _ArmedClock()
        session = self.open_session(
            RepairRoute.REFERENCE, "reference", certify_runners=runners,
            certify_deadline_s=300.0, clock=armed,
        )
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        diag = self.dispatch(session, "certify")
        self.assertEqual((diag.code, diag.ok), (C.CODE_REFUSED_RESOURCE_BUDGET, False))
        # Charged at spawn: the deadline cut a PAID call short.
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        (receipt,) = session.certify_receipts
        self.assertEqual((receipt.resource_stage, receipt.worker), ("reference", C.CERTIFY_WORKER_THREAD))
        release.set()
        receipt.worker_handle.join(10.0)
        self.assertFalse(receipt.worker_handle.is_alive())
        # The declared wall matches the worker deadline the runner applies.
        self.assertEqual(V.proposer_tool("certify").cost.wall_s, C.CERTIFY_DEADLINE_S)
        self.assertEqual(C.CERTIFY_DEADLINE_S, 300.0)

    def test_certify_resource_budget_exhausted_by_the_edit_is_a_paid_outcome_not_a_halt(self):
        """Phase 3 re-check verdict (state machine §2 "paid outcomes versus
        harness faults"; C7 read the right way round, the deadline twin of
        finding 1-0): a resource-cap exhaustion the model's OWN edit caused
        on a stage that edit feeds — DuckDB `OutOfMemoryException` under the
        worker's `CERTIFY_MEMORY_LIMIT_MB`, an `InterruptException` out of
        the interrupted query, the worker's deadline — is the scored, PAID
        `certify_refused_resource_budget`: the 2 oracle bits charged at
        spawn stand, the executed call counts toward `max_certify`, a
        receipt names the stage, the exception text is withheld — never a
        `ToolHarnessFault` / `ToolDeadlineExceeded` halt the model could
        trigger at will by writing a cross join. Through the tool with both
        workers, on the seam, through the bounded runner, and by the rule."""
        import duckdb

        self.assertEqual(C.CODE_REFUSED_RESOURCE_BUDGET, "certify_refused_resource_budget")
        self.assertIn(C.CODE_REFUSED_RESOURCE_BUDGET, PJ.CERTIFY_CODES)
        self.assertIn(C.CODE_REFUSED_RESOURCE_BUDGET, PJ.codes_for("certify"))
        # PAID: never a no-cost refusal, of either certify declaration.
        self.assertNotIn(C.CODE_REFUSED_RESOURCE_BUDGET, C.NO_COST_REFUSAL_CODES)
        self.assertNotIn(C.CODE_REFUSED_RESOURCE_BUDGET, C.ATTACK_NO_COST_REFUSAL_CODES)
        for flagged in (False, True):
            self.assertNotIn(
                C.CODE_REFUSED_RESOURCE_BUDGET, V.proposer_tool("certify", attack_enabled=flagged).cost.no_cost_codes
            )
        self.assertEqual(C.RESOURCE_BUDGET_EXCEPTION_NAMES, frozenset({"OutOfMemoryException", "InterruptException"}))
        self.assertEqual(C.edit_touched_stages(RepairRoute.REFERENCE), ("reference", "attack"))
        self.assertEqual(C.edit_touched_stages(RepairRoute.POPULATION), ("generate", "reference", "attack"))
        self.assertEqual(C.edit_touched_stages(RepairRoute.SPECIFICATION), ())
        # The rule itself: a resource-budget class on a TOUCHED stage only.
        oom = duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate 512MB")
        interrupted = duckdb.InterruptException("INTERRUPT Error")
        self.assertTrue(C.runner_exception_exhausted_resource_budget(oom))
        self.assertTrue(C.runner_exception_exhausted_resource_budget(interrupted))
        self.assertFalse(C.runner_exception_exhausted_resource_budget(duckdb.IOException("IO Error")))
        self.assertFalse(C.runner_exception_exhausted_resource_budget(MemoryError("oom")))
        for exc in (oom, interrupted):
            self.assertTrue(C.resource_budget_exhausted_by_edit(exc, stage="reference", touched_stages=("reference",)))
            self.assertFalse(C.resource_budget_exhausted_by_edit(exc, stage="generate", touched_stages=("reference",)))
            self.assertFalse(C.resource_budget_exhausted_by_edit(exc, stage="reference", touched_stages=()))
            # Still a could-not-measure class when NOT attributed (the list is unchanged).
            self.assertTrue(C.runner_exception_could_not_measure(exc))
            self.assertIsInstance(C.classify_runner_exception(exc), S.ToolHarnessFault)
        chained = duckdb.OutOfMemoryException("wrapped")
        chained.__cause__ = P.BudgetExceededError("budget", scope="task")
        for never in (
            MemoryError("oom"), OSError(28, "No space left on device"), S.SandboxFault("worker died"), chained,
            S.ToolDeadlineExceeded("certify", deadline_s=1.0), ValueError("boom"),
        ):
            self.assertFalse(
                C.resource_budget_exhausted_by_edit(never, stage="reference", touched_stages=("reference",)),
                type(never).__name__,
            )
        # (a) Through the tool, in-process worker: out of memory, then an
        # interrupted query, on the stage the REFERENCE edit feeds.
        for runner in (oom_reference, interrupted_reference):
            with self.subTest(runner=runner.__name__):
                runners = {"generate": pass_runner(), "reference": runner}
                session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
                self.assertEqual(session.touched_stages(), ("reference", "attack"))
                self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
                held = tree_hash(session.trial)
                with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
                    diag = self.dispatch(session, "certify")
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(spawn.call_args.kwargs["touched_stages"], ("reference", "attack"))
                self.assertEqual(
                    (diag.source, diag.code, diag.ok),
                    (PJ.DiagnosticSource.CERTIFY, C.CODE_REFUSED_RESOURCE_BUDGET, False),
                )
                self.assertEqual(dict(diag.flags), {"model_stages_deferred": False, "discrimination_weakened": False})
                self.assert_clean(diag, session)
                for secret in ("512", "4711", "/Users", "answer_key", "Out of Memory", "INTERRUPT"):
                    self.assertNotIn(secret, diag.render())
                # PAID: charged at spawn, counted toward max_certify, a receipt
                # (the copy gone, the held trial byte-unchanged).
                self.assertEqual(
                    (session.certify_calls, session.oracle_bits_used, session.certified_epoch), (1, 2, 1)
                )
                (receipt,) = session.certify_receipts
                self.assertEqual(
                    (receipt.diagnostic.code, receipt.stages, receipt.worker, receipt.resource_stage),
                    (C.CODE_REFUSED_RESOURCE_BUDGET, ("reference",), C.CERTIFY_WORKER_THREAD, ""),
                )
                self.assertFalse(receipt.copy_path.exists())
                self.assertEqual(tree_hash(session.trial), held)
                # Nothing changed since: the next request is the no-cost refusal,
                # so a spent run is never re-spent for free either.
                self.assertEqual(self.dispatch(session, "certify").code, C.CODE_REFUSED_UNCHANGED_TRIAL)
                self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        # (b) The spawned process worker: the same code crosses as a RESULT.
        receipt = C.certify_disposable_copy(
            self.workspace, self.task_id, ("reference",), runners={"reference": oom_reference},
            worker=C.CERTIFY_WORKER_PROCESS, touched_stages=("reference", "attack"),
        )
        self.assertEqual((receipt.diagnostic.code, receipt.worker), (C.CODE_REFUSED_RESOURCE_BUDGET, C.CERTIFY_WORKER_PROCESS))
        self.assertFalse(receipt.copy_path.parent.exists())
        session = self.open_session(
            RepairRoute.REFERENCE, "reference", certify_runners={"generate": green_generate, "reference": oom_reference},
            certify_worker=C.CERTIFY_WORKER_PROCESS,
        )
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        self.assertEqual(self.dispatch(session, "certify").code, C.CODE_REFUSED_RESOURCE_BUDGET)
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (1, 2))
        # (c) On the seam, in a POPULATION session every provider-free member
        # is fed by the edit (conditions drive generation): `generate` out of
        # memory is the paid code there, and the projector is the flagged one
        # when the `attack` context is present.
        with rp.trial_workspace(self.workspace) as copy:
            paid = C.run_provider_free_stages(
                copy, self.task_id, ("generate", "reference"),
                runners={"generate": oom_generate, "reference": pass_runner()},
                touched_stages=C.edit_touched_stages(RepairRoute.POPULATION),
            )
        self.assertEqual((paid.code, paid.ok), (C.CODE_REFUSED_RESOURCE_BUDGET, False))
        self.assertEqual(PJ.project_certify_attack(C.CODE_REFUSED_RESOURCE_BUDGET, model_stages_deferred=True).flags["model_stages_deferred"], True)
        # (d) The supervisor's deadline verdict names the stage it cut short:
        # on the touched stage the PAID receipt, the killed worker in hand.
        with mock.patch.object(C, "_supervise_certify_worker", return_value=("deadline", "reference")):
            receipt = C.certify_disposable_copy(
                self.workspace, self.task_id, ("reference",), runners={"reference": slow_reference_writing_marker},
                worker=C.CERTIFY_WORKER_PROCESS, touched_stages=("reference",),
            )
        self.assertEqual((receipt.diagnostic.code, receipt.resource_stage), (C.CODE_REFUSED_RESOURCE_BUDGET, "reference"))
        self.assertFalse(receipt.worker_handle.is_alive())
        self.assertEqual(receipt.worker_handle.exitcode, -signal.SIGKILL)
        self.assertFalse(receipt.copy_path.parent.exists())
        # (e) Through the bounded runner: the paid outcome is an ordinary
        # tool turn (2 bits charged by the runner too), the session goes on
        # and submits; no fault, no security event.
        runners = {"generate": pass_runner(), "reference": oom_reference}
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        script = [
            [BS.tool_use("apply_edit_trial", reference_edit_args(self.task), "a0")],
            [BS.tool_use("certify", {}, "c0")],
            [BS.tool_use(V.SUBMIT_TOOL, {}, "s0")],
        ]
        limits = S.SessionLimits.declare(max_turns=8, max_tool_calls=8, max_certify=2, max_oracle_bits=4)
        result = S.run_bounded_session(
            ROLE, session.view, V.PROPOSER_TOOLS, V.proposer_policy(limits), limits,
            provider=BS.ScriptedProvider(script), ctx=session.context(),
            worker=V.proposer_validator_worker(),
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertIsNone(result.fault)
        self.assertEqual(result.security_events, ())
        executed = [t for t in result.turns if t.kind == "tool" and t.tool_name == "certify"]
        self.assertEqual([(t.outcome_code, t.refused) for t in executed], [(C.CODE_REFUSED_RESOURCE_BUDGET, False)])
        self.assertEqual((result.oracle_bits_used, session.oracle_bits_used, session.certify_calls), (2, 2, 1))

    def test_certify_resource_fault_on_an_untouched_stage_stays_a_harness_fault(self):
        """The other side of the Phase 3 re-check verdict: the same
        resource-budget class on a stage the edit did NOT feed, the
        pre-stage window, the whole-worker OS envelope (the RSS watchdog),
        a worker death and every OS / harness class of the UNCHANGED
        `COULD_NOT_MEASURE_EXCEPTION_NAMES` stay harness faults (C7: exit 2,
        reward None, nothing the model is charged a verdict for)."""
        import sqlite3

        import duckdb

        self.assertEqual(
            C.COULD_NOT_MEASURE_EXCEPTION_NAMES,
            frozenset({"MemoryError", "OSError", "OperationalError", "FatalException", "InternalError",
                       "InterruptException", "EngineError"}),
        )
        # Out of memory in `generate`, which a REFERENCE edit does not feed.
        with rp.trial_workspace(self.workspace) as copy:
            with self.assertRaises(S.ToolHarnessFault) as ctx:
                C.run_provider_free_stages(
                    copy, self.task_id, ("generate", "reference"),
                    runners={"generate": oom_generate, "reference": pass_runner()},
                    touched_stages=C.edit_touched_stages(RepairRoute.REFERENCE),
                )
        self.assertEqual((ctx.exception.code, ctx.exception.cause_type), ("harness_exception", "OutOfMemoryException"))
        self.assertNotIn("512", str(ctx.exception))
        # A direct call names no touched stage: every exhaustion is a fault,
        # through both workers.
        with self.assertRaises(S.ToolHarnessFault) as thread:
            C.certify_disposable_copy(self.workspace, self.task_id, ("reference",), runners={"reference": oom_reference})
        self.assertEqual(thread.exception.cause_type, "OutOfMemoryException")
        with self.assertRaises(S.ToolHarnessFault) as process:
            C.certify_disposable_copy(
                self.workspace, self.task_id, ("reference",), runners={"reference": interrupted_reference},
                worker=C.CERTIFY_WORKER_PROCESS,
            )
        self.assertEqual(process.exception.cause_type, "InterruptException")
        self.assertFalse(process.exception.copy_path.parent.exists())
        # The OS / harness classes on a TOUCHED stage: could not measure, never paid.
        for exc in (
            MemoryError("cannot allocate the mart"),
            OSError(28, "No space left on device"),
            duckdb.IOException("IO Error: could not read block"),
            duckdb.FatalException("FATAL Error"),
            duckdb.InternalException("INTERNAL Error"),
            sqlite3.OperationalError("database is locked"),
            engine_mod.StageNotWiredError("stage 'reference' has no wired runner"),
        ):
            with self.subTest(exc=type(exc).__name__):
                def raising(engine, task, exc=exc):
                    raise exc

                with rp.trial_workspace(self.workspace) as copy:
                    with self.assertRaises(S.ToolHarnessFault) as harness:
                        C.run_provider_free_stages(
                            copy, self.task_id, ("reference",), runners={"reference": raising},
                            touched_stages=("reference", "attack"),
                        )
                self.assertEqual(harness.exception.cause_type, type(exc).__name__)
        # The supervisor's other verdicts, whatever the stage: the RSS envelope
        # is the OS-level `SandboxFault(memory_limit)`, a deadline before any
        # stage began or on an untouched stage is `ToolDeadlineExceeded`.
        for verdict, expected in (
            (("memory", "reference"), S.SandboxFault),
            (("deadline", None), S.ToolDeadlineExceeded),
            (("deadline", "generate"), S.ToolDeadlineExceeded),
        ):
            with self.subTest(verdict=verdict):
                with mock.patch.object(C, "_supervise_certify_worker", return_value=verdict):
                    with self.assertRaises(expected) as fault:
                        C.certify_disposable_copy(
                            self.workspace, self.task_id, ("reference",),
                            runners={"reference": slow_reference_writing_marker},
                            worker=C.CERTIFY_WORKER_PROCESS, touched_stages=("reference",),
                        )
                self.assertIs(S.terminal_for_exception(fault.exception), S.TerminalState.HARNESS_FAULT)
                self.assertFalse(fault.exception.worker.is_alive())
                self.assertFalse(fault.exception.copy_path.parent.exists())
        # The supervisor reads the worker's progress messages and reports the
        # LAST stage that began (None before any) alongside its verdict.
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        self.addCleanup(receive.close)
        self.addCleanup(send.close)

        class FakeProcess:
            pid = os.getpid()

            @staticmethod
            def is_alive():
                return True

        send.send(("stage", "generate"))
        send.send(("stage", "reference"))
        self.assertEqual(
            C._supervise_certify_worker(
                FakeProcess(), receive, deadline_s=300.0, clock=_JumpingClock(), started=1000.0, rss_limit_bytes=2 ** 40,
            ),
            ("deadline", "reference"),
        )
        send.send(("stage", "reference"))
        send.send(("result", {"x": True}))
        self.assertEqual(
            C._supervise_certify_worker(
                FakeProcess(), receive, deadline_s=300.0, clock=time.monotonic, started=time.monotonic(), rss_limit_bytes=2 ** 40,
            ),
            ("result", {"x": True}),
        )

    def test_certify_is_capped_at_two_per_session(self):
        """SoT T1 RPR (`certify` <= 2): the ceiling is the tool's DECLARED
        `cost.per_session = MAX_CERTIFY_PER_SESSION`, so in a bounded session
        the third certify REQUEST is the runner's PERMIT-time `per_tool_cap`
        refusal — no worker spawned, no bit charged, the session goes on and
        submits — never an in-worker `OracleCapExceeded` ending it as
        LIMIT_ORACLE; refusals do not count toward the two. Dispatched
        directly (no runner in front of it) the tool's own `OracleCapExceeded`
        backstop still fails closed on a third executed call."""
        runners = {"generate": pass_runner(), "reference": pass_runner()}
        certify = V.proposer_tool("certify")
        self.assertEqual(certify.cost.per_session, C.MAX_CERTIFY_PER_SESSION)
        self.assertEqual(C.MAX_CERTIFY_PER_SESSION, 2)

        # The bounded session: the third request is refused at PERMIT and the
        # session submits its (twice-certified) draft.
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        script = []
        for index in range(3):
            script.append([BS.tool_use("apply_edit_trial", reference_edit_args(self.task, f"-- s{index}"), f"a{index}")])
            script.append([BS.tool_use("certify", {}, f"c{index}")])
        script.append([BS.tool_use(V.SUBMIT_TOOL, {}, "s0")])
        limits = S.SessionLimits.declare(max_turns=12, max_tool_calls=16, max_certify=2, max_oracle_bits=4)
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
            result = S.run_bounded_session(
                ROLE, session.view, V.PROPOSER_TOOLS, V.proposer_policy(limits), limits,
                provider=BS.ScriptedProvider(script), ctx=session.context(),
                worker=V.proposer_validator_worker(),
            )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(spawn.call_count, 2)  # the third request spawned nothing
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (2, 4))
        self.assertEqual(result.oracle_bits_used, 4)
        self.assertEqual(result.model_call_count, 7)
        refused = [t for t in result.turns if t.kind == "refused" and t.tool_name == "certify"]
        self.assertEqual([t.outcome_code for t in refused], ["per_tool_cap"])
        executed = [t for t in result.turns if t.kind == "tool" and t.tool_name == "certify"]
        self.assertEqual([t.outcome_code for t in executed], [C.CODE_GREEN, C.CODE_GREEN])
        self.assertEqual(len(session.accumulated_patch().edits), 3)
        self.assertEqual(session.certified_epoch, 2)  # the third edit was never certified

        # Direct dispatch, no runner: the tool's own backstop on the third
        # EXECUTED call (a no-cost refusal in between does not count).
        session2 = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        for index in range(2):
            self.dispatch(session2, "apply_edit_trial", **reference_edit_args(self.task, f"-- note {index}"))
            self.assertEqual(self.dispatch(session2, "certify").code, C.CODE_GREEN)
        self.assertEqual(self.dispatch(session2, "certify").code, C.CODE_REFUSED_UNCHANGED_TRIAL)
        self.dispatch(session2, "apply_edit_trial", **reference_edit_args(self.task, "-- note 2"))
        with self.assertRaises(S.OracleCapExceeded) as ctx:
            self.dispatch(session2, "certify")
        self.assertEqual((ctx.exception.tool, ctx.exception.terminal), ("certify", "LIMIT_ORACLE"))
        self.assertEqual((session2.certify_calls, session2.oracle_bits_used), (2, 4))

    def test_certify_oracle_bits_two_per_call_and_cap_four(self):
        """Two bits per executed call (`ok` + which of two stages), and the
        role's `max_oracle_bits: 4` admits exactly the two the per-tool
        ceiling admits: under the shipped block the third request is the
        PERMIT-time `per_tool_cap` refusal (no bits, no worker) and the
        session submits its twice-certified draft. With a tighter
        `max_oracle_bits: 2` the oracle cap binds first: the second request
        is the LIMIT_ORACLE stop at PERMIT (`oracle_cap_exceeded`, no worker
        spawned) with the last validator-green draft on hand.

        Which of `auto-submit` (certify addendum §3.1, §5) and BLOCKED with a
        salt (SoT T4's LIMIT_ORACLE row, which the runner follows) applies to
        the council-role disposition is the runner's reconciliation; this
        test pins what the tools guarantee — the bits, the stop and the green
        draft — and accepts either disposition of it."""
        certify = V.proposer_tool("certify")
        self.assertEqual(certify.cost.oracle_bits, C.CERTIFY_ORACLE_BITS)
        self.assertEqual(C.CERTIFY_ORACLE_BITS, 2)
        block = yaml.safe_load((repo_root() / "config" / "agents.yaml").read_text(encoding="utf-8"))
        session_block = block["roles"]["repair_proposer"]["session"]
        self.assertEqual((session_block["max_oracle_bits"], session_block["max_certify"]), (4, 2))
        self.assertTrue(session_block["enabled"])
        self.assertEqual(certify.cost.per_session, session_block["max_certify"])
        self.assertEqual(
            certify.cost.per_session * certify.cost.oracle_bits, session_block["max_oracle_bits"]
        )

        runners = {"generate": pass_runner(), "reference": pass_runner()}
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        script = [
            [BS.tool_use("apply_edit_trial", reference_edit_args(self.task, "-- one"), "a0")],
            [BS.tool_use("certify", {}, "c0")],
            [BS.tool_use("apply_edit_trial", reference_edit_args(self.task, "-- two"), "a1")],
            [BS.tool_use("certify", {}, "c1")],
            [BS.tool_use("certify", {}, "c2")],
            [BS.tool_use(V.SUBMIT_TOOL, {}, "s0")],
        ]
        # The shipped block's certify caps, with turns to spare so neither
        # `max_turns: 5` nor the tool-call cap is what answers the third request.
        limits = S.SessionLimits.declare(
            max_turns=8, max_tool_calls=session_block["max_tool_calls"],
            max_certify=session_block["max_certify"], max_oracle_bits=session_block["max_oracle_bits"],
        )
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
            result = S.run_bounded_session(
                ROLE, session.view, V.PROPOSER_TOOLS, V.proposer_policy(limits), limits,
                provider=BS.ScriptedProvider(script), ctx=session.context(),
                worker=V.proposer_validator_worker(),
            )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(result.oracle_bits_used, 4)
        self.assertEqual((session.certify_calls, session.oracle_bits_used), (2, 4))
        draft = session.current_draft()
        self.assertEqual(draft, session.accumulated_patch().model_dump(mode="json"))
        self.assertEqual(len(draft["edits"]), 2)
        self.assertEqual(session.certified_epoch, session.state_epoch)
        # The tool result the model saw for each executed certify was green
        # and named no stage but the code; the third was refused, not executed.
        certify_turns = [t for t in result.turns if t.kind == "tool" and t.tool_name == "certify"]
        self.assertEqual([t.outcome_code for t in certify_turns], [C.CODE_GREEN, C.CODE_GREEN])
        refused = [t for t in result.turns if t.kind == "refused" and t.tool_name == "certify"]
        self.assertEqual([t.outcome_code for t in refused], ["per_tool_cap"])

        # The oracle cap binding first: `max_oracle_bits: 2` admits one call;
        # the second REQUEST (a real one — the trial moved) is LIMIT_ORACLE at
        # PERMIT, no worker spawned, with the validator-green draft on hand.
        session2 = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        script2 = [
            [BS.tool_use("apply_edit_trial", reference_edit_args(self.task, "-- one"), "b0")],
            [BS.tool_use("certify", {}, "d0")],
            [BS.tool_use("apply_edit_trial", reference_edit_args(self.task, "-- two"), "b1")],
            [BS.tool_use("certify", {}, "d1")],
        ]
        limits2 = S.SessionLimits.declare(max_turns=8, max_tool_calls=8, max_certify=2, max_oracle_bits=2)
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn2:
            result2 = S.run_bounded_session(
                ROLE, session2.view, V.PROPOSER_TOOLS, V.proposer_policy(limits2), limits2,
                provider=BS.ScriptedProvider(script2), ctx=session2.context(),
                worker=V.proposer_validator_worker(),
            )
        self.assertIs(result2.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual(result2.limit, "oracle")
        self.assertEqual(spawn2.call_count, 1)
        self.assertEqual(result2.oracle_bits_used, 2)
        self.assertEqual((session2.certify_calls, session2.oracle_bits_used), (1, 2))
        self.assertEqual(len(session2.current_draft()["edits"]), 2)
        if result2.auto_submitted:
            # The LAST validator-green draft: the one certify d0 saw (one edit).
            self.assertEqual(len(result2.final["edits"]), 1)
        else:
            self.assertEqual(result2.blocked_on, "session_limit:oracle")
        certify_turns2 = [t for t in result2.turns if t.kind == "tool" and t.tool_name == "certify"]
        self.assertEqual([t.outcome_code for t in certify_turns2], [C.CODE_GREEN])

    def test_certify_runs_only_generate_and_reference_in_phase_1(self):
        """A POPULATION failure at `attack` runs `generate` and `reference`
        only (in order), with `model_stages_deferred=True`; `attack` is
        refused as a member; every route's filter agrees."""
        self.assertEqual(C._PROVIDER_FREE_STAGES, ("generate", "reference"))
        self.assertEqual(C.PROVIDER_FREE_STAGES, C._PROVIDER_FREE_STAGES)
        self.assertEqual(C.provider_free_stages(RepairRoute.POPULATION, "attack"), ("generate", "reference"))
        self.assertEqual(C.provider_free_stages(RepairRoute.POPULATION, "generate"), ("generate",))
        self.assertEqual(C.provider_free_stages(RepairRoute.REFERENCE, "attack"), ("reference",))
        self.assertEqual(C.provider_free_stages(RepairRoute.REFERENCE, "reference"), ("reference",))
        self.assertEqual(C.provider_free_stages(RepairRoute.REFERENCE, "gates"), ("reference",))
        self.assertEqual(C.provider_free_stages(RepairRoute.RUNTIME, "generate"), ())
        self.assertTrue(C.model_stages_deferred(RepairRoute.POPULATION, "attack"))
        self.assertFalse(C.model_stages_deferred(RepairRoute.REFERENCE, "reference"))
        self.assertTrue(C.model_stages_deferred(RepairRoute.REFERENCE, "attack"))

        invoked: list[str] = []

        def recording(stage):
            def run(engine, task):
                invoked.append(stage)
                return StageOutcome(VERDICT_PASS, StagePayload(detail=f"{stage} ok"))

            return run

        runners = {
            stage: recording(stage)
            for stage in ("generate", "reference", "review", "attack", "gates", "gates_extract_load", "gates_transform")
        }
        session = self.open_session(RepairRoute.POPULATION, "attack", certify_runners=runners)
        applied = self.dispatch(session, "apply_edit_trial", **population_edit_args())
        self.assertEqual((applied.code, applied.subject), ("applied", "population"))
        verdict = self.dispatch(session, "certify")
        self.assertEqual(invoked, ["generate", "reference"])
        self.assertEqual(verdict.code, C.CODE_GREEN)
        self.assertTrue(verdict.flags["model_stages_deferred"])
        self.assertFalse(verdict.flags["discrimination_weakened"])
        self.assertEqual(session.certify_receipts[-1].stages, ("generate", "reference"))
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            C.run_provider_free_stages(session.trial, self.task_id, ("generate", "attack"), runners=runners)
        self.assertEqual(ctx.exception.code, "stage_not_provider_free")
        self.assertEqual(invoked, ["generate", "reference"])
        # The copy's ledger carried each member's row (currency for the next
        # member); the held trial's ledger got none.
        trial_engine = Engine(session.trial)
        self.addCleanup(trial_engine.close)
        self.assertIsNone(trial_engine.latest_report(self.task_id, "generate"))


# ---------------------------------------------------------------------------
# read_field, apply_edit_trial, validate_scope
# ---------------------------------------------------------------------------

class EditingToolsTest(ProposerToolsCase):
    def test_read_field_refuses_literal_rows_even_on_population_route(self):
        """`populations.*.literal_rows` is a `ForbiddenArgument` on EVERY route
        (C5; OQ-21 not reopened), as is any private field; public schema
        paths project identifiers only; an off-route public field is a
        refusal code."""
        session = self.open_session(RepairRoute.POPULATION, "generate")
        for path in (
            "populations.3.literal_rows",
            "populations.3.literal_rows.orders",
            "populations.3.literal_rows.orders.0",
            "populations.3.literal_rows.orders.0.status",
        ):
            with self.subTest(path=path):
                self.assertFalse(V.field_is_readable(path, RepairRoute.POPULATION))
                with self.assertRaises(S.ForbiddenArgument) as ctx:
                    self.dispatch(session, "read_field", field=path)
                self.assertEqual((ctx.exception.code, ctx.exception.detail), ("forbidden_argument", "literal_rows"))
                self.assertTrue(ctx.exception.security_event)
        for pattern in V.LITERAL_ROWS_PATHS:
            self.assertNotIn(pattern, V.readable_field_paths(RepairRoute.POPULATION))
        self.assertIn("populations.*.conditions.*", V.readable_field_paths(RepairRoute.POPULATION))
        # The route's own text field is acknowledged (it is in the view), a
        # declared scale (a number) is withheld, schema paths are identifiers.
        self.assertEqual(self.dispatch(session, "read_field", field="populations.1.conditions.1").code, "field_text_in_view")
        self.assertEqual(self.dispatch(session, "read_field", field="populations.1.scale.orders").code, "field_withheld")
        columns = self.dispatch(session, "read_field", field="tables.1.columns")
        self.assertEqual((columns.code, columns.ok), ("field_value", True))
        self.assertTrue(set(columns.names) <= set(PJ.PublicIdentifierSet(self.task)))
        typed = self.dispatch(session, "read_field", field="tables.0.columns.0.type")
        self.assertEqual((typed.code, typed.subject), ("field_value", self.task.tables[0].columns[0].type.value))
        self.assertEqual(self.dispatch(session, "read_field", field="tables.9").code, "field_not_found")
        # Private on every route: attack cases; off-route: another route's material.
        for route, path in (
            (RepairRoute.POPULATION, "attack_cases.0.mutation"),
            (RepairRoute.SPECIFICATION, f"reference.sql_by_mart.{MART}"),
            (RepairRoute.REFERENCE, "populations.1.conditions.0"),
        ):
            with self.subTest(route=route.value, path=path):
                other = self.open_session(route, "generate" if route is RepairRoute.POPULATION else "reference")
                with self.assertRaises(S.ForbiddenArgument):
                    self.dispatch(other, "read_field", field=path)
        reference = self.open_session(RepairRoute.REFERENCE, "reference")
        off_route = self.dispatch(reference, "read_field", field="solver_prompt")
        self.assertEqual((off_route.code, off_route.ok), ("field_outside_allowlist", False))
        self.assertEqual(self.dispatch(reference, "read_field", field=f"reference.sql_by_mart.{MART}").code, "field_text_in_view")
        for diag in (columns, typed, off_route):
            self.assert_clean(diag, session if diag is not off_route else reference)

    def test_apply_edit_trial_refuses_a_second_artifact_in_one_session(self):
        """`RepairPatch.artifact` is singular: the session's artifact is fixed
        by its first applied edit and a different one is a
        `ToolProtocolFault` (a correction), leaving the trial and the
        accumulated patch untouched. The wire schema admits only the
        session artifacts; the reference file tree is unreachable because
        the argument policy denies `answer_key/`."""
        session = self.open_session(RepairRoute.REFERENCE, "reference")
        schema = V.proposer_tool("apply_edit_trial").input_schema
        self.assertEqual(schema["properties"]["artifact"]["enum"], list(V.SESSION_ARTIFACTS))
        self.assertEqual(V.SESSION_ARTIFACTS, ("task_ir.json",))
        self.assertEqual(S._forbidden_argument({"artifact": "answer_key/reference/solution.sql"}), "denied_tree")
        first = self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        self.assertEqual(first.code, "applied")
        self.assertEqual(session.artifact, "task_ir.json")
        bytes_after_first = session.trial_text()
        with mock.patch.object(V, "SESSION_ARTIFACTS", ("task_ir.json", "other.json")):
            args = dict(reference_edit_args(self.task, "-- again"), artifact="other.json")
            with self.assertRaises(S.ToolProtocolFault) as ctx:
                self.dispatch(session, "apply_edit_trial", **args)
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("invalid_arguments", "second_artifact_in_one_session"))
        self.assertFalse(ctx.exception.ends_session)
        self.assertEqual(session.artifact, "task_ir.json")
        self.assertEqual(len(session.edits), 1)
        self.assertEqual(session.trial_text(), bytes_after_first)
        # An artifact outside the session's set is an argument error too.
        with self.assertRaises(S.ToolProtocolFault):
            self.dispatch(session, "apply_edit_trial", **dict(reference_edit_args(self.task), artifact="other.json"))
        patch = session.accumulated_patch()
        self.assertIsInstance(patch, RepairPatch)
        self.assertEqual((patch.artifact, len(patch.edits), patch.route), ("task_ir.json", 1, RepairRoute.REFERENCE))
        self.assertEqual(patch.proposer_role, rp.ROLE_NAME)

    def test_validate_scope_runs_on_every_apply_edit(self):
        """`validate_scope` runs on EVERY `apply_edit_trial` that reaches the
        trial (S8 §3.1 seam fact 2), and an edit whose LOCATOR is outside the
        route is refused BEFORE any anchor is applied — the same code
        `validate_scope` would answer (`scope_route_mismatch` for another
        route's field, `scope_field_outside_allowlist` for a field no route
        edits), the trial byte-unchanged, so the held trial carries validated
        bytes only and an out-of-route anchor is never tried."""
        session = self.open_session(RepairRoute.REFERENCE, "reference")
        with mock.patch.object(rp, "validate_scope", wraps=rp.validate_scope) as spy, \
                mock.patch.object(rp, "apply_patch_text", wraps=rp.apply_patch_text) as anchor:
            wrong_route = self.dispatch(session, "apply_edit_trial", **spec_edit_args())
            self.assertEqual((spy.call_count, anchor.call_count), (0, 0))
            self.assertEqual((wrong_route.source.value, wrong_route.code), ("rejection", "scope_route_mismatch"))
            self.assertEqual(session.trial_text(), session.before_ir)
            self.assertEqual(session.edits, [])
            self.assertEqual(session.state_epoch, 0)

            applied = self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
            self.assertEqual((spy.call_count, anchor.call_count), (1, 1))
            self.assertEqual(applied.code, "applied")
            validated = session.trial_text()
            self.assertIn("-- clarified reference", validated)

            outside = self.dispatch(
                session, "apply_edit_trial", artifact="task_ir.json", op="replace",
                locator="marts.0.grain", old=self.task.marts[0].grain, new="one row per order",
                rationale="not this route's field",
            )
            self.assertEqual((spy.call_count, anchor.call_count), (1, 1))
            self.assertEqual((outside.source.value, outside.code), ("rejection", "scope_field_outside_allowlist"))
            self.assertEqual(session.trial_text(), validated)
            self.assertEqual(len(session.edits), 1)
            self.assertEqual(session.state_epoch, 1)
            # A private field is a violation, never a scope refusal, and runs no check.
            with self.assertRaises(S.ForbiddenArgument):
                self.dispatch(
                    session, "apply_edit_trial", artifact="task_ir.json", op="replace",
                    locator="attack_cases.0.mutation", old="x", new="y", rationale="never",
                )
            self.assertEqual((spy.call_count, anchor.call_count), (1, 1))
        for diag in (wrong_route, applied, outside):
            self.assert_clean(diag, session)
        self.assertEqual(self.dispatch(session, "check_scope").code, "scope_ok")

    def test_validate_scope_baseline_is_the_live_snapshot(self):
        """`before` / `before_ir` are the LIVE workspace's snapshot taken once
        at session INIT — not the trial's post-edit state, not a later live
        state — for every apply, and `certify` never changes them."""
        live_snapshot = repair.snapshot(self.workspace, self.task_id)
        live_ir = (self.workspace / "tasks" / self.task_id / "task_ir.json").read_text(encoding="utf-8")
        runners = {"generate": pass_runner(), "reference": pass_runner()}
        session = self.open_session(RepairRoute.REFERENCE, "reference", certify_runners=runners)
        self.assertEqual(session.before, live_snapshot)
        self.assertEqual(session.before_ir, live_ir)
        seen: list[tuple[dict, str]] = []
        original = rp.validate_scope

        def recording(trial, task_id, patch, before, after, *, before_ir):
            seen.append((dict(before), before_ir))
            return original(trial, task_id, patch, before, after, before_ir=before_ir)

        with mock.patch.object(rp, "validate_scope", side_effect=recording):
            self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
            trial_after_first = repair.snapshot(session.trial, self.task_id)
            self.assertNotEqual(trial_after_first, live_snapshot)
            self.dispatch(session, "certify")
            self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task, "-- second"))
            self.dispatch(session, "check_scope")
        self.assertEqual(len(seen), 3)
        for before, before_ir in seen:
            self.assertEqual(before, live_snapshot)
            self.assertEqual(before_ir, live_ir)
        self.assertNotEqual(seen[1][0], trial_after_first)
        self.assertEqual(session.before, live_snapshot)
        # A later change to the LIVE file does not move the INIT baseline.
        path = self.workspace / "tasks" / self.task_id / "task_ir.json"
        path.write_text(live_ir + "\n", encoding="utf-8")
        self.assertEqual(session.before, live_snapshot)
        self.assertEqual(session.before_ir, live_ir)


# ---------------------------------------------------------------------------
# The matrix, the argument rules, the value-free contract, no nested call
# ---------------------------------------------------------------------------

class MatrixTest(ProposerToolsCase):
    def test_every_tool_result_passes_assert_result_clean_on_demo_task(self):
        """Every result of every proposer tool — successes, refusals and
        rejections, on the SPECIFICATION and the REFERENCE route — passes
        `serialize_for_transport` and the D1 gatekeeper and renders without
        a digit or a path."""
        runners = {"generate": pass_runner(), "reference": pass_runner()}
        checked = 0
        for route, stage, edit in (
            (RepairRoute.SPECIFICATION, "review", spec_edit_args()),
            (RepairRoute.REFERENCE, "reference", reference_edit_args(self.task)),
        ):
            session = self.open_session(route, stage, certify_runners=runners)
            calls = [
                ("read_view", {}),
                ("read_field", {"field": "tables"}),
                ("read_field", {"field": "tables.0"}),
                ("read_field", {"field": "relationships.0"}),
                ("read_field", {"field": "solver_prompt"}),
                ("read_field", {"field": "marts.0.columns.0"}),
                ("check_scope", {}),
                ("check_cheap", {}),
                ("certify", {}),
                ("apply_edit_trial", edit),
                ("apply_edit_trial", edit),  # the anchor is consumed: a rejection
                ("check_scope", {}),
                ("check_cheap", {}),
                ("certify", {}),
                ("certify", {}),
                ("submit_patch", {}),
                ("abort", {"reason_code": "cannot_repair"}),
            ]
            for name, args in calls:
                with self.subTest(route=route.value, tool=name, args=args):
                    diag = self.dispatch(session, name, **args)
                    self.assertIsInstance(diag, PJ.Diagnostic)
                    self.assert_clean(diag, session)
                    checked += 1
        self.assertEqual(checked, 34)
        # Every tool object satisfies the registry's Tool protocol.
        for tool in V.PROPOSER_TOOLS:
            self.assertIsInstance(tool, RG.Tool)

    def test_manifest_per_role_matches_matrix_table(self):
        """The declared proposer manifest is exactly the matrix's RPR column
        (names, ceilings, bits, walls, reads, writes, validators, terminals);
        every role registry matches the shipped enabled/disabled profile;
        and an explicit proposer `session.enabled: false` rollback removes
        its tools from the wire and changes its behaviour digest."""
        declared = RG.ToolRegistry.declared_for_role(ROLE)
        self.assertEqual(set(declared.names), set(MATRIX))
        self.assertEqual(V.PROPOSER_TOOL_NAMES, tuple(t.name for t in V.PROPOSER_TOOLS))
        for name, row in MATRIX.items():
            tool = declared.get(name)
            with self.subTest(tool=name):
                self.assertEqual(tool.cost.oracle_bits, row["bits"])
                self.assertEqual(tool.cost.per_session, row["per_session"])
                self.assertEqual(tool.cost.no_cost_codes, frozenset(row["no_cost"]))
                self.assertEqual(callable(getattr(tool, "permit", None)), bool(row["no_cost"]))
                self.assertEqual(tool.cost.idempotent_read, row["idempotent"])
                self.assertEqual(tool.cost.wall_s, row["wall_s"])
                self.assertEqual(bool(getattr(tool, "surface_write", False)), row["write"])
                self.assertEqual(bool(getattr(tool, "validator", False)), row["validator"])
                self.assertEqual(bool(getattr(tool, "terminal", False)), row["terminal"])
                self.assertEqual(tool.permitted_roles, frozenset({ROLE}))
                self.assertTrue(tool.description)
        self.assertEqual(V.MODEL_FACING_TOOL_NAMES, tuple(n for n in V.PROPOSER_TOOL_NAMES if not MATRIX[n]["terminal"]))
        _DECLARED_NAMES = {
            V.AUTHOR_ROLE: tuple(sorted(V.AUTHOR_TOOL_NAMES)),
            V.IMPLEMENTER_ROLE: tuple(sorted(V.IMPLEMENTER_TOOL_NAMES)),
            V.LOADER_ROLE: tuple(sorted(V.LOADER_TOOL_NAMES)),
        }
        _ACTIVE_NAMES = {
            V.AUTHOR_ROLE: tuple(sorted(V.AUTHOR_TOOL_NAMES)),
            "population_adversary": ("compile_proposal",),
            "shortcut_attacker": ("compile_probe",),
            V.IMPLEMENTER_ROLE: tuple(sorted(V.IMPLEMENTER_TOOL_NAMES)),
            V.LOADER_ROLE: tuple(sorted(V.LOADER_TOOL_NAMES)),
        }
        for role in ROLES:
            if role == ROLE:
                continue
            self.assertEqual(RG.ToolRegistry.for_role(role).names, _ACTIVE_NAMES.get(role, ()), role)
            self.assertEqual(
                RG.ToolRegistry.declared_for_role(role).names,
                _DECLARED_NAMES.get(role, ()),
                role,
            )
        # Shipped config: enabled -> bounded tools are active and behavior-bound.
        P.clear_behavior_caches()
        self.assertEqual(set(RG.ToolRegistry.for_role(ROLE).names), set(MATRIX))
        self.assertTrue(P.role_is_agentic(ROLE))
        self.assertEqual(set(t["name"] for t in P.wire_tools_for(ROLE)), set(MATRIX))
        self.assertEqual(set(t.name for t in P.session_policy_for(ROLE).tools), set(MATRIX))
        enabled_sha = P.role_behavior_sha256(ROLE)
        # Explicit rollback: disabled -> off the wire and one-shot behavior.
        doc = json.loads(json.dumps(P._agents_doc()))
        doc["roles"][ROLE]["session"]["enabled"] = False
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            try:
                self.assertEqual(RG.ToolRegistry.for_role(ROLE).names, ())
                self.assertFalse(P.role_is_agentic(ROLE))
                self.assertEqual(P.session_policy_for(ROLE).tools, ())
                self.assertEqual([t["name"] for t in P.wire_tools_for(ROLE)], [])
                self.assertNotEqual(P.role_behavior_sha256(ROLE), enabled_sha)
            finally:
                P.clear_behavior_caches()
        self.assertEqual(set(RG.ToolRegistry.for_role(ROLE).names), set(MATRIX))
        # A declared name of another role is `tool_not_permitted`, not unknown.
        ctx = RG.ToolContext(root=self.workspace, task_id=self.task_id, role="independent_implementer")
        with self.assertRaises(RG.ToolLookupError) as lookup:
            RG.ToolRegistry("independent_implementer", ()).lookup(ctx, "certify")
        self.assertEqual(lookup.exception.kind, "tool_not_permitted")
        self.assertIsInstance(lookup.exception.as_policy_fault(), S.ToolNotPermitted)

    def test_no_tool_accepts_a_population_argument(self):
        """The population is a constant of the tool context (DEVELOPMENT):
        no proposer tool schema names one at any depth, the policy refuses
        a tool that would, and the argument rule refuses one on the wire."""
        self.assertEqual(RG.ToolContext.population, "development")
        self.assertEqual(V.ProposerToolContext.population, "development")
        self.assertNotIn("population", inspect.signature(V.ProposerToolContext).parameters)

        def property_names(node, out):
            if isinstance(node, dict):
                if isinstance(node.get("properties"), dict):
                    out.update(node["properties"])
                for value in node.values():
                    property_names(value, out)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    property_names(value, out)
            return out

        for tool in V.PROPOSER_TOOLS:
            names = property_names(dict(tool.input_schema), set())
            self.assertNotIn(S.FORBIDDEN_ARG_NAME, names, tool.name)
            self.assertNotIn("population", inspect.signature(tool.run).parameters, tool.name)
        V.proposer_policy()  # constructs: no tool declares a population
        self.assertEqual(S._forbidden_argument({"population": "primary"}), "population_argument")
        self.assertEqual(S._forbidden_argument({"field": "tables.0", "population": "stress"}), "population_argument")

    def test_no_tool_has_free_path_or_url_argument(self):
        """Every argument is structured: enum-typed or pattern-bound
        identifiers, never a path, URL or file name; the runner's argument
        policy passes every legitimate call and refuses a path-shaped value."""
        banned = {"path", "url", "uri", "file", "filename", "href", "dir", "directory", "relative_path"}
        for tool in V.PROPOSER_TOOLS:
            schema = dict(tool.input_schema)
            self.assertIs(schema.get("additionalProperties"), False, tool.name)
            self.assertFalse(banned & set(schema.get("properties", {})), tool.name)
            for prop, spec in schema.get("properties", {}).items():
                self.assertEqual(spec.get("type"), "string", f"{tool.name}.{prop}")
                self.assertTrue("enum" in spec or "pattern" in spec or "maxLength" in spec, f"{tool.name}.{prop}")
        for schema, args in (
            (V.proposer_tool("read_field").input_schema, {"field": "a/b"}),
            (V.proposer_tool("read_field").input_schema, {"field": "../x"}),
            (V.proposer_tool("read_field").input_schema, {"field": "/etc/passwd"}),
            (V.proposer_tool("apply_edit_trial").input_schema, dict(spec_edit_args(), locator="answer_key/gold")),
            (V.proposer_tool("apply_edit_trial").input_schema, dict(spec_edit_args(), artifact="answer_key/reference/solution.sql")),
        ):
            self.assertIsNotNone(S.validate_args(schema, args), args)
        legitimate = [
            ("read_view", {}),
            ("read_field", {"field": "tables.0.columns.1.type"}),
            ("apply_edit_trial", reference_edit_args(self.task)),
            ("apply_edit_trial", spec_edit_args()),
            ("check_scope", {}),
            ("check_cheap", {}),
            ("certify", {}),
            ("submit_patch", {}),
            ("abort", {"reason_code": "infeasible"}),
        ]
        for name, args in legitimate:
            with self.subTest(tool=name):
                self.assertIsNone(S.validate_args(V.proposer_tool(name).input_schema, args))
                self.assertEqual(S._forbidden_argument(args), "")
        # A path-typed argument is judged by the path rules (a violation); a
        # free-text argument naming a private path is at most a correction.
        self.assertEqual(S._forbidden_argument({"new": "/etc/passwd"}), "")
        self.assertEqual(S._free_text_problem({"new": "/etc/passwd"}), S.FREE_TEXT_PRIVATE_PATH_CODE)
        self.assertEqual(S._forbidden_argument({"locator": "runs/x"}), "denied_tree")
        self.assertEqual(S._forbidden_argument({"locator": "answer_key/gold"}), "denied_tree")

    def test_no_session_tool_makes_a_nested_model_call(self):
        """A whole bounded session — every tool, two REAL certifies — pops
        exactly the proposer's own turns from the transport FIFO, reaches no
        `complete`, records no exchange evidence, and leaves the live
        workspace byte-identical; the accumulated patch is what
        `submit_patch` hands on."""
        self.materialize()
        live_before = tree_hash(self.workspace)
        session = self.open_session(RepairRoute.REFERENCE, "reference")  # default (real) runners
        second_edit = {
            "artifact": "task_ir.json", "op": "insert",
            "locator": f"reference.sql_by_mart.{MART}", "old": "", "new": "\n-- appended note",
            "rationale": "note the dedupe rule",
        }
        script = [
            [BS.tool_use("read_view", {}, "t0")],
            [BS.tool_use("read_field", {"field": "tables"}, "t1")],
            [BS.tool_use("apply_edit_trial", reference_edit_args(self.task), "t2")],
            [BS.tool_use("check_scope", {}, "t3")],
            [BS.tool_use("check_cheap", {}, "t4")],
            [BS.tool_use("certify", {}, "t5")],
            [BS.tool_use("apply_edit_trial", second_edit, "t6")],
            [BS.tool_use("certify", {}, "t7")],
            [BS.tool_use(V.SUBMIT_TOOL, {}, "t8")],
        ]
        transport = BS.ScriptedProvider(script)
        transport.complete = self.provider.complete  # the one-shot seam, never reached
        transport.exchange_evidence = self.provider.exchange_evidence
        limits = S.SessionLimits.declare(max_turns=12, max_tool_calls=16, max_certify=2, max_oracle_bits=4)
        # The worker is a spawned child: the parent-side spy is the SPAWN
        # site (`certify_disposable_copy`), one per executed certify.
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as runs:
            result = S.run_bounded_session(
                ROLE, session.view, V.PROPOSER_TOOLS, V.proposer_policy(limits), limits,
                provider=transport, ctx=session.context(), worker=V.proposer_validator_worker(),
            )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(transport.script, [])
        self.assertEqual(len(transport.calls), 9)
        self.assertEqual(result.model_call_count, 9)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.provider.exchange_evidence, [])
        self.assertEqual(runs.call_count, 2)
        self.assertEqual(result.tool_call_count, 8)
        self.assertEqual(result.oracle_bits_used, 4)
        self.assertEqual(session.certify_calls, 2)
        self.assertEqual([r.diagnostic.code for r in session.certify_receipts], [C.CODE_GREEN, C.CODE_GREEN])
        self.assertEqual([r.worker for r in session.certify_receipts], [C.CERTIFY_WORKER_PROCESS] * 2)
        self.assertEqual(tree_hash(self.workspace), live_before)
        patch = session.accumulated_patch()
        self.assertEqual((patch.route, patch.artifact, len(patch.edits)), (RepairRoute.REFERENCE, "task_ir.json", 2))
        self.assertEqual(rp.apply_patch_text(session.before_ir, patch), session.trial_text())
        # Every tool_result the model saw was a rendered projection.
        answered = [
            block for call in transport.calls for message in call["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"
        ]
        self.assertTrue(answered)
        for block in answered:
            self.assertRegex(block["content"], r"^\[(trial|field|cheap|certify|rejection)\] ")
            self.assertIsNone(re.search(r"\d", block["content"]), block["content"])
        self.assertTrue(result.verify_chain())


# ---------------------------------------------------------------------------
# Phase 1 review remediation (docs/plans/bounded_agents_phase1.md §2):
# the anchor is never an oracle; certify is cancellable
# ---------------------------------------------------------------------------

def _string_leaves(node, path: str = ""):
    """(dotted path, value) of every string leaf of a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _string_leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _string_leaves(value, f"{path}.{index}")
    elif isinstance(node, str):
        yield path, node


class _JumpingClock:
    """Every reading is 200 s later than the last: the second poll of a
    300 s deadline has expired."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 200.0
        return self.now


class LocatorScopeTest(ProposerToolsCase):
    """Finding 0-0 / 3-0: `apply_edit_trial` decides scope from the LOCATOR
    before any anchor is applied, so a field the route may neither read nor
    edit answers ONE code whether or not `old` occurs in it."""

    def _edit(self, session, locator, old):
        return self.dispatch(
            session, "apply_edit_trial", artifact="task_ir.json", op="replace",
            locator=locator, old=old, new="x" + old, rationale="a probe",
        )

    def test_apply_edit_trial_answers_out_of_route_locators_before_the_anchor(self):
        """For every route and every string field outside the route's
        editable set (and not private), a TRUE substring and an ABSENT one
        answer the same rejection code, `apply_patch_text` is never reached,
        the trial bytes and the epoch are unchanged: the anchor is no
        substring oracle over plan predicates, revision reasons, ids or the
        reference's inert fields."""
        cases = (
            (RepairRoute.SPECIFICATION, "review"),
            (RepairRoute.REFERENCE, "reference"),
            (RepairRoute.POPULATION, "generate"),
        )
        for route, failed_stage in cases:
            with self.subTest(route=route.value):
                session = self.open_session(route, failed_stage)
                doc = json.loads(session.trial_text())
                probes = [
                    (path, value)
                    for path, value in _string_leaves(doc)
                    if len(value) >= 4
                    and not V.field_is_editable(path, route)
                    and not V._names_private_field(path, route)
                ]
                self.assertGreater(len(probes), 20)
                before = session.trial_text()
                with mock.patch.object(rp, "apply_patch_text", side_effect=AssertionError("anchor applied")):
                    for path, value in probes:
                        hit = self._edit(session, path, value[: max(1, len(value) // 2)])
                        miss = self._edit(session, path, "zzz-never-in-any-field-zzz")
                        self.assertEqual((hit.source.value, hit.code), ("rejection", miss.code), path)
                        self.assertIn(
                            hit.code, {"scope_field_outside_allowlist", "scope_route_mismatch"}, path
                        )
                        self.assert_clean(hit, session)
                self.assertEqual(session.trial_text(), before)
                self.assertEqual((session.state_epoch, session.edits), (0, []))
                # The compiler-only plan details are reference SQL by another
                # name: naming them is a ForbiddenArgument on every route, for
                # a read and for an edit, whatever the anchor.
                for path in (
                    "marts.0.plan.ops.5.details.select",
                    "marts.0.plan.ops.0.details.sql",
                    "marts.1.plan.ops.2.details.name",
                ):
                    with self.assertRaises(S.ForbiddenArgument) as read:
                        self.dispatch(session, "read_field", field=path)
                    self.assertEqual(read.exception.detail, "private_field")
                    for old in ("SUM(ot.order_total)", "zzz-never"):
                        with self.assertRaises(S.ForbiddenArgument) as edit:
                            self._edit(session, path, old)
                        self.assertEqual(edit.exception.detail, "private_field")
        self.assertEqual(
            V.COMPILER_ONLY_PLAN_PATHS,
            tuple(f"marts.*.plan.ops.*.details.{k}" for k in sorted(V._COMPILER_ONLY_DETAIL_KEYS)),
        )
        self.assertEqual(set(V._COMPILER_ONLY_DETAIL_KEYS), {"select", "sql", "name"})

    def test_locator_list_index_must_be_a_canonical_unsigned_integer(self):
        """Phase 3 re-check verdict (locator canonicalization): a list-index
        segment of `read_field.field` / `apply_edit_trial.locator` has ONE
        spelling, the unsigned canonical integer (`0 | [1-9][0-9]*`). Every
        alias Python's `int()` would also read — negative (`-1` is the LAST
        element), signed, zero-padded, whitespace, a digit-group underscore
        (`1_0` is ten) — fails the wire schema, is the
        `ForbiddenArgument(non_canonical_index)` at the runner's PERMIT
        (outranking the schema like the population rule: no correction, no
        worker spawned) and on a direct dispatch, and resolves nowhere for
        `read_field`'s resolver, `apply_patch_text` and the withheld guard."""
        aliases = (
            "populations.-1.conditions.1", "populations.+1.conditions.1", "populations.01.conditions.1",
            "populations. 1.conditions.1", "populations.1 .conditions.1", "populations.1_0.conditions",
            "tables.-1.columns", "tables.00.columns", "marts.0.columns.-1", "tables.０.columns",
        )
        canonical = ("populations.1.conditions.1", "tables.0.columns", "marts.0.columns.0", "tables.10.columns")
        read_schema = V.proposer_tool("read_field").input_schema
        edit_schema = V.proposer_tool("apply_edit_trial").input_schema
        self.assertEqual(read_schema["properties"]["field"]["pattern"], V._FIELD_PATH_RE)
        self.assertEqual(edit_schema["properties"]["locator"]["pattern"], V._LOCATOR_RE)
        self.assertEqual(V._LOCATOR_RE, V._FIELD_PATH_RE)
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertIsNotNone(S.validate_args(read_schema, {"field": alias}))
                self.assertIsNotNone(S.validate_args(edit_schema, dict(population_edit_args(), locator=alias)))
                self.assertIsNone(re.fullmatch(V._FIELD_PATH_RE, alias))
                self.assertEqual(S.locator_argument_problem(alias), S.NON_CANONICAL_INDEX_CODE)
                self.assertEqual(S._forbidden_argument({"field": alias}), S.NON_CANONICAL_INDEX_CODE)
                self.assertEqual(S._forbidden_argument({"locator": alias}), S.NON_CANONICAL_INDEX_CODE)
        for path in canonical:
            with self.subTest(path=path):
                self.assertIsNone(S.validate_args(read_schema, {"field": path}))
                self.assertIsNotNone(re.fullmatch(V._FIELD_PATH_RE, path))
                self.assertEqual(S.locator_argument_problem(path), "")
                self.assertEqual(S._forbidden_argument({"field": path}), "")
        self.assertEqual(S.canonical_list_index("10"), 10)
        self.assertEqual(S.canonical_list_index("0"), 0)
        for segment in ("010", "1_0", "+10", " 10", "10 ", "-0", "٣", "a", ""):
            self.assertIsNone(S.canonical_list_index(segment), segment)
        # The rule outranks the schema at PERMIT (with the population rule),
        # and is scoped to LOCATOR arguments: a load-plan path is not one.
        self.assertEqual(S.SCHEMA_OUTRANKING_ARGUMENT_RULES, frozenset({"population_argument", "non_canonical_index"}))
        self.assertEqual(S._forbidden_argument({"path": "parts/01.parquet"}), "")
        self.assertEqual(S._forbidden_argument({"relative_path": "data/-1.csv"}), "")
        # Direct dispatch: a ForbiddenArgument (a security event), the anchor
        # never applied, the trial bytes and the epoch unchanged.
        session = self.open_session(RepairRoute.POPULATION, "generate")
        before = session.trial_text()
        with mock.patch.object(rp, "apply_patch_text", side_effect=AssertionError("anchor applied")):
            for alias in aliases:
                with self.subTest(alias=alias):
                    with self.assertRaises(S.ForbiddenArgument) as read:
                        self.dispatch(session, "read_field", field=alias)
                    self.assertEqual(
                        (read.exception.code, read.exception.detail), ("forbidden_argument", S.NON_CANONICAL_INDEX_CODE)
                    )
                    self.assertTrue(read.exception.security_event)
                    self.assertIs(S.terminal_for_exception(read.exception), S.TerminalState.POLICY_VIOLATION)
                    with self.assertRaises(S.ForbiddenArgument) as edit:
                        self._edit(session, alias, "Cancelled orders")
                    self.assertEqual(edit.exception.detail, S.NON_CANONICAL_INDEX_CODE)
        self.assertEqual(session.trial_text(), before)
        self.assertEqual((session.state_epoch, session.edits), (0, []))
        # The resolvers agree with the guard: nothing resolves under an alias.
        doc = json.loads(session.trial_text())
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertIs(V._resolve(doc, alias), V._MISSING)
                with self.assertRaises(rp.PatchApplicationError) as ctx:
                    rp._json_get(doc, alias)
                self.assertEqual(ctx.exception.code, "patch_anchor_not_found")
                self.assertIsNone(rp.resolved_condition_slot(alias))
                self.assertFalse(rp.condition_path_is_withheld(alias, frozenset({(1, 1), (4, 1), (10, 0)})))
        self.assertEqual(V._resolve(doc, "populations.1.conditions.1"), doc["populations"][1]["conditions"][1])
        self.assertEqual(rp._json_get(doc, "populations.1.conditions.1"), doc["populations"][1]["conditions"][1])
        self.assertEqual(rp.resolved_condition_slot("populations.1.conditions.1"), (1, 1))
        self.assertEqual(rp.resolved_condition_slot("populations.1.conditions"), (1, None))
        self.assertTrue(rp.condition_path_is_withheld("populations.1.conditions.1", frozenset({(1, 1)})))
        self.assertTrue(rp.condition_path_is_withheld("populations.1.conditions", frozenset({(1, 1)})))
        # Through the real runner: PERMIT answers the violation before any
        # worker (the tool never ran, no edit, no security-free correction).
        for name, args in (
            ("read_field", {"field": "populations.-1.conditions.1"}),
            ("read_field", {"field": "populations.01.conditions.1"}),
            ("apply_edit_trial", dict(population_edit_args(), locator="populations.-1.conditions.1")),
            ("apply_edit_trial", dict(population_edit_args(), locator="populations.01.conditions.1")),
        ):
            with self.subTest(tool=name, args=args):
                probe = self.open_session(RepairRoute.POPULATION, "generate")
                limits = S.SessionLimits.declare(max_turns=4, max_tool_calls=4)
                with mock.patch.object(V.ReadFieldTool, "run", side_effect=AssertionError("worker ran")), \
                        mock.patch.object(V.ApplyEditTrialTool, "run", side_effect=AssertionError("worker ran")):
                    with self.assertRaises(S.SessionPolicyViolation) as violation:
                        S.run_bounded_session(
                            ROLE, probe.view, V.PROPOSER_TOOLS, V.proposer_policy(limits), limits,
                            provider=BS.ScriptedProvider([[BS.tool_use(name, args, "p0")]]), ctx=probe.context(),
                            worker=V.proposer_validator_worker(),
                        )
                fault = violation.exception.fault
                self.assertEqual((fault.tool, fault.detail), (name, S.NON_CANONICAL_INDEX_CODE))
                partial = violation.exception.session_result
                self.assertIs(partial.terminal, S.TerminalState.POLICY_VIOLATION)
                self.assertEqual(partial.correction_count, 0)
                self.assertEqual([e["code"] for e in partial.security_events], ["forbidden_argument"])
                self.assertEqual((probe.tool_calls, probe.edits, probe.state_epoch), (0, [], 0))

    def test_reference_session_edits_only_sql_by_mart(self):
        """On the REFERENCE route a session may edit `reference.sql_by_mart.*`
        ONLY: `provenance`, `implementation_id`, `dialect`, `load_notes` and
        `version` are read by no verifier (`rp.VERIFIER_INERT_IR_PATHS`),
        so an edit there would rotate the content hash with the SQL
        untouched. They are unreadable and unwritable (one code for a hit
        and a miss). Finding 3-0 (docs/plans/bounded_agents_phase1.md §2)
        narrowed the ONE allowlist `ROUTE_IR_PATHS[REFERENCE]` itself —
        rendered into the one-shot proposer's system prompt and enforced by
        `validate_scope` for both proposer modes — so the session's editable
        set IS the route allowlist on every route (a sanctioned security
        change: the one-shot prompt bytes and its transcript keys moved)."""
        self.assertEqual(V.editable_field_paths(RepairRoute.REFERENCE), ("reference.sql_by_mart.*",))
        self.assertEqual(rp.ROUTE_IR_PATHS[RepairRoute.REFERENCE], ("reference.sql_by_mart.*",))
        for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE, RepairRoute.POPULATION):
            self.assertEqual(V.editable_field_paths(route), rp.ROUTE_IR_PATHS[route])
        self.assertEqual(
            rp.VERIFIER_INERT_IR_PATHS,
            ("reference.dialect", "reference.implementation_id", "reference.load_notes",
             "reference.provenance", "reference.version"),
        )
        for path in rp.VERIFIER_INERT_IR_PATHS:
            for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE, RepairRoute.POPULATION):
                self.assertFalse(rp._ir_path_allowed(path, route), (path, route.value))
        # The one-shot prompt renders exactly the narrowed allowlist.
        self.assertIn("reference.sql_by_mart.*", P.repair_route_scope_text())
        self.assertNotIn("reference.*", P.repair_route_scope_text())
        session = self.open_session(RepairRoute.REFERENCE, "reference")
        doc = json.loads(session.trial_text())
        inert = {
            path: value for path, value in _string_leaves(doc["reference"], "reference")
            if not path.startswith("reference.sql_by_mart.")
        }
        self.assertEqual(
            set(inert),
            {"reference.provenance", "reference.implementation_id", "reference.dialect",
             "reference.load_notes", "reference.version"},
        )
        before = session.trial_text()
        for path, value in inert.items():
            with self.subTest(field=path):
                self.assertEqual(self.dispatch(session, "read_field", field=path).code, "field_outside_allowlist")
                hit = self._edit(session, path, value[: max(1, len(value) // 2)])
                miss = self._edit(session, path, "zzz-never-in-any-field-zzz")
                self.assertEqual((hit.code, miss.code), ("scope_field_outside_allowlist",) * 2)
        self.assertEqual(session.trial_text(), before)
        self.assertEqual(session.state_epoch, 0)
        applied = self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        self.assertEqual(applied.code, "applied")
        self.assertEqual(self.dispatch(session, "check_scope").code, "scope_ok")
        self.assertTrue(
            all(e.locator.startswith("reference.sql_by_mart.") for e in session.accumulated_patch().edits)
        )


class CertifyCancellationTest(ProposerToolsCase):
    """Finding 3-1: the certify deadline cancels the worker (DuckDB interrupt
    through the worker's interruptible scope), the torn-down copy is never
    resurrected, and the spawned call is charged."""

    def test_certify_deadline_cancels_the_worker_and_never_resurrects_the_copy(self):
        """A stage that outlives the deadline AND the cancel grace keeps
        running only until it returns: the copy is handed to a reaper and
        removed once the worker ends (never underneath it, never left
        behind), `certify_calls` and the bits were charged at spawn, and
        the held trial saw nothing of it. Re-pinned by the Phase 3 re-check
        verdict: the stalled stage is the one the REFERENCE edit feeds, so
        the deadline is the model's own exhaustion — the PAID
        `certify_refused_resource_budget` receipt (its `worker_handle` the
        cancelled thread) instead of a `ToolDeadlineExceeded` halt; the
        cancel, the reaper and the copy discipline are byte-identical."""
        resurrected: list[Path] = []
        armed = _ArmedClock()

        def slow(engine, task):
            armed.expire()  # the deadline passes only once the stage is RUNNING
            time.sleep(0.4)  # past the deadline and the (patched) grace
            marker = Path(engine.workspace) / "resurrected.txt"
            marker.write_text("late", encoding="utf-8")
            resurrected.append(marker)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="late"))

        runners = {"generate": pass_runner(), "reference": slow}
        session = self.open_session(
            RepairRoute.REFERENCE, "reference", certify_runners=runners,
            certify_deadline_s=300.0, clock=armed,
        )
        self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
        with mock.patch.object(C, "CERTIFY_CANCEL_GRACE_S", 0.05):
            diag = self.dispatch(session, "certify")
        self.assertEqual((diag.code, diag.ok), (C.CODE_REFUSED_RESOURCE_BUDGET, False))
        self.assertEqual((session.certify_calls, session.oracle_bits_used, session.certified_epoch), (1, 2, 1))
        (receipt,) = session.certify_receipts
        self.assertEqual((receipt.resource_stage, receipt.worker), ("reference", C.CERTIFY_WORKER_THREAD))
        worker, copy_path = receipt.worker_handle, receipt.copy_path
        # The grace expired with the worker alive: the copy is still there
        # (never torn down under a running stage) ...
        self.assertTrue(worker.is_alive())
        self.assertTrue(copy_path.exists())
        worker.join(10.0)
        self.assertFalse(worker.is_alive())
        # ... and gone once the worker ended, together with what it wrote.
        deadline = time.monotonic() + 10.0
        while copy_path.parent.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(copy_path.exists())
        self.assertFalse(copy_path.parent.exists())
        self.assertEqual(len(resurrected), 1)
        self.assertEqual(resurrected[0].parent.resolve(), copy_path.resolve())
        self.assertFalse((session.trial / "resurrected.txt").exists())
        self.assertFalse((self.workspace / "resurrected.txt").exists())

    def test_certify_deadline_interrupts_a_running_gold_query(self):
        """The reference runner's gold connection is opened inside the
        worker's interruptible scope (under `CERTIFY_MEMORY_LIMIT_MB`), so
        the supervisor's deadline interrupts a query the stage is inside and
        the worker unwinds within the grace instead of burning CPU until the
        interpreter exits. Outside a scope — every live run — the reference
        connection is untouched: nothing registered, no memory limit set."""
        import duckdb

        from elt_taskgen.reference import duckdb_sandbox
        from elt_taskgen.reference import runner as reference_runner

        seen: dict = {}

        def long_query(engine, task):
            con = reference_runner.open_reference_connection()
            (seen["memory_limit"],) = con.execute("SELECT current_setting('memory_limit')").fetchone()
            seen["threads"] = con.execute("SELECT current_setting('threads')").fetchone()[0]
            try:
                con.execute("SELECT COUNT(*) FROM range(300000) a, range(300000) b").fetchone()
            except duckdb.Error as exc:
                seen["error"] = type(exc).__name__
                raise
            finally:
                con.close()
            return StageOutcome(VERDICT_PASS, StagePayload(detail="never"))

        with self.assertRaises(S.ToolDeadlineExceeded) as ctx:
            C.certify_disposable_copy(
                self.workspace, self.task_id, ("reference",), runners={"reference": long_query},
                deadline_s=C.CERTIFY_DEADLINE_S, clock=_JumpingClock(),
            )
        started = time.monotonic()
        ctx.exception.worker.join(10.0)
        self.assertFalse(ctx.exception.worker.is_alive())
        self.assertLess(time.monotonic() - started, C.CERTIFY_CANCEL_GRACE_S)
        self.assertEqual(seen["error"], "InterruptException")
        self.assertEqual(seen["memory_limit"], f"{float(C.CERTIFY_MEMORY_LIMIT_MB):.1f} MiB")  # set in MiB
        self.assertEqual(str(seen["threads"]), "1")
        self.assertFalse(ctx.exception.copy_path.parent.exists())
        # The live path: no scope, no registration, no limit.
        con = reference_runner.open_reference_connection()
        try:
            self.assertFalse(duckdb_sandbox.register_interruptible(con))
            self.assertIsNone(duckdb_sandbox.current_scope_memory_limit_mb())
            (live_limit,) = con.execute("SELECT current_setting('memory_limit')").fetchone()
            self.assertNotEqual(live_limit, seen["memory_limit"])
        finally:
            con.close()
        self.assertEqual(duckdb_sandbox.interrupt_scope(-1), 0)



# Certify's spawned process group is killed before workspace cleanup;
# module-level helpers remain picklable across the spawn boundary.

_GRANDCHILD_PID_FILE_ENV = "ELT_TASKGEN_TEST_CERTIFY_GRANDCHILD_PID_FILE"


def green_generate(engine, task):
    return StageOutcome(VERDICT_PASS, StagePayload(detail="generate ok"))


def slow_reference_writing_marker(engine, task):
    """Starts a grandchild (its pid reported through the env-named file),
    sleeps past every deadline the tests arm, then writes a marker into its
    workspace — the copy the supervisor must have removed by then."""
    pid_file = os.environ.get(_GRANDCHILD_PID_FILE_ENV)
    if pid_file:
        child = subprocess.Popen(["/bin/sleep", "60"])
        Path(pid_file).write_text(str(child.pid), encoding="utf-8")
    time.sleep(4.0)
    Path(engine.workspace, "resurrected.txt").write_text("late", encoding="utf-8")
    return StageOutcome(VERDICT_PASS, StagePayload(detail="late"))


def crashing_reference(engine, task):
    raise MemoryError("gold at /Users/x/answer_key exceeded 4711 rows")


def parser_error_reference(engine, task):
    """A model-attributable runner exception (what DuckDB raises for
    reference SQL the model broke), picklable for the process worker."""
    import duckdb

    raise duckdb.ParserException('Parser Error: syntax error at or near "SELEKT" in /Users/x/answer_key 4711')


def oom_reference(engine, task):
    """DuckDB out of memory under the worker's `CERTIFY_MEMORY_LIMIT_MB` on
    the stage a REFERENCE edit feeds — the model's own resource-budget
    exhaustion (Phase 3 re-check verdict), picklable for the process worker."""
    import duckdb

    raise duckdb.OutOfMemoryException(
        "Out of Memory Error: failed to allocate 512MB at /Users/x/answer_key (4711 rows)"
    )


def oom_generate(engine, task):
    """The same exhaustion in `generate` (which a REFERENCE edit does not feed)."""
    import duckdb

    raise duckdb.OutOfMemoryException(
        "Out of Memory Error: failed to allocate 512MB at /Users/x/answer_key (4711 rows)"
    )


def interrupted_reference(engine, task):
    """The query the supervisor interrupted (`con.interrupt()` at the deadline)."""
    import duckdb

    raise duckdb.InterruptException("INTERRUPT Error: query interrupted at /Users/x/answer_key 4711")


def blocked_reference(engine, task):
    return StageOutcome(
        engine_mod.VERDICT_BLOCKED,
        StagePayload(
            detail="waiting: the warehouse at /Users/x/runs/live is down",
            data={engine_mod.BLOCKED_ON_KEY: engine_mod.BLOCKED_ON_ENVIRONMENT},
        ),
    )


def dying_reference(engine, task):
    os._exit(3)


class CertifyStageWindowTest(ProposerToolsCase):
    """finding p4-2-0: `on_stage(name)` was a LATCH, not a window."""

    def test_a_deadline_outside_a_stage_runner_is_a_harness_fault(self):
        """The paid `certify_refused_resource_budget` was charged to the model
        for a harness stall that happened OUTSIDE any stage runner.

        `run_provider_free_stages` announced the stage immediately BEFORE the
        runner and never cleared it, so every piece of harness bookkeeping
        between and after runners — `engine.save_task`, `engine.record_report`,
        `_record_memo_served_review`, the final green projection,
        `engine.close()`, the worker's IPC send — sat inside a window
        attributed to a completed, TOUCHED stage. A certify whose stages had
        all returned GREEN was reported and billed as the model's own resource
        exhaustion: 2 oracle bits charged at spawn stand, one of `max_certify`
        is spent, the session is steered as if its edit blew the envelope, and
        the passing result is discarded.
        """
        import elt_taskgen.engine as engine_mod

        armed = _ArmedClock()
        stalls: list[str] = []

        def stall(original):
            def wrapper(self, *args, **kwargs):
                stalls.append("stalled")
                armed.expire()
                time.sleep(0.5)
                return original(self, *args, **kwargs)

            return wrapper

        # (a) A deadline during POST-STAGE bookkeeping (`record_report`).
        with mock.patch.object(
            engine_mod.Engine, "record_report", stall(engine_mod.Engine.record_report)
        ):
            with self.assertRaises(S.ToolDeadlineExceeded) as raised:
                C.certify_disposable_copy(
                    self.workspace, self.task_id, ("reference",),
                    runners={"reference": pass_runner()},
                    deadline_s=1.0, clock=armed,
                    touched_stages=("reference", "attack"),
                )
        self.assertEqual(stalls, ["stalled"])
        raised.exception.worker.join(10.0)

        # (b) A deadline during TEARDOWN, after every stage returned GREEN.
        armed = _ArmedClock()
        stalls.clear()
        with mock.patch.object(
            engine_mod.Engine, "close", stall(engine_mod.Engine.close)
        ):
            with self.assertRaises(S.ToolDeadlineExceeded) as raised:
                C.certify_disposable_copy(
                    self.workspace, self.task_id, ("reference",),
                    runners={"reference": pass_runner()},
                    deadline_s=1.0, clock=armed,
                    touched_stages=("reference", "attack"),
                )
        self.assertEqual(stalls, ["stalled"])
        raised.exception.worker.join(10.0)

        # The window closes with the announcement `NO_STAGE`, and the
        # deadline receipt answers None — the harness fault — for it.
        self.assertEqual(C.NO_STAGE, "")
        self.assertIsNone(
            C._deadline_receipt(
                stage=C.NO_STAGE, touched_stages=("reference",), attack=None,
                model_stages_deferred=False, stages=("reference",),
                copy_path=self.workspace, elapsed_s=0.0,
                worker=C.CERTIFY_WORKER_THREAD, worker_handle=None,
            )
        )
        # ...while a deadline INSIDE a touched stage's runner is still the
        # PAID receipt it always was.
        self.assertIsNotNone(
            C._deadline_receipt(
                stage="reference", touched_stages=("reference",), attack=None,
                model_stages_deferred=False, stages=("reference",),
                copy_path=self.workspace, elapsed_s=0.0,
                worker=C.CERTIFY_WORKER_THREAD, worker_handle=None,
            )
        )


class _ArmedClock:
    """Real time until `expire()` is called (from any thread), then real
    time plus a day: the supervisor's next reading passes any deadline."""

    def __init__(self):
        self._expired = threading.Event()

    def expire(self):
        self._expired.set()

    def __call__(self):
        return time.monotonic() + (86_400.0 if self._expired.is_set() else 0.0)


class CertifyProcessWorkerTest(ProposerToolsCase):
    """docs/plans/bounded_agents_phase1.md §2, finding 3-1: the production
    certify worker is a spawned process on the semantic scorer's pattern;
    the deadline kills its whole process group and only then removes the
    copy; the spawned call is charged; only typed faults cross back."""

    def assert_process_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return
            except PermissionError:  # pragma: no cover - another user's pid
                return
            time.sleep(0.02)
        self.fail(f"process {pid} is still alive")

    def test_certify_deadline_kills_the_worker_process_group_before_teardown(self):
        """Two deadlines: one that fires while the child is still
        bootstrapping (the pre-`setsid` window: the process alone is
        killed) and one that fires while the stage is RUNNING with a
        grandchild of its own (the group is killed). Either way the worker
        is dead (SIGKILL) before the copy is removed, the copy is gone when
        the fault surfaces, the marker the stage would have written later
        never lands anywhere, `certify_calls` and the bits were charged at
        spawn, and the held trial saw nothing of it. Re-pinned by the Phase
        3 re-check verdict: the bootstrap deadline fires before any stage
        began and stays the `ToolDeadlineExceeded` harness fault; the
        running deadline cuts short the stage the REFERENCE edit feeds and
        is the PAID `certify_refused_resource_budget` receipt (its
        `worker_handle` the killed process) — the kill-first, tear-down-
        second discipline is byte-identical either way."""
        runners = {"generate": green_generate, "reference": slow_reference_writing_marker}
        for case in ("bootstrap", "running"):
            with self.subTest(case=case):
                pid_file = Path(self._tmp.name) / f"grandchild-{case}.pid"
                clock = _JumpingClock() if case == "bootstrap" else _ArmedClock()
                session = self.open_session(
                    RepairRoute.REFERENCE, "reference", certify_runners=runners,
                    certify_worker=C.CERTIFY_WORKER_PROCESS, certify_deadline_s=300.0, clock=clock,
                )
                self.assertEqual(session.certify_worker, C.CERTIFY_WORKER_PROCESS)
                self.dispatch(session, "apply_edit_trial", **reference_edit_args(self.task))
                if case == "running":
                    def arm_once_the_stage_runs():
                        until = time.monotonic() + 60.0
                        while not pid_file.exists() and time.monotonic() < until:
                            time.sleep(0.02)
                        time.sleep(0.1)
                        clock.expire()

                    threading.Thread(target=arm_once_the_stage_runs, daemon=True).start()
                started = time.monotonic()
                with mock.patch.dict(os.environ, {_GRANDCHILD_PID_FILE_ENV: str(pid_file)}):
                    if case == "running":
                        diag = self.dispatch(session, "certify")
                        self.assertEqual((diag.code, diag.ok), (C.CODE_REFUSED_RESOURCE_BUDGET, False))
                        (receipt,) = session.certify_receipts
                        self.assertEqual((receipt.resource_stage, receipt.worker), ("reference", C.CERTIFY_WORKER_PROCESS))
                        worker, copy_path = receipt.worker_handle, receipt.copy_path
                    else:
                        with self.assertRaises(S.ToolDeadlineExceeded) as ctx:
                            self.dispatch(session, "certify")
                        exc = ctx.exception
                        self.assertEqual((exc.tool, exc.deadline_s), ("certify", 300.0))
                        self.assertEqual(session.certify_receipts, [])
                        worker, copy_path = exc.worker, exc.copy_path
                # Charged at spawn: a deadline is a paid call.
                self.assertEqual(
                    (session.certify_calls, session.oracle_bits_used, session.certified_epoch), (1, 2, 1)
                )
                self.assertIsInstance(worker, multiprocessing.process.BaseProcess)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, -signal.SIGKILL)
                self.assertFalse(copy_path.exists())
                self.assertFalse(copy_path.parent.exists())
                self.assert_process_gone(worker.pid)
                if case == "running":
                    self.assertLess(time.monotonic() - started, 30.0)
                    self.assert_process_gone(int(pid_file.read_text(encoding="utf-8")))  # the group went too
                else:
                    self.assertFalse(pid_file.exists())  # killed before the stage began
                # Past the stage's own sleep: nothing resurrected anywhere.
                time.sleep(max(0.0, 4.5 - (time.monotonic() - started)))
                self.assertFalse(copy_path.parent.exists())
                self.assertFalse((session.trial / "resurrected.txt").exists())
                self.assertFalse((self.workspace / "resurrected.txt").exists())
                self.assertEqual(session.trial_text(), rp.apply_patch_text(session.before_ir, session.accumulated_patch()))

    def test_certify_process_worker_transports_typed_faults_only(self):
        """Across the spawn boundary travel the copy path, the task id, the
        stage names and (for a test) picklable module-level runners; back
        come a `Diagnostic` dump or a typed fault descriptor. A runner that
        raises is a `ToolHarnessFault` naming the CLASS only, a member that
        WAITED is `stage_could_not_measure` with its `blocked_on`, a worker
        that dies without a word is `SandboxFault(worker_failed)`, a
        closure is refused before any copy is opened, and the RSS envelope
        is the semantic scorer's."""
        from elt_taskgen.semantic.models import SemanticLimits

        self.materialize()
        for runner, code, cause in (
            (crashing_reference, "harness_exception", "MemoryError"),
            (blocked_reference, "stage_could_not_measure", "blocked_on:environment"),
        ):
            with self.subTest(runner=runner.__name__):
                with self.assertRaises(S.ToolHarnessFault) as ctx:
                    C.certify_disposable_copy(
                        self.workspace, self.task_id, ("generate", "reference"),
                        runners={"generate": green_generate, "reference": runner},
                        worker=C.CERTIFY_WORKER_PROCESS,
                    )
                exc = ctx.exception
                self.assertEqual((exc.tool, exc.code, exc.cause_type), ("certify", code, cause))
                self.assertEqual(engine_mod._infra_marker_for(exc), "ToolHarnessFault")
                for secret in ("4711", "answer_key", "/Users", "runs/live"):
                    self.assertNotIn(secret, str(exc))
                if runner is blocked_reference:
                    self.assertEqual(exc.blocked_on, engine_mod.BLOCKED_ON_ENVIRONMENT)
                # The worker ended and its copy went before the fault surfaced.
                self.assertFalse(exc.worker.is_alive())
                self.assertFalse(exc.copy_path.parent.exists())
        with self.assertRaises(S.SandboxFault) as died:
            C.certify_disposable_copy(
                self.workspace, self.task_id, ("reference",),
                runners={"reference": dying_reference}, worker=C.CERTIFY_WORKER_PROCESS,
            )
        self.assertEqual(died.exception.code, "worker_failed")
        self.assertFalse(died.exception.worker.is_alive())
        self.assertFalse(died.exception.copy_path.parent.exists())
        # A closure cannot cross: refused as a harness fault before any copy is opened.
        with mock.patch.object(rp, "trial_workspace", side_effect=AssertionError("a copy was opened")):
            with self.assertRaises(S.ToolHarnessFault) as closure:
                C.certify_disposable_copy(
                    self.workspace, self.task_id, ("reference",),
                    runners={"reference": lambda engine, task: None}, worker=C.CERTIFY_WORKER_PROCESS,
                )
        self.assertEqual(closure.exception.code, "runner_not_transportable")
        # The green path through the real runners, as a process.
        receipt = C.certify_disposable_copy(self.workspace, self.task_id, ("reference",))
        self.assertEqual((receipt.diagnostic.code, receipt.worker), (C.CODE_GREEN, C.CERTIFY_WORKER_PROCESS))
        self.assertFalse(receipt.copy_path.exists())
        self.assertFalse(receipt.copy_path.parent.exists())
        # Worker selection: the production dict spawns, an injected dict
        # stays in-process, anything else is refused.
        self.assertEqual(C.resolve_worker_kind(None, None), C.CERTIFY_WORKER_PROCESS)
        self.assertEqual(C.resolve_worker_kind(None, {"reference": green_generate}), C.CERTIFY_WORKER_THREAD)
        self.assertEqual(C.resolve_worker_kind("thread", None), C.CERTIFY_WORKER_THREAD)
        with self.assertRaises(ValueError):
            C.resolve_worker_kind("fork", None)
        with self.assertRaises(ValueError):
            C.certify_disposable_copy(self.workspace, self.task_id, ("reference",), rss_limit_mb=64)
        self.assertEqual(C.CERTIFY_WORKER_RSS_LIMIT_MB, SemanticLimits().worker_rss_limit_mb)
        self.assertEqual(V.proposer_tool("certify").cost.wall_s, C.CERTIFY_DEADLINE_S)

    def test_certify_worker_rss_watchdog_and_death_are_sandbox_faults(self):
        """The supervisor's verdicts, driven by a fake process: an RSS
        breach is `memory`, an exit without a message is `died`, a result
        or fault message is returned as is, junk on the pipe is `died`, and
        the injectable clock decides `deadline`. End to end, a tripped
        watchdog kills the real worker FIRST (SIGKILL) and removes the copy
        SECOND, surfacing as `SandboxFault(memory_limit)`."""
        from elt_taskgen.semantic import scoring

        class FakeProcess:
            def __init__(self, alive=True):
                self.pid = os.getpid()
                self._alive = alive

            def is_alive(self):
                return self._alive

        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        self.addCleanup(receive.close)
        self.addCleanup(send.close)
        common = dict(deadline_s=300.0, clock=time.monotonic, started=time.monotonic())
        with mock.patch.object(scoring, "_process_rss_bytes", return_value=2 ** 40):
            self.assertEqual(
                C._supervise_certify_worker(FakeProcess(), receive, rss_limit_bytes=2 ** 30, **common),
                ("memory", None),
            )
        with mock.patch.object(scoring, "_process_rss_bytes", return_value=1):
            self.assertEqual(
                C._supervise_certify_worker(FakeProcess(alive=False), receive, rss_limit_bytes=2 ** 30, **common),
                ("died", None),
            )
            send.send(("result", {"x": True}))
            self.assertEqual(
                C._supervise_certify_worker(FakeProcess(), receive, rss_limit_bytes=2 ** 30, **common),
                ("result", {"x": True}),
            )
            send.send("junk")
            self.assertEqual(
                C._supervise_certify_worker(FakeProcess(), receive, rss_limit_bytes=2 ** 30, **common),
                ("died", None),
            )
            self.assertEqual(
                C._supervise_certify_worker(
                    FakeProcess(), receive, deadline_s=300.0, clock=_JumpingClock(), started=1000.0,
                    rss_limit_bytes=2 ** 40,
                ),
                ("deadline", None),
            )
        # End to end with a real worker: watchdog verdict -> kill -> teardown.
        with mock.patch.object(C, "_supervise_certify_worker", return_value=("memory", None)):
            with self.assertRaises(S.SandboxFault) as ctx:
                C.certify_disposable_copy(
                    self.workspace, self.task_id, ("reference",),
                    runners={"reference": slow_reference_writing_marker}, worker=C.CERTIFY_WORKER_PROCESS,
                )
        exc = ctx.exception
        self.assertEqual(exc.code, "memory_limit")
        self.assertIs(S.terminal_for_exception(exc), S.TerminalState.HARNESS_FAULT)
        self.assertFalse(exc.worker.is_alive())
        self.assertEqual(exc.worker.exitcode, -signal.SIGKILL)
        self.assertFalse(exc.copy_path.parent.exists())
        self.assert_process_gone(exc.worker.pid)


class WithheldAnchorTest(ProposerToolsCase):
    """Anchor permissions agree exactly with what each route view exposes.

    The specification route now shows its task-public author context, while
    the population route continues to guard withheld private conditions.
    """

    def _edit(self, session, op, locator, old, new="x"):
        return self.dispatch(
            session, "apply_edit_trial", artifact="task_ir.json", op=op,
            locator=locator, old=old, new="" if op == "delete" else new, rationale="a probe",
        )

    def test_specification_author_context_makes_editable_text_visible(self):
        session = self.open_session(RepairRoute.SPECIFICATION, "review")
        doc = json.loads(session.trial_text())
        visible = [
            (path, value)
            for path, value in _string_leaves(doc)
            if V.field_is_editable(path, RepairRoute.SPECIFICATION)
            and len(value) >= 4
        ]
        self.assertGreaterEqual(len(visible), 4)
        self.assertTrue(
            all(V.anchor_is_visible(path, RepairRoute.SPECIFICATION) for path, _ in visible)
        )
        self.assertTrue(V.anchor_is_visible(f"reference.sql_by_mart.{MART}", RepairRoute.REFERENCE))
        self.assertTrue(V.anchor_is_visible("populations.1.conditions.1", RepairRoute.POPULATION))
        self.assertFalse(V.anchor_is_visible("solver_prompt", RepairRoute.REFERENCE))
        for path, _value in visible:
            with self.subTest(field=path):
                self.assertEqual(
                    self.dispatch(session, "read_field", field=path).code,
                    "field_text_in_view",
                )

        # A description shown in the author context supports an ordinary
        # exact-anchor replacement; an absent anchor still returns one code.
        path = "tables.0.description"
        old = doc["tables"][0]["description"]
        replaced = self._edit(
            session, "replace", path, old, new=old + " Public clarification."
        )
        self.assertEqual((replaced.code, session.state_epoch), ("applied", 1))
        self.assertEqual(
            self._edit(session, "replace", path, "zzz-never-in-the-description").code,
            "patch_anchor_not_found",
        )
        self.assertEqual(self._edit(session, "insert", "solver_prompt", "", new=" " + SENTINEL).code, "applied")
        self.assertEqual(
            self._edit(session, "replace", "solver_prompt", "customer_id ascending", new="customer_id descending").code,
            "applied",
        )
        self.assertEqual(self._edit(session, "replace", "solver_prompt", "zzz-never-in-the-prose").code, "patch_anchor_not_found")
        self.assertEqual(self._edit(session, "delete", "solver_prompt", "zzz-never-in-the-prose").code, "patch_anchor_not_found")
        self.assertEqual(session.state_epoch, 3)

    def test_withheld_condition_guard_decides_on_the_resolved_pair(self):
        """Phase 3 re-check verdict: `condition_is_withheld` decides on the
        RESOLVED `(population index, slot)` pair — the pair `read_field`'s
        resolver and `apply_patch_text` address — never on a `\\d+` reading
        of the path, so the guard and the resolvers can never disagree.
        Before, `populations.-2.conditions.<j>` matched the guard's regex
        nowhere (not withheld) while `int("-2")` resolved it to the same
        population: a replace anchor over the demo's withheld
        counterfactual line answered `applied` for its true text and
        `patch_anchor_not_found` for an absent one — a substring oracle
        over withheld material, open to the one-shot proposer's patch too.
        Now no alias resolves anywhere and the tools refuse it before a
        byte is read."""
        from elt_taskgen.models import RepairEdit, RepairEditOp

        session = self.open_session(RepairRoute.POPULATION, "generate")
        withheld = sorted(session.withheld_conditions)
        self.assertEqual(withheld, sorted(rp.withheld_population_conditions(self.task)))
        self.assertTrue(withheld)
        doc = json.loads(session.trial_text())
        populations = doc["populations"]
        index, slot = withheld[0]
        text = populations[index]["conditions"][slot]
        canonical = f"populations.{index}.conditions.{slot}"
        self.assertTrue(session.condition_is_withheld(canonical))
        self.assertTrue(session.condition_is_withheld(f"populations.{index}.conditions"))
        self.assertEqual(rp.resolved_condition_slot(canonical), (index, slot))
        self.assertEqual(self.dispatch(session, "read_field", field=canonical).code, "field_withheld")
        aliases = {
            "negative index": f"populations.{index - len(populations)}.conditions.{slot}",
            "zero-padded index": f"populations.0{index}.conditions.{slot}",
            "signed index": f"populations.+{index}.conditions.{slot}",
            "whitespace index": f"populations. {index}.conditions.{slot}",
            "zero-padded slot": f"populations.{index}.conditions.0{slot}",
            "negative slot": f"populations.{index}.conditions.{slot - len(populations[index]['conditions'])}",
            "whole list, zero-padded": f"populations.0{index}.conditions",
        }
        before = session.trial_text()
        for label, alias in aliases.items():
            with self.subTest(alias=label):
                parts = alias.split(".")
                # What `int()` resolved before: the withheld text itself.
                reached = populations[int(parts[1])]["conditions"]
                if len(parts) == 4:
                    self.assertEqual(reached[int(parts[3])], text)
                else:
                    self.assertIn(text, reached)
                # The guard, the resolvers and the patch applier now agree:
                # the alias names nothing.
                self.assertIsNone(rp.resolved_condition_slot(alias))
                self.assertFalse(session.condition_is_withheld(alias))
                self.assertIs(V._resolve(doc, alias), V._MISSING)
                with self.assertRaises(rp.PatchApplicationError) as resolved:
                    rp._json_get(doc, alias)
                self.assertEqual(resolved.exception.code, "patch_anchor_not_found")
                if len(parts) == 4:
                    for old in (text, text[: len(text) // 2], "zzz-never-in-any-condition-zzz"):
                        patch = RepairPatch(
                            route=RepairRoute.POPULATION, artifact="task_ir.json",
                            edits=(RepairEdit(op=RepairEditOp.REPLACE, locator=alias, old=old, new=old + " x"),),
                            rationale="a probe", proposer_role=ROLE,
                        )
                        with self.assertRaises(rp.PatchApplicationError) as applied:
                            rp.apply_patch_text(before, patch)
                        self.assertEqual(applied.exception.code, "patch_anchor_not_found")  # one code, hit or miss
                # The tools refuse the alias before any byte is read.
                with self.assertRaises(S.ForbiddenArgument) as read:
                    self.dispatch(session, "read_field", field=alias)
                self.assertEqual(read.exception.detail, S.NON_CANONICAL_INDEX_CODE)
                with mock.patch.object(rp, "apply_patch_text", side_effect=AssertionError("anchor applied")):
                    for op in ("replace", "delete", "insert"):
                        with self.assertRaises(S.ForbiddenArgument) as edit:
                            self._edit(session, op, alias, text[: len(text) // 2], new="x")
                        self.assertEqual(edit.exception.detail, S.NON_CANONICAL_INDEX_CODE)
        self.assertEqual(session.trial_text(), before)
        self.assertEqual((session.state_epoch, session.edits), (0, []))
        # The canonical path still answers ONE code for a hit and a miss
        # (finding 3-0), and `insert` stays open.
        with mock.patch.object(rp, "apply_patch_text", side_effect=AssertionError("anchor applied")):
            for old in (text, "zzz-never-in-any-condition-zzz"):
                self.assertEqual(self._edit(session, "replace", canonical, old).code, "patch_anchor_not_found")
        # Through the real runner, the alias is a policy violation at PERMIT:
        # no worker, no edit, no security-free correction turn.
        probe = self.open_session(RepairRoute.POPULATION, "generate")
        limits = S.SessionLimits.declare(max_turns=4, max_tool_calls=4)
        edit_args = {
            "artifact": "task_ir.json", "op": "replace", "locator": aliases["negative index"],
            "old": text, "new": text + " x", "rationale": "a probe",
        }
        with self.assertRaises(S.SessionPolicyViolation) as violation:
            S.run_bounded_session(
                ROLE, probe.view, V.PROPOSER_TOOLS, V.proposer_policy(limits), limits,
                provider=BS.ScriptedProvider([[BS.tool_use("apply_edit_trial", edit_args, "p0")]]),
                ctx=probe.context(), worker=V.proposer_validator_worker(),
            )
        self.assertEqual(violation.exception.fault.detail, S.NON_CANONICAL_INDEX_CODE)
        self.assertEqual((probe.tool_calls, probe.edits), (0, []))
        self.assertEqual(probe.trial_text(), before)


class SessionDefaultsTest(unittest.TestCase):
    """The document's top-level `session:` block (today
    `format_error_disposition`) reaches every `SessionLimits` built outside
    `providers.session_policy_for` — the author's, the proposer policy's
    and the bounded proposer's — un-hashed, so the documented location is
    honoured and no transcript key moves."""

    def test_author_and_proposer_policies_carry_the_session_wide_defaults(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)
        self.assertEqual(V.session_defaults_for(), {"format_error_disposition": "halt"})
        baseline = V.proposer_policy().sha256()
        author_baseline = V.author_policy().sha256()
        doc = json.loads(json.dumps(P._agents_doc()))
        doc["session"] = {"format_error_disposition": "stage_fail"}
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            self.assertEqual(V.session_defaults_for(), {"format_error_disposition": "stage_fail"})
            built = {
                "author_limits": V.author_limits(),
                "author_policy": V.author_policy().limits,
                "author_policy(limits)": V.author_policy(S.SessionLimits.declare(max_revisions=1, max_turns=2)).limits,
                "proposer_policy": V.proposer_policy().limits,
                "proposer_policy(limits)": V.proposer_policy(S.SessionLimits.declare(max_turns=3)).limits,
                "AgenticRepairProposer": rp.AgenticRepairProposer(RaisingProvider()).limits,
            }
            for name, limits in built.items():
                with self.subTest(built=name):
                    self.assertEqual(limits.format_error_disposition, "stage_fail")
                    self.assertNotIn("format_error_disposition", limits.as_manifest())
                    self.assertEqual(dict(limits.session_defaults), {"format_error_disposition": "stage_fail"})
            # Un-hashed: the policy identity is the block's alone.
            self.assertEqual(V.proposer_policy().sha256(), baseline)
            self.assertEqual(V.author_policy().sha256(), author_baseline)
            # A role block's own key wins; explicit defaults are kept as given.
            self.assertEqual(V.author_limits({"format_error_disposition": "halt"}).format_error_disposition, "halt")
            explicit = S.SessionLimits.from_block({}, session_defaults={"format_error_disposition": "halt"})
            self.assertEqual(V.proposer_policy(explicit).limits.format_error_disposition, "halt")
            self.assertEqual(V.author_policy(explicit).limits.format_error_disposition, "halt")
            # A document declaring no block: `{}` and the `halt` default.
            del doc["session"]
            P.clear_behavior_caches()
            self.assertEqual(V.session_defaults_for(), {})
            self.assertEqual(V.proposer_policy().limits.format_error_disposition, "halt")
        P.clear_behavior_caches()
        self.assertEqual(V.proposer_policy().limits.format_error_disposition, "halt")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ===========================================================================
# Phase 2 (roadmap 2.a): the DEV/T implementer and EL loader session tools
# (SoT T1 IMP/LDR, T3; ADDENDUM_constraint_status §2/§4 and A23; OQ-23 = C)
# ===========================================================================

_DEV_FIXTURE: dict = {}


def _dev_fixture() -> dict:
    """A generated demo workspace (all five populations rendered), its frozen
    gold, an emitted EL bundle and a materialized DEVELOPMENT warehouse — built
    ONCE (tens of seconds) and shared by the witness test classes."""
    if not _DEV_FIXTURE:
        import tempfile as _tempfile

        from elt_taskgen.export import eltbench as _eltbench
        from elt_taskgen.models import PopulationName as _Pop
        from elt_taskgen.models import TaskVariant as _Variant
        from elt_taskgen.reference import gold as _gold_mod
        from elt_taskgen.reference import runner as _runner_mod
        from elt_taskgen.training import dev_tool as _dev_tool

        task = demo_fixture.demo_task()
        tmp = _tempfile.TemporaryDirectory()
        # This fixture is shared across several test classes, so no individual
        # class owns its teardown.  Give the module explicit ownership instead
        # of relying on TemporaryDirectory's warning-producing finalizer.
        def cleanup_fixture() -> None:
            _DEV_FIXTURE.clear()
            tmp.cleanup()

        unittest.addModuleCleanup(cleanup_fixture)
        ws = (Path(tmp.name) / "witness-workspace").resolve()
        engine = Engine(ws)
        try:
            engine.register(task)
            task = engine.load_task(demo_fixture.DEMO_TASK_ID)
            assert cli_mod.run_generate(engine, task).verdict == VERDICT_PASS
            tdir = engine.task_dir(task.task_id)
            results = {pop: _runner_mod.run_reference(task, pop, ws) for pop in _Pop}
            gold = _gold_mod.freeze_gold(task, results, tdir / "answer_key")
            _eltbench.emit_variant(
                task, gold, _Variant.EXTRACT_LOAD, tdir / "variants" / "extract_load",
                populations_dir=tdir / "populations",
            )
            _dev_tool.materialize_dev_warehouse(task, ws)
        finally:
            engine.close()
        _DEV_FIXTURE.update(task=task, tmp=tmp, ws=ws, gold=gold, tdir=tdir)
    return _DEV_FIXTURE


class ImplementerToolsCase(unittest.TestCase):
    """The shared witness workspace plus a fresh implementer session per test."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture = _dev_fixture()
        cls.task = fixture["task"]
        cls.ws = fixture["ws"]
        cls.gold = fixture["gold"]
        cls.tdir = fixture["tdir"]
        cls.MART = demo_fixture.MART_NAME
        cls.good_sql = cls.task.reference.sql_by_mart[cls.MART]

    def session(self, **options) -> V.ImplementerSession:
        return V.ImplementerSession(workspace=self.ws, task=self.task, **options)

    def dispatch(self, session, name, **args):
        return V.implementer_registry().dispatch(session.context(), name, args)

    def assert_clean(self, diag, task=None):
        payload = PJ.serialize_for_transport(diag, task=task or self.task)
        PJ.assert_value_free(payload.encode("utf-8"), task=task or self.task)


class DevQueryTest(ImplementerToolsCase):
    def test_dev_query_returns_source_base_tables_only(self):
        session = self.session()
        listed = self.dispatch(session, "list_schemas")
        self.assertEqual(listed.code, "schemas_listed")
        self.assertEqual(set(listed.names), {t.name for t in self.task.tables})
        for table in self.task.tables:
            rows = self.dispatch(session, "dev_query", sql=f"SELECT * FROM {table.name}")
            self.assertIsInstance(rows, PJ.DevRows)
            self.assertEqual(set(rows.columns), {c.name for c in table.columns})
            self.assertGreaterEqual(len(rows.rows), 1)
            self.assert_clean(rows)

    def test_dev_query_never_returns_marts_or_gold_tables(self):
        session = self.session()
        # A mart name is not a DEVELOPMENT source BASE TABLE: the AST validator
        # refuses any relation outside `task.tables` BEFORE execution, so the
        # code is `invalid_query` (finding 3-0), never a leak and never rows.
        d = self.dispatch(session, "dev_query", sql=f"SELECT * FROM {self.MART}")
        self.assertIsInstance(d, PJ.Diagnostic)
        self.assertEqual((d.source, d.code), (PJ.DiagnosticSource.DEV_QUERY, "invalid_query"))
        for gold_name in ("gold", "answer_key", "stage1_counts"):
            d = self.dispatch(session, "dev_query", sql=f'SELECT * FROM "{gold_name}"')
            self.assertEqual(d.code, "invalid_query")
        # The warehouse itself holds exactly the source BASE TABLEs, no gold/mart.
        import duckdb

        con = duckdb.connect(str(session.dev_warehouse), read_only=True)
        try:
            relations = con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE'"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual({r[0] for r in relations}, {t.name for t in self.task.tables})

    def test_dev_query_warehouse_is_resolved_by_path_equality(self):
        from elt_taskgen.training import dev_tool

        session = self.session()
        canonical = dev_tool.dev_warehouse_path(self.ws, self.task.task_id)
        # A `..`-laden path that RESOLVES to the canonical warehouse is accepted.
        detour = canonical.parent / "x" / ".." / dev_tool.DEV_WAREHOUSE_BASENAME
        session.dev_warehouse = detour
        rows = self.dispatch(session, "dev_query", sql="SELECT * FROM customers")
        self.assertIsInstance(rows, PJ.DevRows)
        # A symlink that resolves to the canonical warehouse is accepted.
        link = canonical.parent / "link_warehouse.duckdb"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(canonical)
        session.dev_warehouse = link
        self.assertIsInstance(
            self.dispatch(session, "dev_query", sql="SELECT * FROM customers"), PJ.DevRows
        )
        # A DECOY real file beside it (resolves elsewhere) is refused.
        decoy = canonical.parent / "decoy.duckdb"
        import shutil

        shutil.copyfile(canonical, decoy)
        session.dev_warehouse = decoy
        with self.assertRaises(S.ForbiddenArgument) as ctx:
            self.dispatch(session, "dev_query", sql="SELECT * FROM customers")
        self.assertEqual(ctx.exception.detail, "warehouse_path_mismatch")

    def test_dev_query_refuses_hidden_population_paths(self):
        from elt_taskgen.models import PopulationName
        from elt_taskgen.reference.runner import population_dir
        from elt_taskgen.training import dev_tool

        session = self.session()
        for hidden in (PopulationName.PRIMARY, PopulationName.STRESS, PopulationName.COUNTERFACTUAL):
            session.dev_warehouse = (
                population_dir(self.ws, self.task.task_id, hidden) / dev_tool.DEV_WAREHOUSE_BASENAME
            )
            with self.assertRaises(S.ForbiddenArgument, msg=hidden.value) as ctx:
                self.dispatch(session, "dev_query", sql="SELECT * FROM customers")
            self.assertEqual(ctx.exception.detail, "not_development_warehouse")

    def test_no_tool_reads_answer_key_stage1_counts_file(self):
        import builtins

        from elt_taskgen.reference.runner import population_dir

        session = self.session()
        # Poison the frozen stage-1 counts with an impossible value: the DEV
        # count must come from the ROWS, never from this file (A23 / gap_repo_6).
        poison = (
            population_dir(self.ws, self.task.task_id, __import__(
                "elt_taskgen.models", fromlist=["PopulationName"]
            ).PopulationName.DEVELOPMENT)
        )
        # Also poison the answer_key location the addendum names explicitly.
        ak = self.tdir / "answer_key" / "gold" / "development" / "stage1_counts.json"
        ak.parent.mkdir(parents=True, exist_ok=True)
        ak.write_text(json.dumps({"customers": 999999, "orders": 999999, "order_items": 999999}))

        opened: list[str] = []
        real_open = builtins.open

        def spy_open(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)

        with mock.patch.object(builtins, "open", spy_open):
            self.dispatch(session, "list_schemas")
            rows = self.dispatch(session, "dev_query", sql="SELECT * FROM customers")
        # The DEVELOPMENT customers count is 2 by construction — the poisoned
        # 999999 was never consulted.
        self.assertEqual(len(rows.rows), 2)
        self.assertFalse(
            [p for p in opened if "stage1_counts.json" in p or "answer_key" in p],
            "a witness tool read the answer-key stage-1 counts file",
        )

    def test_dev_query_call_cap_enforced_per_session(self):
        session = self.session()
        # SoT T1 IMP: `dev_query` per_tool cap is 8; the 9th is refused at PERMIT.
        self.assertEqual(V.implementer_tool("dev_query").cost.per_session, 8)
        script = [
            [BS.tool_use("dev_query", {"sql": "SELECT * FROM customers"}, f"t{i}")]
            for i in range(9)
        ]
        script.append(
            [BS.tool_use("submit_sql_by_mart", {"sql_by_mart": [{"mart": self.MART, "sql": self.good_sql}]}, "tsub")]
        )
        transport = BS.ScriptedProvider(script)
        limits = S.SessionLimits.declare(max_turns=12, max_tool_calls=20)
        result = S.run_bounded_session(
            V.IMPLEMENTER_ROLE, "the view", V.IMPLEMENTER_TOOLS,
            V.implementer_policy(limits), limits,
            provider=transport, ctx=session.context(), worker=V.implementer_validator_worker(),
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.tool_call_count, 8)  # exactly eight dev_query ran
        self.assertGreaterEqual(result.refused_count, 1)  # the ninth refused
        self.assertTrue(result.verify_chain())

    def test_dev_query_refuses_catalog_and_pragma_functions(self):
        """Findings 0-0 / 0-1 / 3-0: DuckDB catalog / settings / pragma /
        system-schema access discloses host paths, the secret directory and
        the version/platform fingerprint. Every one is refused with
        `external_access` on the AST — never executed, never delivered."""
        session = self.session()
        for sql in (
            "SELECT * FROM duckdb_databases()",
            'SELECT * FROM "duckdb_databases"()',
            "SELECT * FROM duckdb_databases/**/()",
            "SELECT * FROM duckdb_settings()",
            "SELECT * FROM pragma_version()",
            "SELECT * FROM pragma_user_agent()",
            "SELECT * FROM pragma_database_size()",
            "SELECT * FROM pragma_storage_info('customers')",
            "SELECT current_setting('secret_directory') AS customer_name",
            "SELECT current_setting('temp_directory') AS customer_name",
            "SELECT version() AS customer_name",
            "SELECT current_database() AS customer_name",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM pg_catalog.pg_tables",
        ):
            d = self.dispatch(session, "dev_query", sql=sql)
            self.assertIsInstance(d, PJ.Diagnostic, msg=sql)
            self.assertEqual((d.source, d.code), (PJ.DiagnosticSource.DEV_QUERY, "external_access"), msg=sql)
            self.assert_clean(d)  # the refusal itself carries no path/secret

    def test_dev_query_path_launder_variants_are_refused_not_delivered(self):
        """Finding 0-0 / 3-0: hex()/base64()/replace()/string_split() over a
        catalog function's host-path column must be REFUSED at validation, so
        the laundered bytes never reach `serialize_for_transport` /
        `assert_value_free` and never a `tool_result`."""
        session = self.session()
        for sql in (
            "SELECT hex(path) AS customer_name FROM duckdb_databases() WHERE path IS NOT NULL",
            "SELECT base64(path::BLOB) AS customer_name FROM duckdb_databases()",
            "SELECT replace(path, '/', ' ') AS customer_name FROM duckdb_databases()",
            "SELECT string_split(path, '/') AS customer_name FROM duckdb_databases()",
            "SELECT hex(current_setting('secret_directory')) AS customer_name",
            "SELECT replace(current_setting('temp_directory'), '/', ' ') AS customer_name",
        ):
            d = self.dispatch(session, "dev_query", sql=sql)
            self.assertIsInstance(d, PJ.Diagnostic, msg=sql)
            self.assertEqual(d.code, "external_access", msg=sql)
            # And the delivered bytes are the value-free refusal, nothing else.
            payload = PJ.serialize_for_transport(d, task=self.task)
            PJ.assert_value_free(payload.encode("utf-8"), task=self.task)
            for token in ("Users", "private", "/", "duckdb", "secret"):
                self.assertNotIn(token, "".join(d.names))

    def test_dev_query_benign_queries_return_codes_not_faults(self):
        """Finding 3-1: the most natural SELECTs must earn a Diagnostic code
        (or coerced `DevRows`), never crash the harness or trip a tripwire."""
        session = self.session()
        # A non-identifier output column (`SELECT 1`, `SELECT COUNT(*)`): a
        # correctable `invalid_query`, not a harness fault.
        for sql in ("SELECT 1", "SELECT COUNT(*) FROM customers"):
            d = self.dispatch(session, "dev_query", sql=sql)
            self.assertIsInstance(d, PJ.Diagnostic, msg=sql)
            self.assertEqual(d.code, "invalid_query", msg=sql)
        # An alias to a NON-PUBLIC identifier is `invalid_query`, not a leak
        # tripwire.
        d = self.dispatch(session, "dev_query", sql="SELECT customer_id AS cid FROM customers")
        self.assertIsInstance(d, PJ.Diagnostic)
        self.assertEqual(d.code, "invalid_query")
        # A wide cell is truncated to the bound; a DevRows still comes back.
        rows = self.dispatch(session, "dev_query", sql="SELECT repeat('a', 5000) AS customer_name FROM customers")
        self.assertIsInstance(rows, PJ.DevRows)
        self.assertTrue(rows.truncated)
        self.assertTrue(all(len(cell) <= 4096 for row in rows.rows for cell in row if isinstance(cell, str)))
        self.assert_clean(rows)
        # A non-finite float is coerced to NULL, never a JSON-compliance crash.
        rows = self.dispatch(session, "dev_query", sql="SELECT 'nan'::DOUBLE AS customer_id, 'inf'::DOUBLE AS total_spend")
        self.assertIsInstance(rows, PJ.DevRows)
        self.assertEqual(rows.rows, ((None, None),))
        self.assert_clean(rows)
        # A non-finite float NESTED inside a LIST / STRUCT / MAP cell used to
        # raise `ValueError` out of `canonical_json` inside the spawned worker
        # (a `worker_fault` ToolHarnessFault voiding the session). It is a
        # bounded page with the value nulled — a DevRows or a Diagnostic,
        # never an exception (finding 3-1).
        nested = {
            "SELECT ['nan'::DOUBLE] AS customer_id FROM customers": "[null]",
            "SELECT {'x': 'inf'::DOUBLE} AS customer_id FROM customers": '{"x":null}',
            "SELECT MAP {'k': 'nan'::DOUBLE} AS customer_id FROM customers": '{"k":null}',
            "SELECT [['nan'::DOUBLE]] AS customer_id FROM customers": "[[null]]",
            "SELECT MAP {'nan'::DOUBLE: 1} AS customer_id FROM customers": '{"null":1}',
        }
        for sql, cell in nested.items():
            with self.subTest(sql=sql):
                try:
                    out = self.dispatch(session, "dev_query", sql=sql)
                except Exception as exc:  # noqa: BLE001 - the assertion is "never an exception"
                    self.fail(f"{sql!r} raised {type(exc).__name__}")
                self.assertIsInstance(out, (PJ.DevRows, PJ.Diagnostic))
                if isinstance(out, PJ.DevRows):
                    self.assertTrue(all(row == (cell,) for row in out.rows), out.rows)
                    self.assert_clean(out)
                else:
                    self.assertEqual(out.code, "invalid_query")

    def test_dev_query_echoed_path_or_secret_literal_is_invalid_query_not_a_tripwire(self):
        """Finding 3-1 (owner decision): a path-, secret- or executor-text-
        shaped literal the model echoes into a DevRows cell is refused in
        `DevQueryTool.run` as the correctable `invalid_query` BEFORE
        `serialize_for_transport`, so a witness cannot void its own session
        (reward None, LEAK_TRIPWIRE) at will by selecting a path. The
        projector's detectors stay armed behind the refusal."""
        session = self.session()
        echoed = (
            "SELECT '/Users/someone/notes.txt' AS customer_name FROM customers",
            "SELECT '~/notes.txt' AS customer_name FROM customers",
            "SELECT 'AKIAIOSFODNN7EXAMPLE' AS customer_name FROM customers",
            "SELECT 'sk-ant-abcdefghijklmnop' AS customer_name FROM customers",
            "SELECT 'Binder Error: no such column' AS customer_name FROM customers",
        )
        for sql in echoed:
            with self.subTest(sql=sql):
                d = self.dispatch(session, "dev_query", sql=sql)
                self.assertIsInstance(d, PJ.Diagnostic)
                self.assertEqual((d.source, d.code, d.ok), (PJ.DiagnosticSource.DEV_QUERY, "invalid_query", False))
                self.assert_clean(d)
        # The same echo, handed straight to the projector, is still a tripwire:
        # the refusal in the tool is a measured code IN FRONT of the detector,
        # not a hole in it.
        rows = PJ.DevRows(columns=("customer_name",), rows=(("/Users/someone/notes.txt",),))
        self.assertEqual(PJ.dev_rows_leak_shape(rows), "path")
        with self.assertRaises(PJ.DiagnosticTripwire):
            PJ.serialize_for_transport(rows, task=self.task)
        self.assertIsNone(PJ.dev_rows_leak_shape(PJ.DevRows(columns=("customer_name",), rows=(("plain text",),))))
        # A plain literal still comes back as rows, so the refusal is narrow.
        plain = self.dispatch(session, "dev_query", sql="SELECT 'plain text' AS customer_name FROM customers")
        self.assertIsInstance(plain, PJ.DevRows)
        self.assert_clean(plain)

    def test_dev_query_runaway_does_not_poison_a_later_session(self):
        """Finding 3-2: a runaway query runs in a spawned, killable worker, so
        the deadline is the measured `execution_timeout` code and NO locked
        connection is left on the shared warehouse file — a fresh query after
        it still answers."""
        from elt_taskgen.training import dev_tool

        session = self.session()
        warehouse = dev_tool.assert_development_warehouse(
            session.dev_warehouse, session.workspace, session.task_id
        )
        allowed = frozenset(t.name for t in self.task.tables)
        # A deadline the spawned worker cannot beat: it is killed and the
        # measured `execution_timeout` code comes back (never a harness fault,
        # never a leaked connection). The spawn+open alone exceeds 1 ms.
        runaway = "SELECT * FROM customers a, customers b, customers c, customers d"
        with self.assertRaises(dev_tool.DevQueryError) as ctx:
            dev_tool.run_dev_query(warehouse, runaway, allowed_tables=allowed, deadline_s=0.001)
        self.assertEqual(ctx.exception.code, "execution_timeout")
        # The shared warehouse file is not poisoned: a fresh query still works,
        # both directly and through a brand new session/tool dispatch.
        rows = dev_tool.run_dev_query(warehouse, "SELECT * FROM customers", allowed_tables=allowed)
        self.assertIsInstance(rows, PJ.DevRows)
        fresh = self.dispatch(self.session(), "dev_query", sql="SELECT * FROM customers")
        self.assertIsInstance(fresh, PJ.DevRows)


class DryRunAndMartDevTest(ImplementerToolsCase):
    def test_dry_run_projection_drops_duckdb_text_and_classifies(self):
        session = self.session()
        # A binding query with the exact mart columns.
        good = self.dispatch(session, "dry_run_sql", mart=self.MART, sql=self.good_sql)
        self.assertEqual(good.source, PJ.DiagnosticSource.BIND)
        self.assertEqual(good.code, "binds")
        self.assertTrue(good.ok and good.flags["binds"] and good.flags["columns_match"])
        self.assert_clean(good)
        # Classification: parse, unknown column, and a column-set mismatch.
        cases = {
            "SELEC 1": "parse_error",
            "SELECT nope FROM customers": "unknown_column",
        }
        for sql, code in cases.items():
            d = self.dispatch(session, "dry_run_sql", mart=self.MART, sql=sql)
            self.assertEqual(d.code, code, sql)
            self.assertFalse(d.ok)
            # No DuckDB text, no path, no digit ever crosses the boundary.
            text = d.render()
            self.assertIsNone(re.search(r"Error|Binder|Catalog|Parser|LINE \d|/", text), text)
            self.assert_clean(d)
        mism = self.dispatch(session, "dry_run_sql", mart=self.MART, sql="SELECT customer_id AS customer_id FROM customers")
        self.assertEqual(mism.code, "binds")
        self.assertTrue(mism.flags["binds"])
        self.assertFalse(mism.flags["columns_match"])
        self.assertTrue(set(mism.names).issubset({c.name for c in self.task.mart(self.MART).columns}))

    def test_run_mart_sql_dev_returns_code_only_rows_and_counts_discarded(self):
        session = self.session()
        ok = self.dispatch(session, "run_mart_sql_dev", sql_by_mart=[{"mart": self.MART, "sql": self.good_sql}])
        self.assertEqual((ok.source, ok.code, ok.ok), (PJ.DiagnosticSource.MART_DEV, "ok", True))
        # The projection carries only {mart, code}: no numeric field, no rows.
        dump = ok.model_dump(mode="json")
        self.assertEqual(set(dump), {"source", "ok", "code", "subject", "flags", "names"})
        self.assertEqual(dump["flags"], {})
        self.assertEqual(dump["names"], [])
        self.assertNotIn("own_row_count", dump)
        self.assert_clean(ok)
        bad = self.dispatch(session, "run_mart_sql_dev", sql_by_mart=[{"mart": self.MART, "sql": "SELECT 1 AS wrong_col"}])
        self.assertEqual((bad.code, bad.subject), ("column_set_mismatch", self.MART))
        invalid = self.dispatch(session, "run_mart_sql_dev", sql_by_mart=[{"mart": self.MART, "sql": "SELECT * FROM missing_table"}])
        self.assertEqual(invalid.code, "invalid_query")
        self.assert_clean(bad)


class ImplementerWorkerAuditTest(ImplementerToolsCase):
    def test_implementer_manifest_has_no_dbt_write_shell_or_network_verbs(self):
        forbidden = ("dbt", "terraform", "shell", "bash", "sync", "network", "http", "run_dbt", "write_candidate", "curl", "socket")
        for tool in V.IMPLEMENTER_TOOLS:
            lowered = tool.name.lower()
            self.assertFalse([tok for tok in forbidden if tok in lowered], tool.name)
            props = dict(tool.input_schema).get("properties", {})
            self.assertFalse({"path", "url", "uri", "file", "filename", "dir", "directory"} & set(props), tool.name)
            # No implementer tool writes a file / runs dbt / opens a network.
            self.assertFalse(bool(getattr(tool, "surface_write", False)), tool.name)

    def test_implementer_worker_holds_no_gold_or_credentials(self):
        session = self.session()
        for attr in ("gold", "package", "credential", "credentials", "reference", "secret"):
            self.assertIsNone(getattr(session, attr, None), attr)
        ctx = session.context()
        self.assertIsNone(ctx.package)
        self.assertEqual(ctx.population, "development")
        # The materialized DEVELOPMENT warehouse is gold-free: source tables only.
        import duckdb

        con = duckdb.connect(str(session.dev_warehouse), read_only=True)
        try:
            names = {
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM information_schema.tables"
                ).fetchall()
            }
        finally:
            con.close()
        self.assertEqual(names, {t.name for t in self.task.tables})
        self.assertFalse({"gold", "answer_key"} & names)

    def test_implementer_worker_env_is_replaced_and_keyless(self):
        import os

        from elt_taskgen.training import dev_tool

        key = "ELT_TASKGEN_TEST_FAKE_API_KEY"
        os.environ[key] = "supersecret-value"
        self.addCleanup(lambda: os.environ.pop(key, None))
        # The SPAWNED worker scrubs every credential-shaped variable: a keyless
        # child returns no survivors, even though the parent set one.
        survivors = dev_tool.spawned_env_survivors()
        self.assertEqual(survivors, [])


class LoaderToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = _dev_fixture()
        cls.task = fixture["task"]
        cls.ws = fixture["ws"]
        cls.good_plan = [
            {"table": "customers", "path": "postgres/customers.sql", "format": "postgres_sql"},
            {"table": "orders", "path": "mongodb/orders.jsonl", "format": "jsonl"},
            {"table": "order_items", "path": "files/order_items.csv", "format": "csv"},
        ]

    def session(self) -> V.LoaderSession:
        return V.LoaderSession(workspace=self.ws, task=self.task)

    def dispatch(self, session, name, **args):
        return V.loader_registry().dispatch(session.context(), name, args)

    def test_check_load_plan_is_static_and_returns_no_counts(self):
        from elt_taskgen.corpus import calibration as _cal

        session = self.session()
        # STATIC: it never calls the executor.
        with mock.patch.object(
            _cal, "execute_load_plan", side_effect=AssertionError("check_load_plan executed the plan")
        ):
            ok = self.dispatch(session, "check_load_plan", plan=self.good_plan)
        self.assertEqual((ok.source, ok.code, ok.ok), (PJ.DiagnosticSource.LOAD_PLAN, "ok", True))
        # No numeric field, no count: only {code, subject}.
        dump = ok.model_dump(mode="json")
        self.assertEqual(dump["flags"], {})
        self.assertEqual(dump["names"], [])
        self.assertIsNone(re.search(r"\d", ok.render()), ok.render())
        cases = [
            ([dict(self.good_plan[0], format="parquet")] + self.good_plan[1:], "unknown_reader", "customers"),
            ([dict(self.good_plan[0], path="../secret.sql")] + self.good_plan[1:], "path_escape", "customers"),
            (self.good_plan[:2], "table_uncovered", "order_items"),
            ([dict(self.good_plan[0], format="s3_jsonl", path="s3/customers.jsonl")] + self.good_plan[1:], "s3_part_file", "customers"),
        ]
        for plan, code, table in cases:
            d = self.dispatch(session, "check_load_plan", plan=plan)
            self.assertEqual((d.code, d.subject), (code, table), code)

    def test_check_load_plan_nul_byte_path_is_path_escape_not_a_harness_fault(self):
        """Finding 3-4: a NUL byte in a load-plan path used to crash the static
        checker (`Path(...).resolve()` -> `ValueError: embedded null byte` ->
        `ToolHarnessFault`). It must earn `path_escape`, never a harness fault."""
        session = self.session()
        for bad in ("postgres/cust\x00omers.sql", "postgres\\customers.sql"):
            plan = [dict(self.good_plan[0], path=bad)] + self.good_plan[1:]
            d = self.dispatch(session, "check_load_plan", plan=plan)
            self.assertIsInstance(d, PJ.Diagnostic)
            self.assertEqual((d.code, d.subject), ("path_escape", "customers"), bad)

    def test_loader_view_and_every_tool_result_pass_assert_view_clean(self):
        from elt_taskgen.reference import independent

        session = self.session()
        view = independent.loader_view(self.task, session.bundle_dir)
        # The view carries no answer-side material (the council's own detector).
        independent._assert_view_clean(self.task, view)
        self.assertNotIn("answer_key", view.lower())
        self.assertNotIn("reference.sql_by_mart", view)
        s3_part_plan = [
            dict(self.good_plan[0], format="s3_jsonl", path="s3/customers.jsonl")
        ] + self.good_plan[1:]
        calls = [
            ("replace_load_plan", {"plan": self.good_plan}),
            ("check_load_plan", {"plan": self.good_plan}),
            ("check_load_plan", {"plan": self.good_plan[:1]}),
            ("check_load_plan", {"plan": s3_part_plan}),
            ("submit_load_plan", {}),
            ("abort", {"reason_code": "infeasible"}),
        ]
        for name, args in calls:
            diag = self.dispatch(session, name, **args)
            self.assertIsInstance(diag, PJ.Diagnostic)
            payload = PJ.serialize_for_transport(diag, task=self.task)
            PJ.assert_value_free(payload.encode("utf-8"), task=self.task)
            if diag.code == "s3_part_file":
                self.assertEqual(diag.subject, "customers")
            else:
                self.assertIsNone(re.search(r"\d", diag.render()), diag.render())

    def test_batch_338_recorded_loader_turn_is_delivered_not_tripped(self):
        """Batch D5 (reports 338/339, task synsql__educational_expenditure_…):
        the loader's RECORDED `replace_load_plan` named the s3 part FILE
        (`s3/regions/part-00000.jsonl`) for an `s3_jsonl` table, so the
        static check answered `s3_part_file` — and the `3` of that closed
        code tripped the projector's numeric canary (`the canary detector
        refused delivery of a load_plan projection (numeric_value)`), a
        harness fault that ended the EL gate before the correction reached
        the model. Replayed from the on-disk transcript through the real
        static check, projector and gatekeeper: the correction is delivered
        as `[load_plan] s3_part_file subject=regions ok=false`; no digit of
        the plan's PATHS (`part-00000`, `page_0001`) ever enters the
        projection, only the public table name does. Skipped when the batch
        is not on disk."""
        batch = repo_root() / "runs" / "authorized_batch_50_20260908" / "workspace-final"
        task_id = "synsql__educational_expenditure_data_and_analysis__users_expenditures_distribution"
        transcript = (
            batch / "transcripts" / "independent_loader"
            / "82fdbd04dcabd5c58ef8f736512813288df9eb18bac80997f47b0e6c57418d81.json"
        )
        ir = batch / "tasks" / task_id / "task_ir.json"
        if not (ir.is_file() and transcript.is_file()):
            self.skipTest("batch evidence not on disk")
        from elt_taskgen.models import TaskIR

        task = TaskIR.model_validate(json.loads(ir.read_bytes()))
        record = json.loads(transcript.read_bytes())
        self.assertEqual(record["task_id"], task_id)
        self.assertEqual(record["task_content_hash"], task.content_hash())
        call = next(b for b in record["turn"]["content"] if b.get("type") == "tool_use")
        self.assertEqual(call["name"], V.LOADER_REPLACE_TOOL)
        with tempfile.TemporaryDirectory() as tmp:
            session = V.LoaderSession(workspace=Path(tmp), task=task, bundle_dir=Path(tmp) / "bundle",
                                      rendered_dev=Path(tmp) / "rendered")
            ctx = session.context(root=Path(tmp) / "scratch")
            applied = V.loader_registry().dispatch(ctx, V.LOADER_REPLACE_TOOL, call["input"])
            self.assertEqual((applied.source, applied.code), (PJ.DiagnosticSource.TRIAL, "applied"))
            self.assertEqual(session.plan["regions"], {"path": "s3/regions/part-00000.jsonl", "format": "s3_jsonl"})
            # The auto-run static check on the accumulated plan (no args).
            diag = V.loader_registry().dispatch(ctx, V.LOADER_CHECK_TOOL, {})
        self.assertEqual((diag.source, diag.code, diag.subject, diag.ok),
                         (PJ.DiagnosticSource.LOAD_PLAN, "s3_part_file", "regions", False))
        self.assertIn("regions", PJ.PublicIdentifierSet(task))
        payload = PJ.serialize_for_transport(diag, task=task)  # the D3 half: no tripwire
        PJ.assert_value_free(payload.encode("utf-8"), task=task)  # the D1 half: no tripwire
        self.assertEqual(diag.render(), "[load_plan] s3_part_file subject=regions ok=false")
        self.assertNotIn("00000", payload)
        self.assertNotIn("part-", payload)
        # The same recorded plan with the prefix DIRECTORIES the `s3_jsonl` and
        # `rest_pages` readers want is statically clean (the certifier scores it).
        prefixed = {
            item["table"]: {"format": item["format"],
                            "path": item["path"].rsplit("/", 1)[0] if item["format"] in ("s3_jsonl", "rest_pages") else item["path"]}
            for item in call["input"]["plan"]
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(V._check_load_plan_static(task, prefixed, Path(tmp)), ("ok", ""))

    def test_check_load_plan_subject_is_the_public_table_name_even_with_digits(self):
        """Batch D5 class: a public table name carrying digits (`sales-2020`,
        `covid_19_cases`, `v1.2`, `t2`) travels in `subject` and is recognized
        through `PublicIdentifierSet` by the projector and the gatekeeper for
        every load-plan code; the model's OWN table string never reaches the
        projection (the static check names `task.tables`, so a digit-bearing
        string the model invents is `table_unknown` with the subject withheld
        — review finding 1-5 — and, once dropped, `table_uncovered` on a
        PUBLIC name)."""
        try:
            from test_review_tools_projection import DIGIT_BEARING_TABLES, digit_bearing_task
        except ImportError:  # pragma: no cover - depends on how the suite is invoked
            from tests.test_review_tools_projection import DIGIT_BEARING_TABLES, digit_bearing_task

        task = digit_bearing_task()
        public = PJ.PublicIdentifierSet(task)
        plan = {
            "customers": {"path": "postgres/customers.sql", "format": "postgres_sql"},
            "orders": {"path": "mongodb/orders.jsonl", "format": "jsonl"},
            "order_items": {"path": "files/order_items.csv", "format": "csv"},
        }
        for name in DIGIT_BEARING_TABLES:
            plan[name] = {"path": f"s3/{name}", "format": "s3_jsonl"}
        with tempfile.TemporaryDirectory() as tmp:
            session = V.LoaderSession(workspace=Path(tmp), task=task, bundle_dir=Path(tmp) / "bundle",
                                      rendered_dev=Path(tmp) / "rendered")
            ctx = session.context(root=Path(tmp) / "scratch")
            registry = V.loader_registry()
            ok = registry.dispatch(ctx, V.LOADER_CHECK_TOOL, {"plan": plan})
            self.assertEqual((ok.code, ok.subject), ("ok", ""))
            for name in DIGIT_BEARING_TABLES:
                self.assertIn(name, public)
                variants = {
                    "unknown_reader": dict(plan, **{name: {"path": f"s3/{name}", "format": "parquet"}}),
                    "path_escape": dict(plan, **{name: {"path": "../x", "format": "s3_jsonl"}}),
                    "s3_part_file": dict(plan, **{name: {"path": f"s3/{name}/part-00000.jsonl", "format": "s3_jsonl"}}),
                    "table_uncovered": {t: s for t, s in plan.items() if t != name},
                }
                for code, variant in variants.items():
                    diag = registry.dispatch(ctx, V.LOADER_CHECK_TOOL, {"plan": variant})
                    self.assertEqual((diag.code, diag.subject), (code, name), name)
                    payload = PJ.serialize_for_transport(diag, task=task)
                    PJ.assert_value_free(payload.encode("utf-8"), task=task)
                    self.assertEqual(json.loads(payload)["subject"], name)
                # A model-invented digit-bearing table string is never the
                # subject: the plan entry naming no source table is refused
                # first as `table_unknown` (review finding 1-5) with the
                # model-authored name withheld, and the invented string
                # reaches neither the subject nor the wire.
                invented = dict(plan)
                invented["sales-2021"] = invented.pop(name)
                diag = registry.dispatch(ctx, V.LOADER_CHECK_TOOL, {"plan": invented})
                self.assertEqual((diag.code, diag.subject), ("table_unknown", ""))
                payload = PJ.serialize_for_transport(diag, task=task)
                PJ.assert_value_free(payload.encode("utf-8"), task=task)
                self.assertNotIn("sales-2021", payload)
                # With the invented entry dropped, the missing table is named.
                del invented["sales-2021"]
                diag = registry.dispatch(ctx, V.LOADER_CHECK_TOOL, {"plan": invented})
                self.assertEqual((diag.code, diag.subject), ("table_uncovered", name))
                PJ.assert_value_free(PJ.serialize_for_transport(diag, task=task).encode("utf-8"), task=task)


class CouncilManifestC9Test(unittest.TestCase):
    def test_c9_trigger_audit_for_council_manifests(self):
        """No COUNCIL-seat manifest carries a dbt, terraform, shell or network
        verb (constraint addendum §4; the SQL-artifact witnesses pull no C9
        trigger). The dbt/Terraform verbs belong to the L2 policy, not here."""
        forbidden = (
            "dbt", "terraform", "shell", "bash", "network", "http", "curl",
            "socket", "sync_dev", "write_candidate", "run_dbt", "lint_model",
        )
        council_roles = (
            V.AUTHOR_ROLE, V.ROLE, V.IMPLEMENTER_ROLE, V.LOADER_ROLE,
        )
        seen = 0
        for role in council_roles:
            registry = RG.ToolRegistry.declared_for_role(role)
            for name in registry.names:
                tool = registry.get(name)
                seen += 1
                lowered = tool.name.lower()
                self.assertFalse(
                    [tok for tok in forbidden if tok in lowered],
                    f"{role}:{tool.name} names a C9-trigger verb",
                )
                props = dict(getattr(tool, "input_schema", {})).get("properties", {})
                self.assertNotIn("url", props, f"{role}:{tool.name}")
                self.assertNotIn("uri", props, f"{role}:{tool.name}")
        self.assertGreater(seen, 0)


# End-to-end witness-tool integration through the real bounded runner,
# policies, certifier, evidence, and gate readers.

class _WitnessSessionDouble:
    """An offline provider running the REAL runner over a scripted FIFO of
    turns per session (the shape `RoutedProvider.run_session` has); the
    witnesses' tools dispatch in-process against the real projections."""

    unrouted_test_double = True
    agents_config = None

    def __init__(self, *scripts):
        self._make = BS.ScriptedProvider
        self.scripts = [list(s) for s in scripts]
        self.transports: list = []

    def complete(self, role, prompt):
        raise AssertionError("one-shot complete() must not run when the session is enabled")

    def run_session(self, role, view, policy, ctx, **kwargs):
        role_name = getattr(role, "value", str(role))
        transport = self._make(self.scripts.pop(0))
        self.transports.append(transport)
        kwargs.setdefault("worker", None)
        return S.run_bounded_session(
            role_name, view, policy.tools, policy, policy.limits,
            provider=transport, ctx=ctx, **kwargs,
        )


class WitnessSessionIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = _dev_fixture()
        cls.task = fixture["task"]
        cls.ws = fixture["ws"]
        cls.gold = fixture["gold"]
        cls.MART = demo_fixture.MART_NAME
        cls.good_sql = cls.task.reference.sql_by_mart[cls.MART]

    def setUp(self) -> None:
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    def test_implementer_session_build_records_provenance_and_passes_the_gate(self):
        from elt_taskgen.reference import independent
        from elt_taskgen.review.metrology import HARNESS_VERSION

        block = dict(P.DEFAULT_ROUTING_DOC["roles"][V.IMPLEMENTER_ROLE]["session"])
        block["enabled"] = True
        policy = V.implementer_policy(V.implementer_limits(block))
        self.assertEqual(policy.role, V.IMPLEMENTER_ROLE)
        script = [
            [BS.tool_use("list_schemas", {}, "t0")],
            [BS.tool_use("dry_run_sql", {"mart": self.MART, "sql": self.good_sql}, "t1")],
            [BS.tool_use("run_mart_sql_dev", {"sql_by_mart": [{"mart": self.MART, "sql": self.good_sql}]}, "t2")],
            [BS.tool_use("submit_sql_by_mart", {"sql_by_mart": [{"mart": self.MART, "sql": self.good_sql}]}, "t3")],
        ]
        provider = _WitnessSessionDouble(script)
        result = independent.run_independent_build(
            self.task, self.ws, provider, self.gold, session_policy=policy
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(len(result.samples), 1)
        sample = result.samples[0]
        self.assertEqual(sample.sql_by_mart, {self.MART: self.good_sql})
        self.assertEqual(sample.terminal, "SUBMITTED")
        self.assertEqual(sample.turns, 4)
        # `check_submission` is the harness-run dry run of the submission
        # (2026-09-11, batch10 run O), recorded as a tool turn like every
        # harness validator.
        self.assertEqual([c.name for c in sample.tool_calls], ["list_schemas", "dry_run_sql", "run_mart_sql_dev", "check_submission"])
        self.assertEqual(sample.build_harness_version, HARNESS_VERSION)
        self.assertEqual(sample.manifest_sha256, policy.tools_sha256())
        self.assertEqual(sample.policy_sha256, policy.sha256())
        self.assertEqual(sample.limits, S.SessionLimits.from_block(block).as_manifest())
        self.assertTrue(sample.limits["enabled"])
        self.assertEqual(sample.limits["per_tool"], {"dev_query": 8, "dry_run_sql": 8, "run_mart_sql_dev": 2})
        for call in sample.tool_calls:
            self.assertEqual(call.sanitizer_version, PJ.DIAGNOSTICS_VERSION)
        # Recorded, re-loaded, and green at the gate that consumes it.
        independent.record_build_result(self.ws, self.task, result)
        loaded = independent.load_build_result(self.ws, self.task.task_id)
        self.assertEqual(loaded["samples"][0]["tool_calls"][0]["name"], "list_schemas")
        self.assertEqual(loaded["samples"][0]["build_harness_version"], HARNESS_VERSION)
        revived = independent.IndependentBuildResult.model_validate(loaded)
        self.assertEqual(revived, result)
        gate = gates_mod._gate_dual_build_agreement(self.task, self.ws)
        self.assertTrue(gate.passed, gate.details)
        trusted = gates_mod._gate_trusted_solution(self.task, self.ws, self.gold)
        self.assertTrue(trusted.passed, trusted.details)

    def test_loader_session_build_records_provenance_and_passes_the_gate(self):
        from elt_taskgen.reference import independent
        from elt_taskgen.review.metrology import HARNESS_VERSION

        block = dict(P.DEFAULT_ROUTING_DOC["roles"][V.LOADER_ROLE]["session"])
        block["enabled"] = True
        policy = V.loader_policy(V.loader_limits(block))
        plan = [
            {"table": "customers", "path": "postgres/customers.sql", "format": "postgres_sql"},
            {"table": "orders", "path": "mongodb/orders.jsonl", "format": "jsonl"},
            {"table": "order_items", "path": "files/order_items.csv", "format": "csv"},
        ]
        provider = _WitnessSessionDouble([
            [BS.tool_use("replace_load_plan", {"plan": plan}, "t0")],
            [BS.tool_use("submit_load_plan", {}, "t1")],
        ])
        result = independent.run_independent_load_build(
            self.task, self.ws, provider, self.gold, session_policy=policy
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        sample = result.samples[0]
        self.assertEqual(sample.load_plan, {p["table"]: {"path": p["path"], "format": p["format"]} for p in plan})
        self.assertEqual(sample.terminal, "SUBMITTED")
        self.assertEqual(sample.turns, 2)
        # The static check auto-ran on the write: a validator turn, recorded.
        self.assertEqual([c.name for c in sample.tool_calls], ["replace_load_plan", "check_load_plan"])
        self.assertEqual(sample.build_harness_version, HARNESS_VERSION)
        self.assertEqual(sample.manifest_sha256, policy.tools_sha256())
        self.assertEqual(sample.limits, S.SessionLimits.from_block(block).as_manifest())
        independent.record_load_build_result(self.ws, self.task, result)
        gate = gates_mod._gate_el_independent_load(self.task, self.ws)
        self.assertTrue(gate.passed, gate.details)
        revived = independent.IndependentLoadBuildResult.model_validate(
            independent.load_load_build_result(self.ws, self.task.task_id)
        )
        self.assertEqual(revived, result)



class CheckSubmissionValidatorTest(ImplementerToolsCase):
    """batch10 run O (2026-09-11), synsql__educational: the witness submitted a
    statement that did not bind, it scored 0 on every population, and one of
    three sessions was spent. The harness-run `check_submission` validator
    dry-runs every submitted mart: red, naming the mart, when a statement
    does not bind or misses declared columns; green when all bind."""

    def test_an_unbound_submission_is_red_and_names_the_mart(self):
        session = self.session()
        bad = self.good_sql.replace("SELECT", "SELECT nonexistent_column_zz,", 1)
        diag = self.dispatch(session, "check_submission", sql_by_mart=[{"mart": self.MART, "sql": bad}])
        self.assertFalse(diag.ok)
        self.assertEqual(diag.subject, self.MART)
        self.assertFalse(diag.flags.get("binds"))
        self.assert_clean(diag)

    def test_a_binding_submission_is_green(self):
        session = self.session()
        diag = self.dispatch(session, "check_submission", sql_by_mart=[{"mart": self.MART, "sql": self.good_sql}])
        self.assertTrue(diag.ok, diag)
        self.assertEqual(diag.code, "binds")
        self.assert_clean(diag)

    def test_the_validator_is_harness_only_and_declared(self):
        from elt_taskgen.review import providers as P
        self.assertNotIn("check_submission", [t["name"] for t in V.implementer_registry().wire_tools()])
        self.assertEqual(P.role_loop_limits("independent_implementer")["harness_validators"], ["check_submission"])

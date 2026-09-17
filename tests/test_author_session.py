"""Tests for the semantic author's harness-driven revision session (roadmap
Phase 1 item 1.A; SoT T1 AUT, T3; the permission matrix's AUT column).

WHY THIS EXISTS
`council.author_prose_session` sits beside the untouched `council.author_prose`
and `cli.make_author_runner` calls it only when
`roles.semantic_author.session` is `enabled` with `max_revisions > 0`. These
tests pin, with NO network and NO live model (a `FakeTransport` FIFO of raw
API bodies through the real `RoutedProvider.run_session`, or the real runner
over a scripted provider), that:

  * every submitted draft is checked harness-side by `check_prose` — ONE call
    to `prose_fidelity.check_prose_fidelity` (declarative prose included) on
    `task.model_copy(update={"solver_prompt": draft})` — projected through
    `project_prose_problems` to codes only, fed back as a revision request,
    and that a green draft stops the session at zero problems;
  * the leak scan (`council.leak_findings`) never runs inside the loop and
    stays at review; `check_structure` is not an author tool;
  * the PRESERVED-patch rule of the author stage runs BEFORE any session;
  * the ledger row's `source` gains `authored_revised` with the revision count;
  * either rollback key (`enabled: false`, or `max_revisions: 0`) is
    byte-identical to the one-shot `author_prose` path: same prompt, same
    wire, same ledger data, while the shipped default uses the session;
  * the author manifest folds into `role_behavior_sha256("semantic_author")`
    only, never into the admission fingerprint (R-B).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import cli, demo_fixture
from elt_taskgen import engine as engine_mod
from elt_taskgen.generation.mart_plan import solver_safe_mart_requirements
from elt_taskgen.models import CouncilRole, MartOpKind, RepairRoute
from elt_taskgen.review import council, declarative_prose, metrology, prompts, prose_fidelity
from elt_taskgen.review import providers as P
from elt_taskgen.review import session as S
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG
from elt_taskgen.review.tools import validators as V
from elt_taskgen.verification import structural_completeness
from tests.test_bounded_session import ScriptedProvider, tool_use
from tests.test_prose_fidelity import DECLARATIVE_PROSE
from tests.test_providers import FakeTransport
from tests.test_providers_session import _bound, _provider, tool_use_body

AUTHOR = "semantic_author"
TASK = demo_fixture.demo_task()
#: Complete but a recipe: red on the declarative half of the ONE check.
RECIPE_PROSE = metrology.build_prose(TASK)
#: Red on the completeness half: every item of the mart is missing.
INCOMPLETE_PROSE = "This prose omits everything that matters."
GREEN_PROSE = DECLARATIVE_PROSE

ENABLED_BLOCK = {
    "enabled": True,
    "max_revisions": 1,
    "max_turns": 2,
    "max_tool_calls": 2,
    "max_usd": 1.00,
    "wall_clock_s": 300,
    "hard_caps": {"revisions": 2, "usd": 1.50, "wall_clock_s": 1800},
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _author_doc(**block):
    """A copy of the loaded agents document with the author's `session:`
    block replaced by `ENABLED_BLOCK` updated with `block`."""
    doc = json.loads(json.dumps(P._agents_doc()))
    doc["roles"][AUTHOR]["session"] = {**ENABLED_BLOCK, **block}
    return doc


class _AuthorConfig:
    """Patch the loaded agents document for the duration of a test and drop
    the behaviour caches on the way in and out."""

    def __init__(self, **block):
        self.doc = _author_doc(**block)
        self._patch = mock.patch.object(P, "_agents_doc", lambda: self.doc)

    def __enter__(self):
        self._patch.__enter__()
        P.clear_behavior_caches()
        return self

    def __exit__(self, *exc):
        self._patch.__exit__(*exc)
        P.clear_behavior_caches()
        return False


def _policy(**block) -> S.SessionPolicy:
    return V.author_policy(V.author_limits({**ENABLED_BLOCK, **block}))


def _submit(text: str, id_: str = "t0") -> dict:
    return tool_use_body(V.AUTHOR_SUBMIT_TOOL, {"text": text}, id=id_)


def _tool_results(payload) -> list[tuple[str, bool]]:
    """(content, is_error) of every tool_result the request carried."""
    out = []
    for message in payload["messages"]:
        if message.get("role") == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    out.append((str(block["content"]), bool(block.get("is_error"))))
    return out


def _prose_flag_vocabulary(task) -> set[str]:
    """Every flag key `project_prose_check` may emit for `task` (D9 grammar):
    `prose_<kind>`, `prose_item_<item>`, `prose_mart_<mart>_<item>`,
    `prose_mart_<mart>_rule_<operation>`, `prose_operator_<category>`."""
    kinds = {f"prose_{k}" for k in PJ.PROSE_KINDS}
    items = {V._flag_key(V.PROSE_ITEM_FLAG_PREFIX, i) for i in prose_fidelity.PROSE_ITEM_KINDS}
    marts: set[str] = set()
    for mart in task.marts:
        for item in prose_fidelity.PROSE_ITEM_KINDS:
            key = V._mart_flag(mart.name, item)
            if key:
                marts.add(key)
        for op in MartOpKind:
            key = V._mart_flag(mart.name, "rule", op.value)
            if key:
                marts.add(key)
    categories = {
        V._flag_key(V.PROSE_OPERATOR_FLAG_PREFIX, c) for c in (
            "join-operator", "function-call", "coalesce", "distinct-operator",
            "by-clause", "case-expression", "set-operator", "window-syntax",
            "join-predicate", "query-clause",
        )
    }
    return kinds | items | marts | categories


class SessionDouble:
    """An offline provider that runs the REAL runner over a scripted FIFO of
    turns the way `RoutedProvider.run_session` does; `complete` is refused so
    a test proves the session path was taken."""

    def __init__(self, script):
        self.scripted = ScriptedProvider(list(script))
        self.sessions: list[dict] = []
        self.completes = 0

    def complete(self, role, prompt):
        self.completes += 1
        raise AssertionError("one-shot complete() must not run when the session is enabled")

    def run_session(self, role, view, policy, ctx, **kwargs):
        role_name = getattr(role, "value", str(role))
        self.sessions.append({"role": role_name, "view": view, "policy": policy})
        kwargs.setdefault("worker", None)
        return S.run_bounded_session(
            role_name, view, policy.tools, policy, policy.limits,
            provider=self.scripted, ctx=ctx, **kwargs,
        )


class OneShotDouble:
    """A one-shot provider: `complete` answers, `run_session` must never run."""

    def __init__(self, prose: str):
        self.prose = prose
        self.prompts: list = []
        self.sessions = 0

    def complete(self, role, prompt):
        self.prompts.append((role, prompt))
        return self.prose

    def run_session(self, *args, **kwargs):
        self.sessions += 1
        raise AssertionError("the session must not run under an explicit rollback config")


def _engine(tmp: str, *, rows=None, history=None):
    """A duck-typed engine: a workspace and the two ledger readers the
    author stage consults."""
    rows = rows or {}

    def latest_report(task_id, stage):
        return rows.get((stage, "latest"))

    def latest_report_with_verdict(task_id, stage, verdict):
        return rows.get((stage, verdict))

    def report_history(task_id, stage):
        return tuple((history or {}).get(stage, ()))

    values = dict(
        workspace=Path(tmp),
        latest_report=latest_report,
        latest_report_with_verdict=latest_report_with_verdict,
    )
    if history is not None:
        values["report_history"] = report_history
    return SimpleNamespace(**values)


def _pass_row(data: dict):
    return SimpleNamespace(verdict=cli.VERDICT_PASS, payload_json=json.dumps({"data": data}))


def _report_row(verdict: str, data: dict, *, id_: int):
    return SimpleNamespace(
        id=id_, verdict=verdict, payload_json=json.dumps({"data": data})
    )


class _Case(unittest.TestCase):
    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)


# ---------------------------------------------------------------------------
# The loop, end to end through the real provider layer
# ---------------------------------------------------------------------------

class AuthorSessionTest(_Case):
    def test_author_session_feeds_prose_problem_codes_and_stops_at_zero(self):
        """A red first draft comes back to the model as CODES ONLY (the gate,
        the kinds, the public mart / column names — never a sentence, never
        a number), the revised draft is checked again, and a green check ends
        the session; a green FIRST draft ends it after one turn: the loop
        stops at zero problems, it never asks for a revision it does not need."""
        policy = _policy()
        self.assertEqual([t["name"] for t in policy.wire_tools], ["abort", "submit_prose"])
        self.assertEqual(policy.limits.harness_validators, ("check_prose",))
        self.assertEqual(policy.limits.max_compile_corrections, 1)
        for red, expected_flag, expect_mart in (
            (RECIPE_PROSE, "prose_operator_vocabulary=true", False),
            (INCOMPLETE_PROSE, "prose_not_represented=true", True),
        ):
            with self.subTest(red=expected_flag), tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([_submit(red, "t0"), _submit(GREEN_PROSE, "t1")])
                provider = _bound(_provider(tmp, transport))
                record: dict = {}
                prose = council.author_prose_session(
                    TASK, provider, tools=policy.tools, policy=policy, record=record
                )
                self.assertEqual(prose, GREEN_PROSE)
                self.assertEqual(len(transport.calls), 2)
                result = record["result"]
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual(result.model_call_count, 2)
                self.assertEqual(result.validator_run_count, 2)
                self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
                self.assertEqual(record["revisions"], 1)
                self.assertEqual(record["drafts"], 2)
                validators = [t for t in result.turns if t.kind == "validator"]
                self.assertEqual([t.tool_name for t in validators], ["check_prose", "check_prose"])
                self.assertEqual([t.outcome_code for t in validators], ["prose_problems", "cheap_green"])
                # The revision request: the second request carries ONE tool_result,
                # an error, with the codes and nothing else.
                second = transport.calls[1][2]
                self.assertEqual([t["name"] for t in second["tools"]], ["abort", "submit_prose"])
                self.assertEqual(second["tool_choice"], {"type": "any"})
                results = _tool_results(second)
                self.assertEqual(len(results), 1)
                content, is_error = results[0]
                self.assertTrue(is_error)
                self.assertTrue(content.startswith("[cheap] prose_problems"), content)
                self.assertIn("prose_ok=false", content)
                self.assertIn(expected_flag, content)
                self.assertFalse(re.search(r"\d", content), content)
                for sentence in prose_fidelity.check_prose_fidelity(
                    TASK.model_copy(update={"solver_prompt": red})
                ):
                    self.assertNotIn(sentence, content)
                if expect_mart:
                    self.assertIn("names=", content)
                    self.assertIn(demo_fixture.MART_NAME, content)
                # One evidence row per session (SoT T8).
                self.assertEqual(len(provider.exchange_evidence), 1)
                row = provider.exchange_evidence[-1]
                self.assertEqual(row["role"], AUTHOR)
                self.assertEqual(row["terminal"], "SUBMITTED")
                self.assertEqual(row["validator_run_count"], 2)
        # A green first draft: one turn, no revision, nothing asked twice.
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([_submit(GREEN_PROSE, "t0")])
            provider = _bound(_provider(tmp, transport))
            record = {}
            prose = council.author_prose_session(
                TASK, provider, tools=policy.tools, policy=policy, record=record
            )
            self.assertEqual(prose, GREEN_PROSE)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(record["revisions"], 0)
            self.assertEqual(record["drafts"], 1)
            self.assertIs(record["result"].terminal, S.TerminalState.SUBMITTED)
            self.assertEqual(record["result"].correction_count, 0)

    def test_author_loop_never_runs_leak_findings_and_leak_scan_stays_at_review(self):
        """The leak scan is not an author tool and is never executed inside
        the loop, on a red or a green draft; `run_council` still runs it
        first on the authored task (the firewall stays at review)."""
        calls = {"leak": 0, "ast": 0, "semantic": 0}
        real_leak = council.leak_findings

        def counting_leak(task):
            calls["leak"] += 1
            return real_leak(task)

        def counting(name):
            def spy(task):
                calls[name] += 1
                return []

            return spy

        policy = _policy()
        self.assertNotIn("leak_findings", V.AUTHOR_TOOL_NAMES)
        self.assertNotIn("leak_findings", policy.allowlist)
        self.assertEqual(policy.limits.harness_validators, ("check_prose",))
        with mock.patch.object(council, "leak_findings", counting_leak), \
                mock.patch.object(council, "ast_leak_findings", counting("ast")), \
                mock.patch.object(council, "semantic_leak_findings", counting("semantic")), \
                tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([_submit(RECIPE_PROSE, "t0"), _submit(GREEN_PROSE, "t1")])
            provider = _bound(_provider(tmp, transport))
            prose = council.author_prose_session(TASK, provider, tools=policy.tools, policy=policy)
            self.assertEqual(prose, GREEN_PROSE)
            self.assertEqual(calls, {"leak": 0, "ast": 0, "semantic": 0})
            # ... and the scan runs at review, before any critic is consulted.
            authored = TASK.model_copy(update={"solver_prompt": prose})

            class EmptyFindings:
                def complete(self, role, prompt):
                    return '{"findings": []}'

            council.run_council(authored, EmptyFindings())
            self.assertEqual(calls["leak"], 1)
        # A tool named after the scan is refused by the session itself.
        bad = SimpleNamespace(name="leak_findings", input_schema={}, cost=RG.ToolCost(),
                              permitted_roles=frozenset({AUTHOR}), run=lambda ctx, args: None)
        with self.assertRaises(ValueError):
            council.author_prose_session(
                TASK, SessionDouble([]), tools=(*policy.tools, bad), policy=policy
            )

    def test_contamination_precheck_runs_once_on_the_final_draft(self):
        """`contamination_precheck` runs harness-side exactly once, on the
        FINAL draft, after the terminal — never inside the loop — and its
        projection carries the level, the fatal bit and the kinds only."""
        seen: list[str] = []

        class Index:
            def scan_pre(self, task, *, require=None):
                seen.append(task.solver_prompt)
                coverage = SimpleNamespace(level=SimpleNamespace(value="name_only"))
                collision = SimpleNamespace(kind="family", against="eltbench", fatal=False,
                                            detail="family:secret-corpus-name 0xdeadbeef")
                return SimpleNamespace(call_point="pre", coverage=coverage, collisions=(collision,))

        policy = _policy()
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([_submit(RECIPE_PROSE, "t0"), _submit(GREEN_PROSE, "t1")])
            provider = _bound(_provider(tmp, transport))
            record: dict = {}
            council.author_prose_session(
                TASK, provider, tools=policy.tools, policy=policy,
                contamination_index=Index(), record=record,
            )
        self.assertEqual(seen, [GREEN_PROSE])
        precheck = record["precheck"]
        self.assertIsInstance(precheck, PJ.Diagnostic)
        self.assertEqual((precheck.source, precheck.code, precheck.ok), (PJ.DiagnosticSource.GATE, "ok", True))
        self.assertEqual(precheck.subject, "contamination-clean")
        self.assertEqual(dict(precheck.flags), {"unarmed": False, "name_only": True, "armed": False, "fatal": False, "kind_family": True})
        rendered = precheck.render()
        self.assertNotIn("eltbench", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("deadbeef", rendered)
        # The precheck is not a validator turn of the loop (two check_prose runs only).
        self.assertEqual(record["result"].validator_run_count, 2)
        # No index handle: the fail-closed reading of an unarmed index.
        closed = V.project_contamination_precheck(None)
        self.assertEqual((closed.ok, closed.code), (False, "failed"))
        self.assertTrue(closed.flags["unarmed"] and closed.flags["fatal"] and closed.flags["kind_index"])

    def test_author_abort_fails_the_stage_on_the_empty_output_route(self):
        """`abort(reason_code)` is a legitimate stop: the session returns
        ABSTAINED and the stage fails on today's empty-output route (a
        `ValueError`, never an infrastructure halt)."""
        policy = _policy()
        provider = SessionDouble([[tool_use("abort", {"reason_code": "infeasible"}, "a")]])
        with self.assertRaises(ValueError) as caught:
            council.author_prose_session(TASK, provider, tools=policy.tools, policy=policy)
        self.assertIn("abstained", str(caught.exception))
        self.assertIn("infeasible", str(caught.exception))
        self.assertEqual(engine_mod._infra_marker_for(caught.exception), "")

    def test_protocol_says_the_rule_line_annotations_are_part_of_the_rule(self):
        """Batch 2026-09-10: drafts stated a join rule's sentence and dropped
        its trailing `join preservation: left`, and the checker (which folds
        that structured field into the rule's required terms) answered "every
        passage naming it never states ['left']". The view shows it and the
        operator rule allows 'left-sided' / 'left preservation', so the
        requirement was satisfiable and merely unstated."""
        from elt_taskgen.review import declarative_prose

        text = prompts.SEMANTIC_AUTHOR_SESSION_PROTOCOL
        lowered = text.lower()
        self.assertIn("the trailing annotations on a rule line are part of the rule", lowered)
        self.assertIn("join preservation", lowered)
        self.assertIn("preservation is left-sided", lowered)
        self.assertIn("never 'left join'", lowered)
        # The phrasings the protocol recommends must themselves be legal.
        identifiers = declarative_prose._task_identifiers(TASK)
        for phrase in ("preservation is left-sided", "left preservation"):
            with self.subTest(phrase=phrase):
                self.assertFalse(declarative_prose.operator_problems(phrase, identifiers))
        self.assertTrue(declarative_prose.operator_problems("the left join of orders", identifiers))

    def test_abort_about_the_draft_keeps_the_draft_abort_about_the_task_stops(self):
        """Rerun 2026-09-10: 10 of 52 author turns aborted `cannot_repair` /
        `insufficient_information` on a RED CHECK after writing a complete
        draft, and the whole draft was discarded to human adjudication. Those
        two codes describe the draft, so a session that already submitted one
        keeps it and the stage's own gate decides; the three codes that make a
        claim about the VIEW still stop the session, draft or no draft."""
        policy = _policy()
        for code in ("cannot_repair", "insufficient_information"):
            with self.subTest(kept=code):
                provider = SessionDouble([
                    [tool_use("submit_prose", {"text": RECIPE_PROSE}, "a")],
                    [tool_use("abort", {"reason_code": code}, "b")],
                ])
                record: dict = {}
                prose = council.author_prose_session(
                    TASK, provider, tools=policy.tools, policy=policy, record=record)
                self.assertEqual(prose, RECIPE_PROSE)
                self.assertIs(record["result"].terminal, S.TerminalState.ABSTAINED)
        for code in ("infeasible", "spec_conflict", "out_of_scope"):
            with self.subTest(stops=code):
                provider = SessionDouble([
                    [tool_use("submit_prose", {"text": RECIPE_PROSE}, "a")],
                    [tool_use("abort", {"reason_code": code}, "b")],
                ])
                with self.assertRaises(ValueError) as caught:
                    council.author_prose_session(
                        TASK, provider, tools=policy.tools, policy=policy)
                self.assertIn(code, str(caught.exception))
        # With NO draft at all, a draft-shaped code still stops the session.
        provider = SessionDouble([[tool_use("abort", {"reason_code": "cannot_repair"}, "a")]])
        with self.assertRaises(ValueError) as caught:
            council.author_prose_session(TASK, provider, tools=policy.tools, policy=policy)
        self.assertIn("cannot_repair", str(caught.exception))

    def test_author_limit_stop_returns_the_last_draft_for_the_gate(self):
        """A limit stop with no validator-green draft hands the LAST submitted
        draft to the author stage's unchanged fidelity gate, which fails it
        to SPECIFICATION exactly as a red one-shot draft — never a silent
        pass, never an infrastructure halt."""
        policy = _policy(max_turns=1)  # one turn: the revision the block allows cannot fit
        provider = SessionDouble([[tool_use("submit_prose", {"text": RECIPE_PROSE}, "a")]])
        record: dict = {}
        prose = council.author_prose_session(
            TASK, provider, tools=policy.tools, policy=policy, record=record
        )
        self.assertEqual(prose, RECIPE_PROSE)
        self.assertIs(record["result"].terminal, S.TerminalState.LIMIT_TURNS)
        self.assertIsNone(record["result"].final)
        self.assertEqual(record["terminal"], "LIMIT_TURNS")
        # Nothing submitted at all and the limit binding: NOT the empty-output
        # route — the typed limit-stop signal (a harness-imposed cap is not a
        # task defect; the stage waits on a salted re-run instead).
        empty = SessionDouble([[tool_use("submit_prose", {"text": ""}, "a")]] * 3)
        with self.assertRaises(council.AuthorSessionLimitStop) as caught:
            council.author_prose_session(TASK, empty, tools=policy.tools, policy=policy)
        self.assertEqual((caught.exception.terminal, caught.exception.limit), ("LIMIT_TURNS", "turns"))
        self.assertNotIsInstance(caught.exception, ValueError)
        self.assertEqual(engine_mod._infra_marker_for(caught.exception), "")

    def test_author_limit_stop_without_draft_is_blocked_with_a_salt_never_a_round(self):
        """A limit stop with NO draft raises `AuthorSessionLimitStop` (the
        terminal name, the limit kind, the partial result, the record), and
        `author_session_limit_outcome` maps it exactly as the engine maps a
        limit-stopped proposer session: BLOCKED with `blocked_on =
        session_limit:<kind>` and the next salt while re-runs remain; at the
        bound, `wall` halts as transient infrastructure and every
        agent-attributable kind takes today's empty-output route, once. The
        salt folds a fixed re-run marker into the view so the re-run keys
        its own transcript; salt 0 is byte-identical to the unsalted view."""
        policy = _policy(max_turns=1)
        provider = SessionDouble([[tool_use("submit_prose", {"text": ""}, "a")]] * 3)
        with self.assertRaises(council.AuthorSessionLimitStop) as caught:
            council.author_prose_session(TASK, provider, tools=policy.tools, policy=policy, session_salt=2)
        exc = caught.exception
        self.assertEqual((exc.terminal, exc.limit, exc.session_salt), ("LIMIT_TURNS", "turns", 2))
        self.assertIs(exc.session_result.terminal, S.TerminalState.LIMIT_TURNS)
        self.assertEqual(exc.session_result.blocked_on, "session_limit:turns")
        self.assertIsNone(exc.session_result.final)
        self.assertEqual(sorted(exc.record), sorted(council.AUTHOR_SESSION_RECORD_KEYS))
        self.assertEqual(exc.record["terminal"], "LIMIT_TURNS")
        self.assertNotIn("SQL", str(exc))
        # The salted view carries the fixed marker after the unchanged protocol.
        view = provider.sessions[0]["view"]
        unsalted = prompts.semantic_author_session_view(council.render_view(CouncilRole.SEMANTIC_AUTHOR, TASK), max_revisions=1)
        self.assertTrue(view.startswith(unsalted))
        self.assertIn("=== RE-RUN 2 ===", view)
        self.assertEqual(prompts.semantic_author_session_view("V", max_revisions=1, session_salt=0),
                         prompts.semantic_author_session_view("V", max_revisions=1))
        self.assertNotEqual(prompts.semantic_author_session_view("V", max_revisions=1, session_salt=1),
                            prompts.semantic_author_session_view("V", max_revisions=1, session_salt=2))
        # The policy's own salt is the default.
        salted = SessionDouble([[tool_use("submit_prose", {"text": ""}, "a")]] * 3)
        with self.assertRaises(council.AuthorSessionLimitStop):
            council.author_prose_session(TASK, salted, tools=policy.tools, policy=V.author_policy(policy.limits, session_salt=1))
        self.assertIn("=== RE-RUN 1 ===", salted.sessions[0]["view"])

        # The disposition, against a duck-typed engine reporting the re-runs
        # already on the ledger at this identity.
        def engine_with(reruns):
            return SimpleNamespace(session_limit_reruns=lambda task_id, stage: reruns)

        outcome = council.author_session_limit_outcome(exc, engine_with(0), TASK)
        self.assertEqual(outcome.verdict, engine_mod.VERDICT_BLOCKED)
        data = outcome.payload.data
        self.assertEqual(data[engine_mod.BLOCKED_ON_KEY], "session_limit:turns")
        self.assertEqual((data["session_salt"], data["rerun"], data["limit"], data["reruns_used"]), ("1", "1/2", "turns", "0"))
        self.assertEqual(data["session_terminal"], "LIMIT_TURNS")
        self.assertEqual(data["session_sha256"], exc.session_result.session_sha256)
        self.assertIsNone(outcome.route)
        self.assertEqual(council.author_session_limit_outcome(exc, engine_with(1), TASK).payload.data["session_salt"], "2")
        # An engine without the counter (a lightweight double) counts zero.
        self.assertEqual(council.author_session_limit_outcome(exc, SimpleNamespace(), TASK).payload.data["session_salt"], "1")
        # At the bound: the agent-attributable kind takes ONE ordinary round
        # on today's route, recorded as the fallback...
        fallback = council.author_session_limit_outcome(exc, engine_with(engine_mod.MAX_SESSION_LIMIT_RERUNS), TASK)
        self.assertEqual(fallback.verdict, engine_mod.VERDICT_FAIL)
        self.assertIs(fallback.route, RepairRoute.SPECIFICATION)
        self.assertEqual(fallback.payload.data[engine_mod.SESSION_LIMIT_FALLBACK_KEY], "turns")
        self.assertNotIn(engine_mod.BLOCKED_ON_KEY, fallback.payload.data)
        self.assertEqual(fallback.payload.infrastructure, "")
        # ...while a wall stop (a clock the agent did not author) halts as infrastructure.
        wall = council.AuthorSessionLimitStop("LIMIT_WALL", limit="wall", session_result=exc.session_result)
        halted = council.author_session_limit_outcome(wall, engine_with(engine_mod.MAX_SESSION_LIMIT_RERUNS), TASK)
        self.assertEqual(halted.verdict, engine_mod.VERDICT_FAIL)
        self.assertEqual(halted.payload.infrastructure, engine_mod.SESSION_WALL_HALT_MARKER)
        self.assertIsNone(halted.route)
        # The signal is never classified as infrastructure by name.
        self.assertEqual(engine_mod._infra_marker_for(exc), "")
        self.assertNotIn("AuthorSessionLimitStop", engine_mod._INFRA_EXCEPTION_NAMES)


# ---------------------------------------------------------------------------
# The author stage runner
# ---------------------------------------------------------------------------

class AuthorRunnerTest(_Case):
    def test_fidelity_red_draft_is_persistable_authored_candidate(self):
        """A failed author draft travels on the outcome and names its digest.

        The engine persists ``outcome.task`` before routing the failure, so the
        specification proposer edits the actual red prose instead of an empty
        ``solver_prompt``.
        """
        with _AuthorConfig(enabled=False), tempfile.TemporaryDirectory() as tmp:
            provider = OneShotDouble(RECIPE_PROSE)
            outcome = cli.make_author_runner(provider)(_engine(tmp), TASK)

        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
        self.assertIsNotNone(outcome.task)
        self.assertEqual(outcome.task.solver_prompt, RECIPE_PROSE)
        self.assertNotEqual(outcome.task.content_hash(), TASK.content_hash())
        self.assertEqual(
            outcome.payload.data[cli.AUTHOR_PROSE_SHA_KEY], _sha(RECIPE_PROSE)
        )
        self.assertEqual(
            outcome.payload.data[cli.AUTHOR_SOURCE_KEY],
            cli.AUTHOR_SOURCE_CANDIDATE,
        )

    def test_failed_candidate_survives_invalidation_and_adjudication_rows(self):
        """Administrative FAIL rows cannot hide or impersonate provenance.

        The newest rows model the repair-proposer adjudication and the
        specification invalidation that follow an authored failure. Even a
        lookalike digest on an unlabeled row is skipped; the earlier explicitly
        labeled candidate remains the repair baseline, so patched prose is
        preserved without another model call.
        """
        candidate = _report_row(
            cli.VERDICT_FAIL,
            {
                cli.AUTHOR_PROSE_SHA_KEY: _sha(RECIPE_PROSE),
                # The enabled-by-default bounded author keeps its more precise
                # session provenance on a red candidate.
                cli.AUTHOR_SOURCE_KEY: cli.AUTHOR_SOURCE_REVISED,
            },
            id_=1,
        )
        invalidation = _report_row(
            cli.VERDICT_FAIL,
            {"route": "specification", "reason": "invalidated by repair"},
            id_=2,
        )
        adjudication = _report_row(
            cli.VERDICT_FAIL,
            {
                "status": "needs_adjudication",
                # Deliberately resembles current provenance, but lacks the
                # author-only source label and therefore has no authority.
                cli.AUTHOR_PROSE_SHA_KEY: _sha(GREEN_PROSE),
            },
            id_=3,
        )
        patched = TASK.model_copy(update={"solver_prompt": GREEN_PROSE})
        provider = SessionDouble([])
        with _AuthorConfig(), tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp,
                rows={
                    ("author", "latest"): adjudication,
                    ("author", cli.VERDICT_FAIL): adjudication,
                },
                history={"author": (adjudication, invalidation, candidate)},
            )
            outcome = cli.make_author_runner(provider)(engine, patched)

        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
        self.assertEqual(
            outcome.payload.data[cli.AUTHOR_SOURCE_KEY],
            cli.AUTHOR_SOURCE_PRESERVED,
        )
        self.assertEqual(provider.sessions, [])
        self.assertEqual(provider.completes, 0)
        self.assertIsNone(outcome.task)

    def test_author_revision_respects_preserved_patch_rule(self):
        """The PRESERVED rule of the author stage runs BEFORE any provider is
        consulted: prose a certified SPECIFICATION patch committed is kept and
        gated, the session never starts, and once preserved it STAYS
        preserved while the bytes are unchanged."""
        patched = TASK.model_copy(update={"solver_prompt": GREEN_PROSE})
        with _AuthorConfig(), tempfile.TemporaryDirectory() as tmp:
            # (A) the prose moved since authoring last wrote it.
            rows = {("author", "latest"): _pass_row({cli.AUTHOR_PROSE_SHA_KEY: _sha("what the author wrote")})}
            provider = SessionDouble([])
            outcome = cli.make_author_runner(provider)(_engine(tmp, rows=rows), patched)
            self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
            self.assertEqual(outcome.payload.data[cli.AUTHOR_SOURCE_KEY], cli.AUTHOR_SOURCE_PRESERVED)
            self.assertNotIn(cli.AUTHOR_REVISIONS_KEY, outcome.payload.data)
            self.assertEqual(provider.sessions, [])
            self.assertEqual(provider.completes, 0)
            self.assertIsNone(outcome.task)
            # (A, sticky) the recorded sha matches but the source is PRESERVED.
            rows = {("author", "latest"): _pass_row({
                cli.AUTHOR_PROSE_SHA_KEY: _sha(GREEN_PROSE),
                cli.AUTHOR_SOURCE_KEY: cli.AUTHOR_SOURCE_PRESERVED,
            })}
            outcome = cli.make_author_runner(provider)(_engine(tmp, rows=rows), patched)
            self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
            self.assertEqual(outcome.payload.data[cli.AUTHOR_SOURCE_KEY], cli.AUTHOR_SOURCE_PRESERVED)
            self.assertEqual(provider.sessions, [])
            # A repaired prose that fails the gate still fails without a session.
            broken = TASK.model_copy(update={"solver_prompt": RECIPE_PROSE})
            rows = {("author", "latest"): _pass_row({cli.AUTHOR_PROSE_SHA_KEY: _sha("older")})}
            outcome = cli.make_author_runner(provider)(_engine(tmp, rows=rows), broken)
            self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
            self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
            self.assertEqual(provider.sessions, [])

    def test_author_revision_count_recorded_in_ledger_source(self):
        """A session-authored PASS row says where the prose came from:
        `source: authored_revised` with the revision count, the drafts
        checked, the terminal and the session digest; the session record is
        persisted beside the task's reports."""
        with _AuthorConfig(), tempfile.TemporaryDirectory() as tmp:
            provider = SessionDouble([
                [tool_use("submit_prose", {"text": RECIPE_PROSE}, "a")],
                [tool_use("submit_prose", {"text": GREEN_PROSE}, "b")],
            ])
            outcome = cli.make_author_runner(provider)(_engine(tmp), TASK)
            self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
            self.assertIsNotNone(outcome.task)
            self.assertEqual(outcome.task.solver_prompt, GREEN_PROSE)
            data = outcome.payload.data
            self.assertEqual(data[cli.AUTHOR_SOURCE_KEY], cli.AUTHOR_SOURCE_REVISED)
            self.assertEqual(data[cli.AUTHOR_SOURCE_KEY], "authored_revised")
            self.assertEqual(data[cli.AUTHOR_REVISIONS_KEY], "1")
            self.assertEqual(data[cli.AUTHOR_DRAFTS_KEY], "2")
            self.assertEqual(data[cli.AUTHOR_SESSION_TERMINAL_KEY], "SUBMITTED")
            self.assertEqual(data[cli.AUTHOR_PROSE_SHA_KEY], _sha(GREEN_PROSE))
            self.assertRegex(data[cli.AUTHOR_SESSION_SHA_KEY], _HEX64)
            self.assertEqual(provider.completes, 0)
            self.assertEqual(len(provider.sessions), 1)
            self.assertEqual(provider.sessions[0]["role"], AUTHOR)
            # The session's first user message is the author view plus the protocol.
            view = provider.sessions[0]["view"]
            self.assertTrue(view.startswith(council.render_view(CouncilRole.SEMANTIC_AUTHOR, TASK)))
            self.assertIn("=== REVISION SESSION ===", view)
            self.assertIn("1 revision(s)", view)
            sessions_dir = Path(tmp) / "tasks" / TASK.task_id / "reports" / "sessions"
            files = sorted(sessions_dir.glob("semantic_author.*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["revisions"], 1)
            self.assertEqual(saved["session"]["terminal_name"], "SUBMITTED")
            self.assertEqual(saved["session"]["session_sha256"], data[cli.AUTHOR_SESSION_SHA_KEY])
            self.assertEqual(saved["contamination_precheck"]["subject"], "contamination-clean")
            # A green first draft is still a session row, with zero revisions.
            provider = SessionDouble([[tool_use("submit_prose", {"text": GREEN_PROSE}, "a")]])
            outcome = cli.make_author_runner(provider)(_engine(tmp), TASK)
            self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
            self.assertEqual(outcome.payload.data[cli.AUTHOR_SOURCE_KEY], "authored_revised")
            self.assertEqual(outcome.payload.data[cli.AUTHOR_REVISIONS_KEY], "0")
            self.assertEqual(outcome.payload.data[cli.AUTHOR_DRAFTS_KEY], "1")
            # A red final draft (revisions spent) fails to SPECIFICATION and the
            # FAIL row carries the same provenance.
            provider = SessionDouble([
                [tool_use("submit_prose", {"text": INCOMPLETE_PROSE}, "a")],
                [tool_use("submit_prose", {"text": RECIPE_PROSE}, "b")],
            ])
            outcome = cli.make_author_runner(provider)(_engine(tmp), TASK)
            self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
            self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
            self.assertIn("SQL mechanics", outcome.payload.error)
            self.assertIsNotNone(outcome.task)
            self.assertEqual(outcome.task.solver_prompt, RECIPE_PROSE)
            self.assertEqual(outcome.payload.data[cli.AUTHOR_SOURCE_KEY], "authored_revised")
            self.assertEqual(
                outcome.payload.data[cli.AUTHOR_PROSE_SHA_KEY], _sha(RECIPE_PROSE)
            )
            self.assertEqual(outcome.payload.data[cli.AUTHOR_REVISIONS_KEY], "1")

    def test_author_session_disabled_is_byte_identical_to_author_prose(self):
        """Both explicit rollback keys call the one-shot `author_prose`: the
        same prompt on the wire (the author view, no protocol), no wire tool,
        the same prose plus the current author-behavior binding in ledger
        data, and `run_session` is never touched.  The shipped configuration
        keeps the bounded session on."""
        expected_prompt = council.render_view(CouncilRole.SEMANTIC_AUTHOR, TASK)
        self.assertIsNotNone(cli._author_session_block(OneShotDouble(GREEN_PROSE)))
        self.assertTrue(P.role_is_agentic(AUTHOR))
        self.assertEqual([tool["name"] for tool in P.wire_tools_for(AUTHOR)], ["abort", "submit_prose"])
        for label, config in (("max_revisions 0", {"max_revisions": 0}),
                              ("enabled false", {"enabled": False})):
            with self.subTest(config=label), tempfile.TemporaryDirectory() as tmp:
                ctx = _AuthorConfig(**config)
                ctx.__enter__()
                try:
                    self.assertIsNone(cli._author_session_block(OneShotDouble(GREEN_PROSE)))
                    self.assertEqual(P.wire_tools_for(AUTHOR), [])
                    self.assertEqual(RG.ToolRegistry.for_role(AUTHOR).names, ())
                    self.assertFalse(P.role_is_agentic(AUTHOR))
                    self.assertEqual(P.session_policy_for(AUTHOR).tools, ())
                    provider = OneShotDouble(GREEN_PROSE)
                    engine = _engine(tmp)
                    outcome = cli.make_author_runner(provider)(engine, TASK)
                    self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
                    self.assertEqual(provider.sessions, 0)
                    self.assertEqual(provider.prompts, [(CouncilRole.SEMANTIC_AUTHOR, expected_prompt)])
                    self.assertEqual(outcome.task.solver_prompt, council.author_prose(TASK, OneShotDouble(GREEN_PROSE)))
                    _, admission = cli._admission_gate(provider, engine.workspace)
                    self.assertEqual(
                        outcome.payload.data,
                        {
                            cli.AUTHOR_PROSE_SHA_KEY: _sha(GREEN_PROSE),
                            cli.AUTHOR_BEHAVIOR_SHA_KEY: P.role_behavior_sha256(AUTHOR),
                            **admission,
                        },
                    )
                    self.assertEqual(
                        outcome.payload.detail,
                        f"solver prose authored ({len(GREEN_PROSE)} chars); fidelity "
                        "gate green; content hash moves, all stages re-attest",
                    )
                    self.assertFalse((Path(tmp) / "tasks" / TASK.task_id / "reports").exists())
                finally:
                    ctx.__exit__(None, None, None)
        # The two rollback keys are one rule.
        self.assertTrue(V.author_session_enabled(ENABLED_BLOCK))
        self.assertFalse(V.author_session_enabled({**ENABLED_BLOCK, "max_revisions": 0}))
        self.assertFalse(V.author_session_enabled({**ENABLED_BLOCK, "enabled": False}))
        self.assertFalse(V.author_session_enabled(None))

    def test_author_manifest_folds_into_role_behavior_only(self):
        """R-B: enabling the author's session moves
        `role_behavior_sha256("semantic_author")` (its tools, policy and
        limits) and puts its two terminals — never its harness validators —
        on the wire, while the admission fingerprint, which iterates the
        critic seats, is byte-identical."""
        routing = P.load_role_routing(None)
        with _AuthorConfig(enabled=False):
            disabled_sha = P.role_behavior_sha256(AUTHOR)
            disabled_fingerprint = metrology.council_routing_fingerprint(routing)
            self.assertFalse(P.role_is_agentic(AUTHOR))
        with _AuthorConfig():
            self.assertEqual(P.wire_tools_for(AUTHOR) and [t["name"] for t in P.wire_tools_for(AUTHOR)], ["abort", "submit_prose"])
            self.assertEqual(set(RG.ToolRegistry.for_role(AUTHOR).names), set(V.AUTHOR_TOOL_NAMES))
            self.assertTrue(P.role_is_agentic(AUTHOR))
            self.assertNotEqual(P.role_behavior_sha256(AUTHOR), disabled_sha)
            self.assertEqual(metrology.council_routing_fingerprint(routing), disabled_fingerprint)
            manifest = P.role_behavior_manifest(AUTHOR)
            self.assertEqual([t["name"] for t in manifest["tools"]], ["abort", "submit_prose"])
            self.assertEqual(manifest["loop_limits"]["max_revisions"], 1)
        self.assertNotEqual(P.role_behavior_sha256(AUTHOR), disabled_sha)


# ---------------------------------------------------------------------------
# The tools and their projections
# ---------------------------------------------------------------------------

class AuthorToolsTest(_Case):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session = V.AuthorSession(task=TASK, max_revisions=1)
        self.ctx = self.session.context(Path(self._tmp.name))

    def dispatch(self, name: str, **args):
        tool = V.author_tool(name)
        return RG.ToolRegistry(AUTHOR, [tool]).dispatch(self.ctx, name, args)

    def assert_clean(self, diag):
        payload = PJ.serialize_for_transport(diag, task=TASK)
        PJ.assert_value_free(payload.encode("utf-8"), task=TASK)
        self.assertFalse(re.search(r"\d", diag.render()), diag.render())

    def test_check_structure_is_not_in_the_author_manifest(self):
        """SoT T3 decided cell 7: `check_structure` is a proposer tool only —
        not an author tool, not in the author's manifest or policy, and never
        executed by the author's `check_prose`; the author's five are exactly
        the matrix's AUT column, with the two terminals alone on the wire."""
        self.assertEqual(
            V.AUTHOR_TOOL_NAMES,
            ("replace_prose", "check_prose", "contamination_precheck", "submit_prose", "abort"),
        )
        self.assertEqual(V.AUTHOR_WIRE_TOOL_NAMES, ("submit_prose", "abort"))
        self.assertNotIn("check_structure", V.AUTHOR_TOOL_NAMES)
        self.assertNotIn("leak_findings", V.AUTHOR_TOOL_NAMES)
        policy = V.author_policy()
        manifest = json.dumps(policy.as_manifest())
        self.assertNotIn("check_structure", manifest)
        self.assertEqual(policy.allowlist, tuple(sorted(V.AUTHOR_TOOL_NAMES)))
        self.assertEqual([t["name"] for t in policy.wire_tools], ["abort", "submit_prose"])
        self.assertEqual(policy.terminal_tool_names, ("submit_prose", "abort"))
        self.assertEqual(policy.mode, "harness_driven")
        self.assertEqual(
            RG.ToolRegistry.declared_for_role(AUTHOR).names, tuple(sorted(V.AUTHOR_TOOL_NAMES))
        )
        self.assertEqual([t["name"] for t in V.author_registry().wire_tools()], ["abort", "submit_prose"])
        for tool in V.AUTHOR_TOOLS:
            self.assertIsInstance(tool, RG.Tool)
            self.assertEqual(tool.permitted_roles, frozenset({AUTHOR}))
            self.assertTrue(tool.description)
        for name in ("replace_prose", "check_prose", "contamination_precheck"):
            self.assertTrue(getattr(V.author_tool(name), "harness_only", False), name)
        for name in ("submit_prose", "abort"):
            self.assertTrue(getattr(V.author_tool(name), "terminal", False), name)
            self.assertFalse(getattr(V.author_tool(name), "harness_only", False), name)
        self.assertTrue(V.author_tool("check_prose").validator)
        self.assertTrue(V.author_tool("contamination_precheck").validator)
        self.assertTrue(V.author_tool("replace_prose").surface_write)
        # The structural gate is never executed by the author's check.
        with mock.patch.object(
            structural_completeness, "check_structural_completeness",
            side_effect=AssertionError("check_structure ran inside the author loop"),
        ):
            diag = self.dispatch("check_prose", text=GREEN_PROSE)
        self.assertTrue(diag.ok)
        # The proposer keeps it (inside check_cheap).
        self.assertIn("check_cheap", V.PROPOSER_TOOL_NAMES)
        self.assertIn("check_structure", V.CheckCheapTool.description)

    def test_prose_projection_calls_check_prose_fidelity_once_and_emits_codes(self):
        """`check_prose` is ONE call to `check_prose_fidelity` — which itself
        calls `check_declarative_prose` once, so the declarative half is never
        run a second time — projected through `project_prose_problems` to
        codes only: the cheap-gate code, `prose_ok`, one `prose_<kind>` flag
        per kind, and implicated identifiers independently recovered from the
        public TaskIR/MartSpec; no sentence, number, private SQL or value."""
        counts = {"fidelity": 0, "declarative": 0, "projection": 0}
        real_fidelity = prose_fidelity.check_prose_fidelity
        real_declarative = declarative_prose.check_declarative_prose
        real_projection = PJ.project_prose_problems

        def fidelity(task):
            counts["fidelity"] += 1
            return real_fidelity(task)

        def declarative(task):
            counts["declarative"] += 1
            return real_declarative(task)

        def projection(problems, *, task):
            counts["projection"] += 1
            return real_projection(problems, task=task)

        with mock.patch.object(prose_fidelity, "check_prose_fidelity", fidelity), \
                mock.patch.object(declarative_prose, "check_declarative_prose", declarative), \
                mock.patch.object(PJ, "project_prose_problems", projection):
            red = self.dispatch("check_prose", text=RECIPE_PROSE)
        self.assertEqual(counts, {"fidelity": 1, "declarative": 1, "projection": 1})
        self.assertIsInstance(red, PJ.Diagnostic)
        self.assertEqual((red.source, red.ok, red.code), (PJ.DiagnosticSource.CHEAP, False, "prose_problems"))
        self.assertFalse(red.flags["prose_ok"])
        self.assertTrue(red.flags["prose_operator_vocabulary"])
        self.assertTrue(all(key.startswith("prose_") for key in red.flags))
        self.assertTrue(set(red.flags) - {"prose_ok"} <= _prose_flag_vocabulary(TASK))
        # The locus flags (D9): the item kind, the operator category and the
        # mart the fragment sits in — codes, never the fragment.
        self.assertTrue(red.flags["prose_item_operator"])
        self.assertTrue(any(key.startswith("prose_operator_") for key in red.flags))
        self.assertIn("order_id", red.names)
        self.assert_clean(red)
        # The completeness half names the public rule inputs too, without
        # forwarding the rule sentence or consulting the private reference.
        missing = self.dispatch("check_prose", text=INCOMPLETE_PROSE)
        self.assertEqual(missing.code, "prose_problems")
        self.assertTrue(missing.flags["prose_not_represented"])
        self.assertIn(demo_fixture.MART_NAME, missing.names)
        self.assertIn("orders", missing.names)
        self.assertIn("order_id", missing.names)
        public = PJ.PublicIdentifierSet(TASK)
        self.assertTrue(all(name in public for name in missing.names))
        self.assertTrue(set(missing.flags) - {"prose_ok"} <= _prose_flag_vocabulary(TASK))
        # One mart implicated: it is the subject, its section is missing, and
        # every item kind of the skeletal draft is named — per mart too.
        self.assertEqual(missing.subject, demo_fixture.MART_NAME)
        mart_key = V._flag_key(V.PROSE_MART_FLAG_PREFIX, demo_fixture.MART_NAME)
        for item in ("source_backend", "relationship", "mart_section", "description",
                     "grain", "key_column", "output_column", "rule"):
            self.assertTrue(missing.flags.get(f"prose_item_{item}"), item)
        for item in ("mart_section", "description", "grain", "key_column", "output_column", "rule"):
            self.assertTrue(missing.flags.get(f"{mart_key}_{item}"), item)
        self.assertTrue(missing.flags.get(f"{mart_key}_rule_join"))
        self.assertTrue(missing.flags.get(f"{mart_key}_rule_aggregate"))
        self.assertFalse(any("_rule_" in key and key.rsplit("_rule_", 1)[1] not in
                             {k.value for k in MartOpKind} for key in missing.flags))
        # names are grouped: the document-level source/relationship names
        # first, then the mart, then what its problems implicate.
        self.assertLess(missing.names.index("orders"), missing.names.index(demo_fixture.MART_NAME))
        self.assertLess(missing.names.index(demo_fixture.MART_NAME), missing.names.index("join"))
        self.assert_clean(missing)
        for sentence in prose_fidelity.check_prose_fidelity(
            TASK.model_copy(update={"solver_prompt": INCOMPLETE_PROSE})
        ):
            self.assertNotIn(sentence, missing.render())
        # Green: the check ran on task.model_copy(update={"solver_prompt": draft}).
        green = self.dispatch("check_prose", text=GREEN_PROSE)
        self.assertEqual((green.ok, green.code, dict(green.flags), green.names), (True, "cheap_green", {"prose_ok": True}, ()))
        self.assert_clean(green)
        self.assertEqual(self.session.drafts, [RECIPE_PROSE, INCOMPLETE_PROSE, GREEN_PROSE])
        self.assertEqual(self.session.green_draft, GREEN_PROSE)
        self.assertEqual(self.session.state_epoch, 3)
        self.assertEqual(self.session.task.solver_prompt, TASK.solver_prompt, "the task itself is never mutated")
        # Every author tool result passes the projector and the gatekeeper.
        for name, args in (
            ("replace_prose", {"text": GREEN_PROSE}),
            ("replace_prose", {"text": GREEN_PROSE}),  # unchanged: no epoch bump
            ("contamination_precheck", {"text": GREEN_PROSE}),
            ("submit_prose", {"text": GREEN_PROSE}),
            ("abort", {"reason_code": "spec_conflict"}),
        ):
            with self.subTest(tool=name):
                diag = self.dispatch(name, **args)
                self.assertIsInstance(diag, PJ.Diagnostic)
                self.assert_clean(diag)
        self.assertEqual(self.session.state_epoch, 3)
        self.assertEqual(self.session.abort_reason, "spec_conflict")

    def test_no_author_tool_accepts_a_population_or_path_argument(self):
        """The population is a constant of the tool context; every argument is
        a bounded string (the prose text or the abort reason enum), never a
        path, URL or file name."""
        banned = {"path", "url", "uri", "file", "filename", "href", "dir", "directory", "relative_path"}
        self.assertEqual(V.AuthorToolContext.population, "development")
        for tool in V.AUTHOR_TOOLS:
            schema = dict(tool.input_schema)
            self.assertIs(schema.get("additionalProperties"), False, tool.name)
            self.assertNotIn(S.FORBIDDEN_ARG_NAME, schema.get("properties", {}), tool.name)
            self.assertFalse(banned & set(schema.get("properties", {})), tool.name)
            for prop, spec in schema.get("properties", {}).items():
                self.assertEqual(spec.get("type"), "string", f"{tool.name}.{prop}")
                self.assertTrue("enum" in spec or "maxLength" in spec, f"{tool.name}.{prop}")
        V.author_policy()  # constructs: no tool declares a population
        self.assertIsNone(S.validate_args(V.author_tool("submit_prose").input_schema, {"text": GREEN_PROSE}))
        self.assertIsNotNone(S.validate_args(V.author_tool("submit_prose").input_schema, {"text": ""}))
        self.assertIsNotNone(S.validate_args(V.author_tool("submit_prose").input_schema, {"text": "x", "population": "primary"}))
        self.assertEqual(S._forbidden_argument({"text": GREEN_PROSE}), "")

    def test_session_protocol_states_the_real_adjacency_rule(self):
        """The session's first message states the mechanical rule of
        review/declarative_prose.py — the bare join / distinct / union words
        are flagged only with a task identifier within `_ADJACENCY + 1`
        words, with two closed public noun-phrase exemptions for distinct —
        and the system prompt is left byte-identical (the session view is the
        author view plus the protocol)."""
        self.assertEqual(declarative_prose._ADJACENCY, 2)
        protocol = prompts.SEMANTIC_AUTHOR_SESSION_PROTOCOL
        self.assertIn("within the next three words", protocol)
        self.assertIn("at most two other words in between", protocol)
        self.assertIn("exactly two", protocol)
        self.assertIn("distinct_status_count", protocol)
        self.assertIn("TaskIR/MartSpec", protocol)
        self.assertIn("never private SQL", protocol)
        self.assertIn("review/declarative_prose.py", protocol)
        self.assertIn("submit_prose", protocol)
        self.assertIn("abort", protocol)
        for code in S.ABORT_REASON_CODES:
            self.assertIn(code, protocol)
        for kind in PJ.PROSE_KINDS:
            self.assertIn(f"prose_{kind}", protocol)
        self.assertIn("cheap_green", protocol)
        self.assertIn("prose_problems", protocol)
        self.assertNotIn("REVISION SESSION", prompts.ROLE_SYSTEM[AUTHOR])
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, TASK)
        session_view = prompts.semantic_author_session_view(view, max_revisions=2)
        self.assertTrue(session_view.startswith(view))
        self.assertIn("2 revision(s)", session_view)
        self.assertNotIn("{revisions}", session_view)
        # The rule as stated agrees with the code it describes.
        identifiers = declarative_prose._task_identifiers(TASK)
        self.assertTrue(declarative_prose.operator_problems("joined to customers", identifiers))
        self.assertTrue(declarative_prose.operator_problems("distinct order_id", identifiers))
        # The two declarative-count heads are the SAME exemption: the public
        # column descriptions write "how many distinct <identifier> values",
        # and flagging only that head left no wording that satisfied both the
        # restate-the-requirement rule and the operator rule (rerun 2026-09-10).
        for phrase in ("the number of distinct order_id values",
                       "how many distinct order_id values occur among them"):
            self.assertFalse(declarative_prose.operator_problems(phrase, identifiers), phrase)
        for phrase in ("count distinct order_id", "select distinct order_id"):
            self.assertTrue(declarative_prose.operator_problems(phrase, identifiers), phrase)
        self.assertTrue(declarative_prose.operator_problems("count distinct order_id", identifiers))
        self.assertEqual(declarative_prose.operator_problems("joined the loyalty program", identifiers), [])
        self.assertEqual(declarative_prose.operator_problems("each distinct kind of shipment", identifiers), [])
        self.assertEqual(
            declarative_prose.operator_problems(
                "the number of distinct order_id values", identifiers
            ),
            [],
        )
        self.assertEqual(
            declarative_prose.operator_problems(
                "distinct status count",
                identifiers | frozenset({"distinct_status_count"}),
            ),
            [],
        )



# Regression for the ten recorded D9 prose-fidelity failures. The scripted
# author repairs flagged items using only diagnostics and its public view.

BATCH_DIR = Path(__file__).resolve().parents[1] / "runs" / "authorized_batch_50_20260908" / "workspace-final"

#: (batch ledger report id, task id) of the ten D9 failures.
D9_TASKS: tuple[tuple[int, str], ...] = (
    (129, "dlt__pipedrive"),
    (177, "synsql__3d_coordinate_system_for_spatial_data_management__users_components_rollup"),
    (203, "synsql__3d_graphics_material_properties_and_rendering__rendering_settings_rendering_logs_rollup"),
    (209, "synsql__3d_model_and_texture_assets_management__types_assets_rollup"),
    (215, "synsql__3d_motion_tracking_and_analysis__sensors_motion_data_cohorts"),
    (233, "synsql__3d_object_pose_estimation_and_tracking__users_access_logs_rollup"),
    (239, "synsql__3d_object_positioning_and_animation_data__users_user_actions_rollup"),
    (275, "synsql__3d_spatial_data_monitoring_and_analysis__drones_flight_logs_cohorts"),
    (281, "synsql__529_college_savings_program_rankings_and_information__plans_plan_expenses_cohorts"),
    (318, "synsql__covid_19_pandemic_data_tracking_and_analysis__countries_vaccination_data_cohorts"),
)


def _d9_task(task_id: str):
    from elt_taskgen.models import task_from_json

    return task_from_json((BATCH_DIR / "tasks" / task_id / "task_ir.json").read_text(encoding="utf-8"))


def _d9_drafts(task_id: str) -> dict[int, tuple[str, str | None]]:
    """turn_index -> (the `submit_prose.text` the model sent, the tool_result
    it had just been shown) from the batch's semantic_author transcripts."""
    out: dict[int, tuple[str, str | None]] = {}
    for path in sorted((BATCH_DIR / "transcripts" / "semantic_author").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("task_id") != task_id:
            continue
        turn = record.get("turn") or {}
        text = None
        for block in turn.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == V.AUTHOR_SUBMIT_TOOL:
                text = block["input"]["text"]
        seen = None
        for message in turn.get("user_messages") or []:
            for block in message.get("content", []):
                if block.get("type") == "tool_result":
                    seen = block.get("content")
        if text is not None:
            out[int(turn.get("turn_index", 0) or 0)] = (text, seen)
    return out


# -- the scripted author: a revision that follows the diagnostic ---------------

_D9_OP_KINDS = tuple(k.value for k in MartOpKind)


def _d9_one_sentence(text: str) -> str:
    """One sentence: inner terminators become ';' (the gate's passage is at
    most two consecutive sentences, so a rule is restated as exactly one)."""
    text = " ".join(str(text).split())
    return re.sub(r"[.!?]\s+", "; ", text).rstrip(".!? ")


def _d9_declarativize(sentence: str, identifiers: frozenset[str]) -> str:
    """The protocol's translation of operator words into outcome words; a
    sentence the scripted author cannot make declarative fails LOUDLY."""
    for pattern, replacement in (
        (r"\bcoalesc(?:e|es|ed|ing)\b", "the default"),
        (r"\bnullif\b", "null-guard"),
        (r"\bunion all\b", "all rows of both"),
        (r"\bgroup(?:ed)? by\b", "for each"),
        (r"\bcase when\b", "when"),
    ):
        sentence = re.sub(pattern, replacement, sentence, flags=re.I)
    for _ in range(4):
        problems = declarative_prose.operator_problems(sentence, identifiers)
        if not problems:
            return sentence
        if any("distinct" in p for p in problems):
            sentence = re.sub(r"\bdistinct\b", "unique", sentence, flags=re.I)
        elif any("join" in p for p in problems):
            sentence = re.sub(r"\bjoin(?:s|ed|ing)?\b", "matched", sentence, flags=re.I)
        elif any("union" in p for p in problems):
            sentence = re.sub(r"\bunions?\b", "combined rows", sentence, flags=re.I)
        else:
            break
    problems = declarative_prose.operator_problems(sentence, identifiers)
    if problems:
        raise AssertionError(f"scripted author cannot declarativize: {problems}")
    return sentence


def _d9_rule_sentence(mart_name: str, step: dict, identifiers: frozenset[str]) -> str:
    """One passage for one numbered rule, from the PUBLIC step of the view:
    the description, the source tables, every carried/output column, the
    join preservation as an outcome, the condition's public identifiers and
    literal values, the semantic parameters."""
    parts = [f"Rule {step['order']} of mart {mart_name}: {_d9_one_sentence(step['description'])}"]
    if step.get("source_inputs"):
        parts.append("it reads source table " + ", ".join(step["source_inputs"]))
    if step.get("carried_fields"):
        parts.append("the row carries " + ", ".join(step["carried_fields"]))
    if step.get("join_preservation") == "left":
        parts.append("every row of the preserved side is retained, including one with no matching row")
    condition = step.get("condition") or {}
    if condition.get("public_identifiers"):
        parts.append("its condition names " + ", ".join(condition["public_identifiers"]))
    if condition.get("literal_values"):
        parts.append("the literal values it tests are " + ", ".join(f"'{v}'" for v in condition["literal_values"]))
    for key, value in sorted((step.get("semantic_parameters") or {}).items()):
        parts.append(f"{key.replace('_', ' ')}: {_d9_one_sentence(value)}")
    return _d9_declarativize("; ".join(parts) + ".", identifiers)


def _d9_decode_flags(diag, task):
    """(document-level items, per-mart items, per-mart rule kinds) read off
    the Diagnostic's flags; a mart named under `names` with no mart flag (a
    digit-bearing name) is marked '*' for a whole rewrite."""
    flags = {key for key, on in diag.flags.items() if on}
    doc_items = {
        item for item in prose_fidelity.PROSE_ITEM_KINDS
        if V._flag_key(V.PROSE_ITEM_FLAG_PREFIX, item) in flags
    }
    per_mart: dict[str, set[str]] = {}
    rule_kinds: dict[str, set[str]] = {}
    marts = sorted(task.marts, key=lambda m: -len(m.name))  # longest prefix first
    for key in sorted(flags):
        for mart in marts:
            prefix = V._flag_key(V.PROSE_MART_FLAG_PREFIX, mart.name) + "_"
            if key.startswith(prefix):
                tail = key[len(prefix):]
                if tail.startswith("rule_"):
                    rule_kinds.setdefault(mart.name, set()).add(tail[5:])
                else:
                    per_mart.setdefault(mart.name, set()).add(tail)
                break
    for mart in task.marts:
        if mart.name in diag.names and mart.name not in per_mart and mart.name not in rule_kinds:
            per_mart.setdefault(mart.name, set()).add("*")
    return doc_items, per_mart, rule_kinds


def _d9_section_lines(task, mart, *, items, rule_kinds, whole, identifiers, original):
    requirements = solver_safe_mart_requirements(task, mart)
    header = f"Mart {mart.name}: {_d9_declarativize(_d9_one_sentence(mart.description), identifiers)}."
    if whole:
        lines = [header]
        items = {"grain", "key_column", "output_column"}
        rule_kinds = set(_D9_OP_KINDS)
    else:
        lines = list(original) or [header]
        if items & {"description", "mart_name"}:
            lines[0] = header
    if "grain" in items:
        lines.append(f"Grain: {_d9_declarativize(_d9_one_sentence(mart.grain), identifiers)}.")
    if "key_column" in items:
        lines.append("Key columns: " + ", ".join(mart.key_columns) + ".")
    if items & {"output_column", "column_description"}:
        lines.append("Output columns:")
        for column in requirements["columns"]:
            lines.append(
                f"- {column['name']} ({column['type']}): "
                f"{_d9_declarativize(_d9_one_sentence(column['description']), identifiers)}."
            )
    if rule_kinds:
        lines.append("Rules restated:")
        for step in requirements["transformation"]["steps"]:
            if step["operation"] in rule_kinds:
                lines.append("- " + _d9_rule_sentence(mart.name, step, identifiers))
    lines.append("")
    return lines


def _d9_overview_lines(task, *, source: bool, relationship: bool) -> list[str]:
    lines: list[str] = []
    if source:
        lines.append("Source tables and their extraction backends:")
        for table in task.tables:
            backend = task.backend_for(table.name).backend.value
            lines.append(f"- {table.name} is extracted from the {backend} backend.")
        lines.append("")
    if relationship:
        lines.append("Relationships between source tables:")
        for rel in task.relationships:
            requirement = "required" if rel.required else "optional"
            lines.append(
                f"- relationship: each {rel.child_table} row's {', '.join(rel.child_columns)} "
                f"refers to the {rel.parent_table} row with that {', '.join(rel.parent_columns)}; "
                f"this relationship is {requirement}."
            )
        lines.append("")
    return lines


def _d9_guided_revision(draft: str, diag, task) -> str:
    """The scripted author's revision: reads ONLY the codes-only Diagnostic
    (flags, names) and the author's own public view data; keeps every passage
    the diagnostic did not flag; adds or rewrites the flagged items of the
    flagged marts, exactly as the session protocol instructs."""
    identifiers = declarative_prose._task_identifiers(task)
    doc_items, per_mart, rule_kinds = _d9_decode_flags(diag, task)
    lines = draft.splitlines()
    sections, _problems = prose_fidelity._mart_sections(task.marts, draft)
    starts: list[tuple[int, str]] = []
    for mart in task.marts:
        text = sections.get(mart.name)
        if text and text.splitlines()[0] in lines:
            starts.append((lines.index(text.splitlines()[0]), mart.name))
    starts.sort()
    spans = {
        name: (start, starts[position + 1][0] if position + 1 < len(starts) else len(lines))
        for position, (start, name) in enumerate(starts)
    }
    out = list(lines[: starts[0][0] if starts else len(lines)])
    if out and out[-1].strip():
        out.append("")
    out.extend(_d9_overview_lines(task, source="source_backend" in doc_items, relationship="relationship" in doc_items))
    for mart in task.marts:
        items = per_mart.get(mart.name, set())
        kinds = rule_kinds.get(mart.name, set())
        original = list(lines[spans[mart.name][0]:spans[mart.name][1]]) if mart.name in spans else []
        while original and not original[-1].strip():
            original.pop()
        if not (items or kinds):
            out.extend(original + [""])
            continue
        whole = bool({"*", "mart_section", "operator"} & items) or not original
        out.extend(_d9_section_lines(task, mart, items=items, rule_kinds=kinds, whole=whole,
                                     identifiers=identifiers, original=original))
    return "\n".join(out).rstrip() + "\n"


def _d9_session_provider(tmp: str, task, transport):
    provider = _provider(tmp, transport)
    provider.begin_task_evidence(task.task_id, task.content_hash())
    return provider


class AuthorDiagnosticLocusTest(_Case):
    """The D9 diagnostic grammar on synthetic drafts: item kinds, per-mart
    flags, operator categories, names order, and the value-free guard for a
    mart whose name carries a digit (never a tripwire, never weakened)."""

    def test_flags_name_the_mart_and_item_kind_and_stay_value_free(self):
        task = TASK
        # A draft with the mart's section present but its key column line and
        # two rules (the dedupe of rule 2, the aggregate of rule 6) cut out.
        draft = "\n".join(
            line for line in GREEN_PROSE.splitlines()
            if not line.startswith("Key column:") and not line.startswith(("  2. ", "  6. "))
        )
        red = task.model_copy(update={"solver_prompt": draft})
        problems = prose_fidelity.check_prose_fidelity(red)
        self.assertTrue(problems)
        diag = V.project_prose_check(problems, task=red)
        self.assertEqual(diag.code, "prose_problems")
        self.assertEqual(diag.subject, demo_fixture.MART_NAME)
        loci = [prose_fidelity.problem_locus(p) for p in problems]
        self.assertTrue(all(l.item in prose_fidelity.PROSE_ITEM_KINDS and l.item != "other" for l in loci), loci)
        mart_key = V._flag_key(V.PROSE_MART_FLAG_PREFIX, demo_fixture.MART_NAME)
        for locus in loci:
            self.assertTrue(diag.flags.get(f"prose_item_{locus.item}"), locus)
            if locus.mart:
                self.assertTrue(diag.flags.get(f"{mart_key}_{locus.item}"), locus)
            if locus.item == "rule":
                self.assertIn(locus.op_kind, _D9_OP_KINDS)
                self.assertTrue(diag.flags.get(f"{mart_key}_rule_{locus.op_kind}"), locus)
                self.assertIn(locus.op_kind, diag.names)
        self.assertTrue(set(diag.flags) - {"prose_ok"} <= _prose_flag_vocabulary(task))
        rendered = diag.render()
        self.assertFalse(re.search(r"\d", rendered), rendered)
        for sentence in problems:
            self.assertNotIn(sentence, rendered)
        raw = PJ.serialize_for_transport(diag, task=red)
        PJ.assert_value_free(raw.encode("utf-8"), task=red)
        # Every locus of the gate's fixed templates parses to a named item.
        skeletal = task.model_copy(update={"solver_prompt": INCOMPLETE_PROSE})
        items = {prose_fidelity.problem_locus(p).item for p in prose_fidelity.check_prose_fidelity(skeletal)}
        self.assertEqual(items, {"source_backend", "relationship", "mart_section", "mart_name",
                                 "description", "grain", "key_column", "output_column", "rule"})
        for sentence in prose_fidelity.check_prose_fidelity(
            task.model_copy(update={"solver_prompt": RECIPE_PROSE})
        ):
            locus = prose_fidelity.problem_locus(sentence)
            if locus.item == "operator":
                self.assertTrue(locus.category)
        self.assertEqual(prose_fidelity.problem_locus("something new the gate may say").item, "other")
        with self.assertRaises(ValueError):
            prose_fidelity.ProblemLocus(item="not_a_kind")

    def test_digit_bearing_mart_name_never_reaches_the_numeric_canary(self):
        """A public mart name carrying a digit cannot be a flag key (a composed
        key is not a public identifier, so the canary would refuse the digit
        run): the mart's item flags stay unprefixed, the mart travels in
        `names` (and as the subject), and the projector + gatekeeper accept
        the bytes — a poorer pointer, never a tripwire, never a weakened
        detector."""
        mart = TASK.marts[0]
        renamed = mart.model_copy(update={"name": "customer_summary_v2"})
        task = TASK.model_copy(update={"marts": (renamed,), "solver_prompt": GREEN_PROSE})
        problems = prose_fidelity.check_prose_fidelity(task)
        self.assertTrue(any("customer_summary_v2" in p for p in problems), problems)
        diag = V.project_prose_check(problems, task=task)
        self.assertEqual(diag.subject, "customer_summary_v2")
        self.assertIn("customer_summary_v2", diag.names)
        self.assertFalse(any(key.startswith(V.PROSE_MART_FLAG_PREFIX) for key in diag.flags), diag.flags)
        self.assertTrue(diag.flags["prose_item_mart_section"])
        self.assertFalse(any(re.search(r"\d", key) for key in diag.flags), diag.flags)
        raw = PJ.serialize_for_transport(diag, task=task)
        PJ.assert_value_free(raw.encode("utf-8"), task=task)
        self.assertIsNone(V._mart_flag("customer_summary_v2", "grain"))
        self.assertEqual(V._mart_flag("customer_summary", "grain"), "prose_mart_customer_summary_grain")
        self.assertIsNone(V._mart_flag("x" * 130, "grain"))

    def test_shipped_author_block_allows_two_aimed_revisions(self):
        """The shipped `roles.semantic_author.session` block is the hard cap:
        two revisions (three turns, three validator runs, two compile
        corrections), a usd/wall budget that fits the third turn; the
        protocol states the diagnostic grammar and that every MartSpec item
        and every numbered rule must be represented; the system prompt says
        every numbered rule 1..N is walked."""
        limits = V.author_limits()
        self.assertEqual(
            (limits.max_revisions, limits.max_turns, limits.max_tool_calls, limits.max_compile_corrections),
            (4, 5, 5, 4),
        )
        self.assertEqual((limits.max_usd, limits.session_wall_seconds), (2.75, 900.0))
        caps = dict(limits.hard_caps)
        self.assertEqual((caps["revisions"], caps["turns"], caps["tool_calls"], caps["compile_corrections"]), (4, 5, 5, 4))
        self.assertLessEqual(limits.max_usd, caps["usd"])
        self.assertLessEqual(limits.session_wall_seconds, caps["wall_clock_s"])
        self.assertTrue(V.author_session_enabled(P.role_loop_limits(AUTHOR)))
        view = prompts.semantic_author_session_view("V", max_revisions=limits.max_revisions)
        self.assertIn("4 revision(s)", view)
        protocol = prompts.SEMANTIC_AUTHOR_SESSION_PROTOCOL
        for needle in (
            "prose_item_<item>", "prose_mart_<mart>_<item>", "prose_mart_<mart>_rule_<operation>",
            "prose_operator_<category>", "EVERY MARTSPEC ITEM AND EVERY NUMBERED RULE MUST BE REPRESENTED",
            "keep every passage the checker did not flag",
        ):
            self.assertIn(needle, protocol)
        for item in prose_fidelity.PROSE_ITEM_KINDS:
            if item != "other":
                self.assertIn(item, protocol)
        for kind in MartOpKind:
            self.assertIn(kind.value, protocol)
        self.assertIn("EVERY NUMBERED RULE MEANS EVERY NUMBERED RULE", prompts.ROLE_SYSTEM[AUTHOR])
        self.assertIn("1 through N", prompts.ROLE_SYSTEM[AUTHOR])


@unittest.skipUnless((BATCH_DIR / "tasks").is_dir(), "batch evidence runs/authorized_batch_50_20260908 not present")
class AuthorD9BatchReproductionTest(_Case):
    """The ten batch failures, from the on-disk evidence, against the REAL
    gate and the REAL session runner (no network, no live model)."""

    @classmethod
    def setUpClass(cls):
        cls.cases = []
        for report_id, task_id in D9_TASKS:
            drafts = _d9_drafts(task_id)
            if 0 not in drafts:
                raise unittest.SkipTest(f"semantic_author transcript for {task_id} not present")
            cls.cases.append((report_id, task_id, _d9_task(task_id), drafts))

    def test_recorded_drafts_are_red_and_the_recorded_diagnostic_was_blind(self):
        """REPRODUCTION. Every recorded draft (first and revised) is red under
        the real gate; the revision changed nothing the gate could see; and the
        tool_result the model was shown before revising named no item and, in
        a multi-mart task, at most the marts — nothing to aim at."""
        for report_id, task_id, task, drafts in self.cases:
            with self.subTest(report=report_id):
                first, _ = drafts[0]
                revised, seen = drafts[1]
                red0 = prose_fidelity.check_prose_fidelity(task.model_copy(update={"solver_prompt": first}))
                red1 = prose_fidelity.check_prose_fidelity(task.model_copy(update={"solver_prompt": revised}))
                self.assertTrue(red0, report_id)
                self.assertTrue(red1, report_id)
                self.assertGreaterEqual(len(red1), len(red0) - 5, (report_id, len(red0), len(red1)))
                self.assertIsNotNone(seen)
                self.assertTrue(seen.startswith("[cheap] prose_problems"), seen)
                self.assertNotIn("prose_item_", seen)
                self.assertNotIn("prose_mart_", seen)
                # Under the repaired gate, no problem asks for a word the
                # author view does not show (D9's second defect).
                view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
                view_vocab = prose_fidelity._vocabulary(view)
                for sentence in red0 + red1:
                    locus = prose_fidelity.problem_locus(sentence)
                    for identifier in locus.identifiers:
                        if "_" in identifier:
                            self.assertIn(identifier.lower(), view_vocab, (report_id, sentence[:160]))

    def test_new_diagnostic_names_every_mart_item_and_rule_kind_value_free(self):
        """The repaired projection of the SAME red drafts names, per mart, the
        item kinds and the rule operation kinds the gate could not find, in
        codes only — through the projector and the D1 gatekeeper."""
        for report_id, task_id, task, drafts in self.cases:
            with self.subTest(report=report_id):
                first, _ = drafts[0]
                red = task.model_copy(update={"solver_prompt": first})
                problems = prose_fidelity.check_prose_fidelity(red)
                diag = V.project_prose_check(problems, task=red)
                self.assertEqual(diag.code, "prose_problems")
                loci = [prose_fidelity.problem_locus(p) for p in problems]
                self.assertNotIn("other", {l.item for l in loci})
                implicated = {l.mart for l in loci if l.mart}
                self.assertTrue(implicated)
                for mart in implicated:
                    self.assertIn(mart, diag.names)
                    key = V._flag_key(V.PROSE_MART_FLAG_PREFIX, mart)
                    self.assertTrue(any(k.startswith(key + "_") for k in diag.flags), (report_id, mart))
                for locus in loci:
                    self.assertTrue(diag.flags.get(f"prose_item_{locus.item}"), (report_id, locus))
                    if locus.mart and locus.item == "rule":
                        self.assertTrue(diag.flags.get(V._mart_flag(locus.mart, "rule", locus.op_kind)), (report_id, locus))
                self.assertEqual(diag.subject, next(iter(implicated)) if len(implicated) == 1 else "")
                raw = PJ.serialize_for_transport(diag, task=red)
                PJ.assert_value_free(raw.encode("utf-8"), task=red)
                rendered = diag.render()
                for sentence in problems:
                    self.assertNotIn(sentence, rendered)

    def test_revision_that_follows_the_diagnostic_clears_the_gate_in_one_round(self):
        """PROOF. Through the real provider layer under the SHIPPED author
        policy: turn 0 submits the batch's recorded first draft; the harness
        answers with the new codes; turn 1 submits the scripted revision built
        from EXACTLY that answer; the real gate is green, the session ends
        SUBMITTED after one revision, and the author stage records a PASS
        with `authored_revised` / revisions 1."""
        policy = V.author_policy()
        self.assertEqual(policy.limits.max_revisions, 4)
        for report_id, task_id, task, drafts in self.cases:
            with self.subTest(report=report_id), tempfile.TemporaryDirectory() as tmp:
                first, _ = drafts[0]
                red = task.model_copy(update={"solver_prompt": first})
                expected = V.project_prose_check(prose_fidelity.check_prose_fidelity(red), task=red)
                revision = _d9_guided_revision(first, expected, task)
                # The scripted author kept the unflagged passages: every line
                # of the first draft outside a rewritten header survives.
                surviving = set(revision.splitlines())
                headers = {sections for sections in prose_fidelity._mart_sections(task.marts, first)[0].values()}
                header_lines = {text.splitlines()[0] for text in headers if text}
                lost = [line for line in first.splitlines() if line.strip() and line not in surviving and line not in header_lines]
                self.assertEqual(lost, [], report_id)
                transport = FakeTransport([_submit(first, "t0"), _submit(revision, "t1")])
                provider = _d9_session_provider(tmp, task, transport)
                record: dict = {}
                prose = council.author_prose_session(task, provider, tools=policy.tools, policy=policy, record=record)
                self.assertEqual(prose, revision)
                self.assertEqual(len(transport.calls), 2)
                content, is_error = _tool_results(transport.calls[1][2])[0]
                self.assertTrue(is_error)
                self.assertEqual(content, expected.render())
                self.assertEqual(prose_fidelity.check_prose_fidelity(task.model_copy(update={"solver_prompt": prose})), [])
                result = record["result"]
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual(record["revisions"], 1)
                self.assertEqual(record["drafts"], 2)
                self.assertEqual(result.red_validators_at_submit, ())
                # The author stage: PASS, provenance authored_revised / 1.
                stage = SessionDouble([
                    [tool_use("submit_prose", {"text": first}, "a")],
                    [tool_use("submit_prose", {"text": revision}, "b")],
                ])
                outcome = cli.make_author_runner(stage)(_engine(tmp), task)
                self.assertEqual(outcome.verdict, cli.VERDICT_PASS, outcome.payload.error)
                self.assertEqual(outcome.payload.data[cli.AUTHOR_SOURCE_KEY], "authored_revised")
                self.assertEqual(outcome.payload.data[cli.AUTHOR_REVISIONS_KEY], "1")
                self.assertEqual(outcome.task.solver_prompt, revision)

    def test_revision_that_ignores_the_diagnostic_stops_bounded_and_structured(self):
        """A session whose revisions ignore the diagnostic (the same red draft
        three times) still ends deterministically under the shipped block:
        SUBMITTED with the red validator recorded after exactly four
        corrections (the shipped budget since the 2026-09-10 rerun), and the
        author stage records a structured FAIL routed to
        SPECIFICATION (never a bare exception); with a turn cap binding before
        any draft, the typed limit stop maps to VERDICT_BLOCKED with a salt."""
        policy = V.author_policy()
        report_id, task_id, task, drafts = self.cases[0]
        first, _ = drafts[0]
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([_submit(first, f"t{index}") for index in range(5)])
            provider = _d9_session_provider(tmp, task, transport)
            record: dict = {}
            prose = council.author_prose_session(task, provider, tools=policy.tools, policy=policy, record=record)
            self.assertEqual(prose, first)
            self.assertEqual(len(transport.calls), 5)
            result = record["result"]
            self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
            self.assertEqual(result.red_validators_at_submit, ("check_prose",))
            self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 4})
            self.assertEqual((record["revisions"], record["drafts"]), (4, 5))
            # Both revision requests carried the aimed codes.
            for call in transport.calls[1:]:
                content, is_error = _tool_results(call[2])[-1]
                self.assertTrue(is_error)
                self.assertIn("prose_item_", content)
                self.assertIn("prose_mart_", content)
            stage = SessionDouble([[tool_use("submit_prose", {"text": first}, i)] for i in ("a", "b", "c", "d", "e")])
            outcome = cli.make_author_runner(stage)(_engine(tmp), task)
            self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
            self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
            self.assertIn("MartSpec item(s) not represented", outcome.payload.error)
            self.assertEqual(outcome.payload.data["gate"], prose_fidelity.GATE_NAME)
            self.assertEqual(outcome.payload.data[cli.AUTHOR_SOURCE_KEY], "authored_revised")
            self.assertEqual(outcome.payload.data[cli.AUTHOR_REVISIONS_KEY], "4")
            self.assertEqual(outcome.payload.data[cli.AUTHOR_SESSION_TERMINAL_KEY], "SUBMITTED")
            self.assertEqual(engine_mod._infra_marker_for(RuntimeError(outcome.payload.error)), "")
            # A harness cap that binds before any draft: BLOCKED with a salt.
            capped = V.author_policy(V.author_limits({**ENABLED_BLOCK, "max_turns": 1}))
            empty = SessionDouble([[tool_use("submit_prose", {"text": ""}, "a")]] * 3)
            with self.assertRaises(council.AuthorSessionLimitStop) as caught:
                council.author_prose_session(task, empty, tools=capped.tools, policy=capped)
            blocked = council.author_session_limit_outcome(
                caught.exception, SimpleNamespace(session_limit_reruns=lambda task_id, stage: 0), task
            )
            self.assertEqual(blocked.verdict, engine_mod.VERDICT_BLOCKED)
            self.assertEqual(blocked.payload.data[engine_mod.BLOCKED_ON_KEY], "session_limit:turns")
            with _AuthorConfig(max_turns=1):
                empty = SessionDouble([[tool_use("submit_prose", {"text": ""}, "a")]] * 3)
                outcome = cli.make_author_runner(empty)(_engine(tmp), task)
            self.assertEqual(outcome.verdict, engine_mod.VERDICT_BLOCKED)
            self.assertEqual(outcome.payload.data[engine_mod.BLOCKED_ON_KEY], "session_limit:turns")


#: The batch's three dlt tasks (review finding 1-0): every one publishes a
#: leading-underscore column — dlt's `_<table>_id` / `_<table>_surrogate_id`
#: families — and the recorded semantic_author drafts of all three are red
#: under the repaired gate on sentences that QUOTE those names.
DLT_TASKS: tuple[str, ...] = ("dlt__personio", "dlt__pipedrive", "dlt__workable")


def _underscore_names(task) -> frozenset[str]:
    """The task's public identifiers a letter-first grammar would refuse."""
    names = {c.name for t in task.tables for c in t.columns}
    names |= {c.name for m in task.marts for c in m.columns}
    names |= {k for m in task.marts for k in m.key_columns}
    for rel in task.relationships:
        names.update(rel.child_columns)
        names.update(rel.parent_columns)
    return frozenset(n for n in names if n.startswith("_"))


@unittest.skipUnless(all((BATCH_DIR / "tasks" / t / "task_ir.json").is_file() for t in DLT_TASKS),
                     "batch evidence runs/authorized_batch_50_20260908 not present")
class AuthorUnderscoreColumnProjectionTest(_Case):
    """Review finding 1-0 (batch-repair round 2): dlt__workable's mart
    `dim_jobs` carries the public column `_jobs_surrogate_id`.  A draft that
    merely omitted it made `check_prose_fidelity` say "output column
    '_jobs_surrogate_id' never mentioned", `project_prose_problems` put that
    name into `DiagnosticText.names`, and the schema's letter-first identifier
    grammar raised its ValidationError inside the author session's
    `check_prose` — a ToolHarnessFault (infrastructure, exit 2) for a model
    draft (C7).  The batch only survived because the recorded draft happened
    to mention the column.

    The decision: a public column with a leading underscore IS a legitimate
    public identifier (dlt's `_dlt_*` / `_<table>_id` families, Fivetran's
    `_fivetran_synced`) and must travel VERBATIM in the author's `check_prose`
    feedback.  `projection._IDENT_RE` now admits the shape (underscores, then
    a letter), so `subject` / `names` carry it; membership in
    `PublicIdentifierSet` is still enforced by both transport halves, and a
    bare number or a path is still refused.  Proved here on the three dlt IRs
    of the batch (read-only) through the REAL fidelity gate, the REAL
    projection, both transport halves and the REAL author session.  Seeded
    from scratchpad/impl/batch/probe_r2_prose_projection_underscore.py."""

    def test_identifier_grammar_admits_leading_underscore_names_and_refuses_values(self):
        for name in ("_jobs_surrogate_id", "_deals_id", "_fivetran_synced", "_dlt_load_id",
                     "__dlt_twice", "customers", "sales-2020", "v1.2"):
            self.assertIsNotNone(PJ._IDENT_RE.fullmatch(name), name)
            self.assertTrue(V._names_entry(name), name)
        for value in ("1234", "_1", "_", "__", "", "a b", "/Users/x", "c_9001 ", "-2"):
            self.assertIsNone(PJ._IDENT_RE.fullmatch(value), value)
        # The schema admits the name in both identifier fields, and the
        # projector / gatekeeper still hold it to the PUBLIC set.
        task = _d9_task("dlt__workable")
        diag = PJ.Diagnostic(source=PJ.DiagnosticSource.CHEAP, ok=False, code="prose_problems",
                             subject="dim_jobs", names=("_jobs_surrogate_id",))
        wire = PJ.serialize_for_transport(diag, task=task)
        PJ.assert_value_free(wire.encode("utf-8"), task=task)
        self.assertIn("names=_jobs_surrogate_id", diag.render())
        private = diag.model_copy(update={"names": ("_not_a_column_of_this_task",)})
        with self.assertRaises(PJ.DiagnosticTripwire):
            PJ.serialize_for_transport(private, task=task)
        with self.assertRaises(PJ.DiagnosticTripwire):
            PJ.assert_value_free(PJ.canonical_json(PJ._transport_payload(private)).encode("utf-8"), task=task)

    def test_omitting_a_leading_underscore_column_is_a_red_diagnostic_naming_it(self):
        """The finding's trigger, on dlt__workable: an empty draft and a draft
        that names the mart but omits its `_jobs_surrogate_id` key/output
        column.  `check_prose` is a red Diagnostic — never a ValidationError
        — that NAMES the column and its mart, through both transport halves;
        the real author session hands the seat that name in its correction
        and ends SUBMITTED with no fault."""
        task = _d9_task("dlt__workable")
        self.assertIn("_jobs_surrogate_id", [c.name for m in task.marts for c in m.columns])
        omitting = "The dim_jobs mart has one row per jobs record."
        for draft in ("", omitting):
            with self.subTest(draft=draft[:20]):
                candidate = task.model_copy(update={"solver_prompt": draft})
                problems = [str(p) for p in prose_fidelity.check_prose_fidelity(candidate)]
                items = PJ.project_prose_problems(problems, task=candidate)
                self.assertEqual(len(items), len(problems))
                diag = V.project_prose_check(problems, task=candidate)
                self.assertFalse(diag.ok)
                if "_jobs_surrogate_id" in " ".join(problems):
                    column_items = [i for i in items if "output column '_jobs_surrogate_id'" in i.text]
                    self.assertTrue(column_items)
                    self.assertTrue(all(i.names == ("_jobs_surrogate_id",) and i.subject == "dim_jobs"
                                        and i.code == "missing_object" for i in column_items), column_items)
                    self.assertIn("_jobs_surrogate_id", diag.names)
                    self.assertIn("dim_jobs", diag.names)
                    self.assertTrue(diag.flags.get("prose_missing_object"))
                    self.assertTrue(diag.flags.get("prose_mart_dim_jobs_output_column"))
                    self.assertTrue(diag.flags.get("prose_mart_dim_jobs_key_column"))
                transport = PJ.serialize_for_transport(diag, task=candidate)
                PJ.assert_value_free(transport.encode("utf-8"), task=candidate)
                self.assertFalse(re.search(r"\d", diag.render()), diag.render())
        # The REAL author session on that IR with the omitting draft: a red
        # `check_prose` correction NAMING the column, a SUBMITTED session,
        # never an escape.
        policy = _policy(max_revisions=1, max_turns=2, max_tool_calls=2)
        provider = SessionDouble([[tool_use("submit_prose", {"text": omitting}, "a")],
                                  [tool_use("submit_prose", {"text": omitting}, "b")]])
        record: dict = {}
        prose = council.author_prose_session(task, provider, tools=policy.tools, policy=policy, record=record)
        self.assertEqual(prose, omitting)
        result = record["result"]
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertIsNone(result.fault)
        self.assertEqual(result.red_validators_at_submit, ("check_prose",))
        self.assertEqual([t.outcome_code for t in result.turns if t.kind == "validator"],
                         ["prose_problems", "prose_problems"])
        correction = provider.scripted.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertTrue(correction.startswith("[cheap] prose_problems"), correction)
        self.assertIn("prose_missing_object=true", correction)
        self.assertIn("_jobs_surrogate_id", correction.split("names=")[-1].split(","))
        self.assertIn("dim_jobs", correction)

    def test_dlt_author_sessions_receive_the_underscore_identifiers_and_a_guided_revision_clears(self):
        """PROOF on all three dlt IRs, through the real provider layer under
        the SHIPPED author policy: turn 0 submits the batch's recorded first
        draft (red under the repaired gate on relationship / column sentences
        that quote `_deals_id`, `_employees_id`, `_candidates_id`, `_jobs_id`,
        `_jobs_surrogate_id`); the harness answers with a correction whose
        `names` carry EVERY underscore identifier those sentences quote,
        verbatim; turn 1 submits the scripted revision built from exactly that
        answer and the real gate is green — SUBMITTED after one revision,
        never a harness fault."""
        policy = V.author_policy()
        for task_id in DLT_TASKS:
            with self.subTest(task=task_id), tempfile.TemporaryDirectory() as tmp:
                task = _d9_task(task_id)
                underscore = _underscore_names(task)
                self.assertTrue(underscore, task_id)
                drafts = _d9_drafts(task_id)
                if 0 not in drafts:
                    self.skipTest(f"semantic_author transcript for {task_id} not present")
                first, _ = drafts[0]
                red = task.model_copy(update={"solver_prompt": first})
                problems = prose_fidelity.check_prose_fidelity(red)
                self.assertTrue(problems, task_id)
                quoted = {
                    identifier
                    for sentence in problems
                    for identifier in prose_fidelity.problem_locus(sentence).identifiers
                    if identifier in underscore
                }
                self.assertTrue(quoted, (task_id, problems[:3]))
                expected = V.project_prose_check(problems, task=red)
                for identifier in quoted:
                    self.assertIn(identifier, expected.names, (task_id, identifier))
                PJ.assert_value_free(PJ.serialize_for_transport(expected, task=red).encode("utf-8"), task=red)
                revision = _d9_guided_revision(first, expected, task)
                transport = FakeTransport([_submit(first, "t0"), _submit(revision, "t1")])
                provider = _d9_session_provider(tmp, task, transport)
                record: dict = {}
                prose = council.author_prose_session(task, provider, tools=policy.tools, policy=policy, record=record)
                self.assertEqual(prose, revision)
                self.assertEqual(len(transport.calls), 2)
                content, is_error = _tool_results(transport.calls[1][2])[0]
                self.assertTrue(is_error)
                self.assertEqual(content, expected.render())
                named = content.split("names=")[-1].split(",")
                for identifier in quoted:
                    self.assertIn(identifier, named, (task_id, identifier))
                self.assertEqual(prose_fidelity.check_prose_fidelity(task.model_copy(update={"solver_prompt": prose})), [])
                result = record["result"]
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertIsNone(result.fault)
                self.assertEqual((record["revisions"], record["drafts"]), (1, 2))
                self.assertEqual(result.red_validators_at_submit, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

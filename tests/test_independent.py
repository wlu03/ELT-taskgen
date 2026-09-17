"""Tests for reference/independent.py — the cross-family dual build.

WHY THIS EXISTS
The dual-build-agreement gate closes the trusted-solution self-certification
hole (a wrong reference certifying its own wrong gold — the highest-severity
Round-1 defect). These tests prove, with REAL execution over rendered
populations and REAL frozen gold:

  * the implementer view is strictly the public bundle (reference SQL and
    attack mutations trip the leak assertion),
  * the {mart: sql} response schema is enforced (ProviderProtocolError),
  * a fixture 'implementer' whose SQL is the known-correct demo solution
    (test-internal instrument — EXECUTED, never trusted) agrees 1.0 with
    correct gold, and DETECTS corrupted gold (flipped value) and wrong-join
    reference gold as disagreements routed to NEEDS_ADJUDICATION,
  * resampling happens only for implementer-side failures that also fail the
    public (development) examples, bounded at MAX_SAMPLES,
  * agreement recorded at a stale hash is red, missing evidence is red, and
    a same-family routing is refused (cross-family is mandatory).

No network, no API keys: the provider doubles here return canned text; the
execution/scoring path underneath is the real one (trusted loaders + THE
single reward).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.demo_fixture import MART_NAME, REFERENCE_SQL
from elt_taskgen.generation import source_data
from elt_taskgen.models import PopulationName, TaskIR
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.reference import independent
from elt_taskgen.reference import runner as runner_mod
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.verification import gates, upstream_eval

P = PopulationName


class ScriptedProvider:
    """Serves canned responses in call order (last one repeats). A bare test
    double: no routing attribute, so it must OPT IN to bypass the cross-family
    guard (unrouted_test_double) — its SQL is executed and scored, never
    trusted."""

    unrouted_test_double = True

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, role, prompt: str) -> str:
        self.calls.append((str(role), prompt))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def correct_response() -> str:
    return json.dumps({MART_NAME: REFERENCE_SQL})


def build_workspace(task: TaskIR, workspace: Path):
    """Real pipeline path: generate + render all five populations, execute the
    reference, freeze gold under answer_key/. Returns the GoldBundle."""
    tdir = workspace / "tasks" / task.task_id
    for pop_spec in sorted(task.populations, key=lambda p: p.name.value):
        pop = pop_spec.name
        source_data.materialize_population(
            task,
            pop,
            tdir / "populations" / pop.value,
        )
    results = {
        pop: runner_mod.run_reference(task, pop, workspace) for pop in P
    }
    return gold_mod.freeze_gold(task, results, tdir / "answer_key")


#: Module-level cache: population generation + rendering + gold freeze for the
#: demo task takes tens of seconds — build it ONCE for every test class here.
_SHARED: dict = {}


def _shared_fixture():
    if not _SHARED:
        task = demo_fixture.demo_task()
        tmp = tempfile.TemporaryDirectory()

        def cleanup_fixture() -> None:
            _SHARED.clear()
            tmp.cleanup()

        unittest.addModuleCleanup(cleanup_fixture)
        workspace = Path(tmp.name) / "taskgen-workspace"
        gold = build_workspace(task, workspace)
        _SHARED.update(
            {"task": task, "tmp": tmp, "workspace": workspace, "gold": gold}
        )
    return _SHARED


class IndependentTestCase(unittest.TestCase):
    """Shared expensive fixture: one generated+rendered demo workspace."""

    task: TaskIR
    base_workspace: Path

    @classmethod
    def setUpClass(cls) -> None:
        shared = _shared_fixture()
        cls.task = shared["task"]
        cls.base_workspace = shared["workspace"]
        cls.gold = shared["gold"]

    def fresh_workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name) / "taskgen-workspace"
        shutil.copytree(self.base_workspace, dest)
        return dest


# ---------------------------------------------------------------------------
# The implementer view: strictly public, leak-tripwired
# ---------------------------------------------------------------------------

class TestImplementerView(IndependentTestCase):
    def test_view_contains_public_material_only(self) -> None:
        view = independent.implementer_view(self.task)
        norm = independent._normalize(view)
        self.assertIn(MART_NAME, view)
        self.assertIn(self.task.mart(MART_NAME).grain.lower(), norm)
        self.assertIn("customers", view)
        # No reference SQL, no attack mutations, no answer-key markers.
        self.assertNotIn(independent._normalize(REFERENCE_SQL), norm)
        for case in self.task.attack_cases:
            if case.mutation:
                self.assertNotIn(independent._normalize(case.mutation), norm)
        self.assertNotIn("answer_key", norm)

    def test_view_raises_when_prose_leaks_reference_sql(self) -> None:
        leaky = self.task.model_copy(
            update={"solver_prompt": "Here is the answer:\n" + REFERENCE_SQL}
        )
        with self.assertRaises(ValueError):
            independent.implementer_view(leaky)

    def test_resample_prompts_are_distinct_transcript_keys(self) -> None:
        p0 = independent.sample_prompt(self.task, 0)
        p1 = independent.sample_prompt(self.task, 1)
        self.assertNotEqual(p0, p1)
        self.assertTrue(p1.startswith(p0))


# Implementer prompt contract. It must not reveal reference existence or shape,
# because independent agreement must come from the specification alone.
_REFERENCE_SHAPE_HINTS: tuple[str, ...] = (
    "reference",
    "gold",
    "expected solution",
    "canonical solution",
    "model solution",
    "the intended query",
    "second implementation",
    "another engineer",
    "another implementation",
    "agreement",
    "agrees with",
    "dual build",
    "dual-build",
    "cross-family",
    "adjudication",
    "left join",       # no join/shape coaching of any kind
    "inner join",
    "coalesce",
    "group by",
)

#: Answer-side material that must never reach any solver-facing view — checked
#: over the WHOLE prompt (factory text plus task material).
_ANSWER_SIDE_MARKERS: tuple[str, ...] = (
    "answer_key",
    "answer-key",
    "stage2_csv",
    "gold_csv",
    "attack_case",
    "expected row count of",
)

#: Private factory vocabulary — checked over the factory-authored preamble
#: only, because a table description legitimately may contain words like
#: 'stress' or 'development' (the leak tripwire owns the task material).
_PRIVATE_VOCAB: tuple[str, ...] = (
    "population",
    "development",
    "resampled",
    "counterfactual",
    "stress",
    "attack",
    "mutation",
    "council",
    "shortcut",
)

#: The SoT T1 IMP block (config/agents.yaml `roles.independent_implementer.
#: session`) with the session ENABLED — what a witness session runs under.
_IMPLEMENTER_SESSION_BLOCK: dict = {
    "enabled": True,
    "max_turns": 12,
    "max_tool_calls": 20,
    "per_tool": {"dev_query": 8, "dry_run_sql": 8, "run_mart_sql_dev": 2},
    # The harness-run submission dry run (2026-09-11, batch10 run O).
    "harness_validators": ["check_submission"],
    "max_compile_corrections": 2,
    "max_usd": 0.50,
    "wall_clock_s": 1500,
    "hard_caps": {"turns": 14, "tool_calls": 24, "compile_corrections": 2, "usd": 0.50, "wall_clock_s": 1800},
}


class TestImplementerPromptContract(IndependentTestCase):
    """The prompt is production text for a live cross-family model: it must
    carry its whole contract and leak nothing, including the SHAPE of the
    reference implementation."""

    def test_prompt_states_its_executable_contract(self) -> None:
        view = independent.implementer_view(self.task)
        # Whitespace-collapsed: the prompt is hard-wrapped, so phrases span
        # line breaks.
        lower = independent._normalize(view)
        # (a) exact output contract: JSON, the mart names, DuckDB dialect.
        self.assertIn("--- RESPONSE FORMAT (STRICT) ---", view)
        self.assertIn("json object", lower)
        self.assertIn("only one json object", lower)
        self.assertIn("duckdb", lower)
        self.assertIn(f'"{MART_NAME}"', view)
        # (a) it is EXECUTED on five populations and compared column-wise
        # against a frozen answer key.
        self.assertIn("executed", lower)
        self.assertIn("five", lower)
        self.assertIn("column by column", lower)
        self.assertIn("frozen answer key", lower)
        # (b) total-order determinism, since the comparator sorts everything.
        self.assertIn("determinism", lower)
        self.assertIn("order by", lower)
        self.assertIn("tie", lower)
        # (d) implement the spec even when it looks odd.
        self.assertIn("the specification wins", lower)
        # what it is NOT given, so it does not hallucinate access.
        self.assertIn("not given", lower)
        # untrusted-input rule (rule 6): view content is data, not orders.
        self.assertIn("untrusted", lower)
        # no acceptance vocabulary: this role never approves anything.
        for verboten in ("approve", "accept the task", "sign off"):
            self.assertNotIn(verboten, lower)
        # The session preamble adds warehouse and tool access while the
        # one-shot prompt stays byte-stable for recorded transcript keys.
        from elt_taskgen.review.tools import validators as witness_tools

        limits = witness_tools.implementer_limits(_IMPLEMENTER_SESSION_BLOCK)
        session_view = independent.implementer_session_view(self.task, 0, limits=limits)
        session_lower = independent._normalize(session_view)
        self.assertTrue(session_view.startswith(independent._IMPLEMENTER_SESSION_PREAMBLE))
        self.assertNotEqual(independent._IMPLEMENTER_SESSION_PREAMBLE, independent._IMPLEMENTER_PREAMBLE)
        # (a) the same executable contract ...
        for phrase in (
            "executed", "five", "column by column", "frozen answer key",
            "determinism", "order by", "tie", "the specification wins",
            "not given", "untrusted", "duckdb",
        ):
            self.assertIn(phrase, session_lower, phrase)
        self.assertIn(f'"{MART_NAME}"', session_view)
        # ... stated through the tool protocol, not a JSON reply ...
        self.assertIn("--- HOW TO ANSWER (TOOL PROTOCOL) ---", session_view)
        self.assertNotIn("--- RESPONSE FORMAT (STRICT) ---", session_view)
        self.assertNotIn("only one json object", session_lower)
        for tool_name in witness_tools.IMPLEMENTER_TOOL_NAMES:
            self.assertIn(tool_name, session_view, tool_name)
        self.assertIn(f"at most {limits.max_turns} turns", session_view)
        self.assertIn(f"{limits.max_tool_calls} tool calls in total", session_view)
        # ... and the reworded "given" section: the one-shot clause denying any
        # way to run or inspect a query would be false, so it is gone, while
        # the graded datasets and every grading result stay withheld.
        self.assertIn("any way to run, test, or inspect a query", lower)
        self.assertNotIn("any way to run, test, or inspect a query", session_lower)
        self.assertIn("sample warehouse", session_lower)
        self.assertIn("no tool result tells you whether a mart matches the answer key", session_lower)
        self.assertIn("any grading result", session_lower)
        for verboten in ("approve", "accept the task", "sign off"):
            self.assertNotIn(verboten, session_lower)
        # The salt of attempt N > 1 is the SAME sentence on both paths, so a
        # session's salt subsumes `sample_prompt`'s (04 §7).
        salted = independent.implementer_session_view(self.task, 1, limits=limits)
        self.assertTrue(salted.startswith(session_view))
        self.assertEqual(
            salted[len(session_view):],
            independent.sample_prompt(self.task, 1)[len(independent.implementer_view(self.task)):],
        )

    def test_prompt_carries_no_reference_shape_hints(self) -> None:
        # Asserted on the factory-authored preamble: task material below it is
        # the author's prose, which the leak scan and prose gate own.
        preamble = independent._normalize(independent._IMPLEMENTER_PREAMBLE)
        for hint in _REFERENCE_SHAPE_HINTS + _PRIVATE_VOCAB:
            self.assertNotIn(hint, preamble, f"reference-shape hint: {hint!r}")
        # And the assembled demo view carries no reference-shape hint either.
        view = independent._normalize(independent.implementer_view(self.task))
        for hint in _REFERENCE_SHAPE_HINTS:
            self.assertNotIn(hint, view, f"reference-shape hint in view: {hint!r}")
        # The SESSION preamble and its tool protocol are held to the same pin
        # (roadmap 2.a: "reworded within the `_PRIVATE_VOCAB` pin").
        from elt_taskgen.review.tools import validators as witness_tools

        limits = witness_tools.implementer_limits(_IMPLEMENTER_SESSION_BLOCK)
        factory_text = independent._normalize(
            independent._IMPLEMENTER_SESSION_PREAMBLE
            + "\n"
            + "\n".join(independent._implementer_session_protocol_lines(self.task, limits))
        )
        for hint in _REFERENCE_SHAPE_HINTS + _PRIVATE_VOCAB:
            self.assertNotIn(hint, factory_text, f"reference-shape hint in session text: {hint!r}")
        session_view = independent._normalize(
            independent.implementer_session_view(self.task, 0, limits=limits)
        )
        for hint in _REFERENCE_SHAPE_HINTS:
            self.assertNotIn(hint, session_view, f"reference-shape hint in session view: {hint!r}")

    def test_prompt_carries_no_answer_side_markers(self) -> None:
        norm = independent._normalize(independent.implementer_view(self.task))
        for marker in _ANSWER_SIDE_MARKERS:
            self.assertNotIn(marker, norm, f"answer-side marker: {marker!r}")
        self.assertNotIn(independent._normalize(REFERENCE_SQL), norm)
        for case in self.task.attack_cases:
            if case.mutation:
                self.assertNotIn(independent._normalize(case.mutation), norm)
        # The session view is held to the same markers and the same leak guard.
        from elt_taskgen.review.tools import validators as witness_tools

        session_norm = independent._normalize(
            independent.implementer_session_view(
                self.task, 0, limits=witness_tools.implementer_limits(_IMPLEMENTER_SESSION_BLOCK)
            )
        )
        for marker in _ANSWER_SIDE_MARKERS:
            self.assertNotIn(marker, session_norm, f"answer-side marker in session view: {marker!r}")
        self.assertNotIn(independent._normalize(REFERENCE_SQL), session_norm)
        leaky = self.task.model_copy(
            update={"solver_prompt": "Here is the answer:\n" + REFERENCE_SQL}
        )
        with self.assertRaises(ValueError):
            independent.implementer_session_view(leaky, 0)

    def test_prompt_is_deterministic_for_a_fixed_task(self) -> None:
        first = independent.implementer_view(demo_fixture.demo_task())
        second = independent.implementer_view(demo_fixture.demo_task())
        self.assertEqual(first, second)
        for index in (0, 1):
            self.assertEqual(
                independent.sample_prompt(demo_fixture.demo_task(), index),
                independent.sample_prompt(demo_fixture.demo_task(), index),
            )
        # Invariant framing FIRST (prompt caching), salt appended LAST.
        self.assertTrue(first.startswith(independent._IMPLEMENTER_PREAMBLE))
        self.assertTrue(
            independent.sample_prompt(self.task, 1).startswith(
                independent.implementer_view(self.task)
            )
        )


# ---------------------------------------------------------------------------
# Response schema enforcement
# ---------------------------------------------------------------------------

class TestParseSqlByMart(IndependentTestCase):
    def test_plain_and_fenced_json_accepted(self) -> None:
        expected = {MART_NAME: REFERENCE_SQL}
        self.assertEqual(
            independent.parse_sql_by_mart(self.task, correct_response()), expected
        )
        fenced = "```json\n" + correct_response() + "\n```"
        self.assertEqual(
            independent.parse_sql_by_mart(self.task, fenced), expected
        )

    def test_invalid_payloads_raise_provider_protocol_error(self) -> None:
        bad = [
            "not json at all",
            json.dumps(["list", "not", "object"]),
            json.dumps({}),                                   # missing mart
            json.dumps({MART_NAME: REFERENCE_SQL, "extra": "SELECT 1"}),
            json.dumps({MART_NAME: ""}),                      # empty SQL
            json.dumps({MART_NAME: 42}),                      # non-string
        ]
        for text in bad:
            with self.assertRaises(ProviderProtocolError, msg=text[:40]):
                independent.parse_sql_by_mart(self.task, text)


# ---------------------------------------------------------------------------
# Agreement with correct gold (the green path)
# ---------------------------------------------------------------------------

class TestAgreement(IndependentTestCase):
    def test_correct_sql_agrees_on_all_five_populations(self) -> None:
        ws = self.fresh_workspace()
        provider = ScriptedProvider([correct_response()])
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(len(result.samples), 1)
        self.assertEqual(
            result.agreement, {p.value: 1.0 for p in P}
        )
        self.assertEqual(result.task_content_hash, self.task.content_hash())

        independent.record_build_result(ws, self.task, result)
        gate = gates._gate_dual_build_agreement(self.task, ws)
        self.assertTrue(gate.passed, gate.details)
        trusted = gates._gate_trusted_solution(self.task, ws, self.gold)
        self.assertTrue(trusted.passed, trusted.details)
        # No adjudication queued on agreement.
        self.assertIsNone(independent.load_adjudication(ws, self.task.task_id))


# ---------------------------------------------------------------------------
# Wrong-gold fixtures: the gate must DETECT disagreement
# ---------------------------------------------------------------------------

def corrupt_gold_value(gold, population: str, mart: str):
    """Flip one numeric value inside the frozen mart CSV for one population."""
    csv_text = gold.stage2_csv[population][mart]
    cols, rows = upstream_eval.parse_canonical_csv(csv_text)
    self_rows = [dict(r) for r in rows]
    assert self_rows, "corruption fixture needs at least one gold row"
    self_rows[0]["total_spend"] = "999999.5"  # a flipped value
    wrong_csv = upstream_eval.rows_to_canonical_csv(self_rows, cols)
    stage2 = {p: dict(m) for p, m in gold.stage2_csv.items()}
    stage2[population][mart] = wrong_csv
    return gold.model_copy(update={"stage2_csv": stage2})


class TestWrongGoldDetection(IndependentTestCase):
    def test_flipped_gold_value_is_detected_as_disagreement(self) -> None:
        ws = self.fresh_workspace()
        wrong_gold = corrupt_gold_value(self.gold, P.PRIMARY.value, MART_NAME)
        provider = ScriptedProvider([correct_response()])
        result = independent.run_independent_build(
            self.task, ws, provider, wrong_gold
        )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        # Development gold is intact => the sample aces the public examples
        # and the hidden disagreement is adjudicated, NOT resampled away.
        self.assertEqual(len(result.samples), 1)
        self.assertTrue(result.samples[0].dev_pass)
        self.assertLess(result.agreement[P.PRIMARY.value], 1.0)
        self.assertEqual(result.agreement[P.DEVELOPMENT.value], 1.0)

        independent.record_build_result(ws, self.task, result)
        gate = gates._gate_dual_build_agreement(self.task, ws)
        self.assertFalse(gate.passed)
        self.assertIn("disagrees", gate.details)
        trusted = gates._gate_trusted_solution(self.task, ws, wrong_gold)
        self.assertFalse(trusted.passed)
        # And the disagreement is queued for human adjudication.
        adjudication = independent.load_adjudication(ws, self.task.task_id)
        self.assertIsNotNone(adjudication)
        self.assertEqual(
            adjudication["task_content_hash"], self.task.content_hash()
        )
        self.assertEqual(adjudication["kind"], independent.ADJUDICATION_KIND)

    def test_wrong_join_reference_gold_is_detected(self) -> None:
        # Gold produced by a WRONG reference (INNER JOIN drops customers with
        # no completed orders). Round 1's gold-vs-gold trusted-solution gate
        # scored this 1.0; the independent build must expose it.
        ws = self.fresh_workspace()
        inner_sql = next(
            c.mutation
            for c in self.task.attack_cases
            if c.name == "inner_join"
        )
        stage2 = {}
        for pop in P:
            _, mart_rows = independent._execute_population(
                self.task, {MART_NAME: inner_sql}, pop, ws
            )
            mart = self.task.mart(MART_NAME)
            stage2[pop.value] = {
                MART_NAME: runner_mod.mart_rows_to_csv(mart_rows[MART_NAME], mart)
            }
        wrong_gold = self.gold.model_copy(update={"stage2_csv": stage2})

        provider = ScriptedProvider([correct_response()])
        result = independent.run_independent_build(
            self.task, ws, provider, wrong_gold
        )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        # Primary has customers without orders: correct LEFT JOIN output
        # disagrees with the inner-join gold there.
        self.assertLess(result.agreement[P.PRIMARY.value], 1.0)

        independent.record_build_result(ws, self.task, result)
        self.assertFalse(
            gates._gate_dual_build_agreement(self.task, ws).passed
        )

    def test_agreement_recorded_at_stale_hash_is_red(self) -> None:
        ws = self.fresh_workspace()
        provider = ScriptedProvider([correct_response()])
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        path = independent.record_build_result(ws, self.task, result)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["task_content_hash"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        gate = gates._gate_dual_build_agreement(self.task, ws)
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)
        trusted = gates._gate_trusted_solution(self.task, ws, self.gold)
        self.assertFalse(trusted.passed)
        self.assertIn("STALE", trusted.details)

    def test_missing_build_is_red_never_green(self) -> None:
        ws = self.fresh_workspace()
        gate = gates._gate_dual_build_agreement(self.task, ws)
        self.assertFalse(gate.passed)
        self.assertIn("independent build not performed", gate.details)
        trusted = gates._gate_trusted_solution(self.task, ws, self.gold)
        self.assertFalse(trusted.passed)
        self.assertIn("independent build not performed", trusted.details)


# ---------------------------------------------------------------------------
# Sampling policy (N=2 bounded)
# ---------------------------------------------------------------------------

class TestSamplingPolicy(IndependentTestCase):
    def test_implementer_side_failure_is_resampled_then_agrees(self) -> None:
        ws = self.fresh_workspace()
        bad = json.dumps({MART_NAME: "SELECT 1 AS nope"})  # fails everywhere
        provider = ScriptedProvider([bad, correct_response()])
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(result.samples), 2)
        first = result.samples[0]
        self.assertFalse(first.dev_pass)
        self.assertTrue(first.errors)  # execution failures recorded, scored 0
        self.assertEqual(first.rewards[P.DEVELOPMENT.value], 0.0)

    def test_persistent_failure_exhausts_budget_to_adjudication(self) -> None:
        ws = self.fresh_workspace()
        bad = json.dumps({MART_NAME: "SELECT 1 AS nope"})
        provider = ScriptedProvider([bad])
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold
        )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(provider.calls), independent.MAX_SAMPLES)
        self.assertEqual(len(result.samples), independent.MAX_SAMPLES)

    def test_unparseable_output_fails_closed_after_budget(self) -> None:
        ws = self.fresh_workspace()
        provider = ScriptedProvider(["this is not JSON"])
        with self.assertRaises(ProviderProtocolError):
            independent.run_independent_build(self.task, ws, provider, self.gold)
        self.assertEqual(len(provider.calls), independent.MAX_SAMPLES)

    def test_max_samples_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            independent.run_independent_build(
                self.task,
                self.base_workspace,
                ScriptedProvider([correct_response()]),
                self.gold,
                max_samples=0,
            )


# ---------------------------------------------------------------------------
# Cross-family mandate
# ---------------------------------------------------------------------------

class TestCrossFamilyMandate(unittest.TestCase):
    def _provider_with_routing(self, implementer_provider: str):
        route = types.SimpleNamespace(provider=implementer_provider)
        routing = types.SimpleNamespace(
            roles={"semantic_author": types.SimpleNamespace(provider="anthropic")},
            for_role=lambda name: route,
        )
        return types.SimpleNamespace(
            routing=routing,
            complete=lambda role, prompt: correct_response(),
        )

    def test_same_family_routing_is_refused(self) -> None:
        task = demo_fixture.demo_task()
        provider = self._provider_with_routing("anthropic")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                independent.run_independent_build(task, Path(tmp), provider, None)
        self.assertIn("cross-family", str(ctx.exception))

    def test_routingless_provider_without_opt_in_is_refused(self) -> None:
        """The bypass for bare test doubles is EXPLICIT OPT-IN: a hand-rolled
        provider with no routing and no unrouted_test_double flag must be
        refused, never silently waved past the mandatory cross-family rule."""
        provider = types.SimpleNamespace(
            complete=lambda role, prompt: correct_response()
        )
        with self.assertRaises(ValueError) as ctx:
            independent._assert_cross_family(provider)
        self.assertIn("unrouted_test_double", str(ctx.exception))

    def test_opted_in_test_double_passes_the_guard(self) -> None:
        independent._assert_cross_family(ScriptedProvider([correct_response()]))

    def test_default_routing_passes_the_guard(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        routing = providers_mod.load_role_routing()
        provider = types.SimpleNamespace(routing=routing)
        # Guard alone: must not raise for the shipped routing.
        independent._assert_cross_family(provider)

    # -- R2: the FAMILY, not the provider key, must differ -----------------

    @staticmethod
    def _routed_provider(model: str, base_url: str, *, role: str = independent.ROLE_NAME):
        """A routing built by load_role_routing on a temp agents.yaml, so the
        real loader (env interpolation, provider validation) is on the path."""
        import os
        import tempfile as _tempfile
        from unittest import mock

        from elt_taskgen.review import providers as providers_mod

        doc = (
            "providers:\n"
            "  anthropic:\n    api_key: sk-x\n"
            "  openai_compat:\n"
            f"    base_url: {base_url}\n    api_key: k\n    model: {model}\n"
            "roles:\n"
            "  semantic_author: {provider: anthropic, model: claude-opus-5, "
            "max_tokens: 100, effort: high}\n"
            f"  {role}: {{provider: openai_compat, model: {model}, "
            "max_tokens: 100}\n"
        )
        tmp = _tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        tmp.write(doc)
        tmp.close()
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                routing = providers_mod.load_role_routing(Path(tmp.name))
        finally:
            os.unlink(tmp.name)
        return types.SimpleNamespace(
            routing=routing, complete=lambda role, prompt: correct_response()
        )

    def test_openrouter_claude_behind_openai_compat_is_refused(self) -> None:
        provider = self._routed_provider(
            "anthropic/claude-opus-5", "https://openrouter.ai/api/v1"
        )
        with self.assertRaises(ValueError) as ctx:
            independent._assert_cross_family(provider)
        message = str(ctx.exception)
        self.assertIn("cross-family", message)
        self.assertIn("anthropic/claude-opus-5", message)
        self.assertIn("openrouter.ai", message)

    def test_anthropic_base_url_behind_openai_compat_is_refused(self) -> None:
        provider = self._routed_provider("foo-model", "https://api.anthropic.com/v1")
        with self.assertRaises(ValueError) as ctx:
            independent._assert_cross_family(provider)
        self.assertIn("cross-family", str(ctx.exception))
        self.assertIn("api.anthropic.com", str(ctx.exception))

    def test_kimi_via_openrouter_passes(self) -> None:
        provider = self._routed_provider(
            "moonshotai/kimi-k2.7-code", "https://openrouter.ai/api/v1"
        )
        independent._assert_cross_family(provider)  # no raise

    def test_same_unknown_family_as_author_is_refused(self) -> None:
        """Two identical self-hosted names are the same family; two distinct
        unknown names are not (unknown families compare by their full id)."""
        route = types.SimpleNamespace(
            provider="openai_compat", model="acme/served-a", max_tokens=1, effort=None
        )
        author = types.SimpleNamespace(provider="openai_compat_author", model="acme/served-b")
        routing = types.SimpleNamespace(
            roles={"semantic_author": author},
            for_role=lambda name: route,
            provider_config={"openai_compat": {"base_url": "http://gw/v1"}},
        )
        provider = types.SimpleNamespace(routing=routing)
        with self.assertRaises(ValueError):  # unknown:acme == unknown:acme
            independent._assert_cross_family(provider)
        author.model = "other/served-b"
        independent._assert_cross_family(provider)  # unknown:other != unknown:acme

    def test_provider_key_equality_is_still_refused(self) -> None:
        """Kept from the old guard so nothing refused before is admitted now."""
        route = types.SimpleNamespace(
            provider="openai_compat", model="moonshotai/kimi-k2.7-code",
            max_tokens=1, effort=None,
        )
        routing = types.SimpleNamespace(
            roles={"semantic_author": types.SimpleNamespace(
                provider="openai_compat", model="deepseek-chat"
            )},
            for_role=lambda name: route,
            provider_config={},
        )
        with self.assertRaises(ValueError):
            independent._assert_cross_family(types.SimpleNamespace(routing=routing))


class TestBuilderProvenance(IndependentTestCase):
    def test_result_records_provenance(self) -> None:
        provider = TestCrossFamilyMandate._routed_provider(
            "moonshotai/kimi-k2.7-code", "https://openrouter.ai/api/v1"
        )
        ws = self.fresh_workspace()
        result = independent.run_independent_build(self.task, ws, provider, self.gold)
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(result.provider, "openai_compat")
        self.assertEqual(result.model, "moonshotai/kimi-k2.7-code")
        self.assertEqual(result.endpoint_host, "openrouter.ai")
        # Round-trips through the recorded evidence file.
        independent.record_build_result(ws, self.task, result)
        loaded = independent.load_build_result(ws, self.task.task_id)
        self.assertEqual(loaded["model"], "moonshotai/kimi-k2.7-code")
        self.assertEqual(loaded["endpoint_host"], "openrouter.ai")

    def test_test_double_records_empty_provenance(self) -> None:
        ws = self.fresh_workspace()
        result = independent.run_independent_build(
            self.task, ws, ScriptedProvider([correct_response()]), self.gold
        )
        self.assertEqual((result.provider, result.model, result.endpoint_host), ("", "", ""))

    def test_pre_provenance_record_still_loads(self) -> None:
        legacy = {
            "task_id": self.task.task_id,
            "task_content_hash": self.task.content_hash(),
            "role": independent.ROLE_NAME,
            "status": independent.STATUS_AGREED,
            "agreement": {p.value: 1.0 for p in P},
            "samples": [],
            "detail": "legacy",
        }
        loaded = independent.IndependentBuildResult.model_validate(legacy)
        self.assertEqual(loaded.model, "")


class TestSandboxedExecution(IndependentTestCase):
    """N-corpus_calibration-8: the implementer's SQL is untrusted model output
    and runs on a sandboxed DuckDB connection — it cannot read the answer key
    off disk or write files on the host."""

    def test_implementer_sql_cannot_read_files(self) -> None:
        ws = self.fresh_workspace()
        gold_csv = (
            ws / "tasks" / self.task.task_id / "answer_key" / "gold" / "primary"
            / f"{MART_NAME}.csv"
        )
        self.assertTrue(gold_csv.is_file())
        leak = f"SELECT * FROM read_csv_auto('{gold_csv}', header=true)"
        with self.assertRaises(Exception) as ctx:
            independent._execute_population(self.task, {MART_NAME: leak}, P.PRIMARY, ws)
        self.assertIn("Permission", type(ctx.exception).__name__ + str(ctx.exception))
        # Scored through evaluate_build the leak earns 0.0 with the error recorded.
        rewards, errors = independent.evaluate_build(
            self.task, self.gold, {MART_NAME: leak}, ws
        )
        self.assertEqual(set(rewards.values()), {0.0})
        self.assertTrue(all("Permission" in e for e in errors.values()), errors)

    def test_implementer_sql_cannot_write_files(self) -> None:
        ws = self.fresh_workspace()
        target = ws / "pwned.csv"
        sql = f"COPY (SELECT 1 AS x) TO '{target}'"
        rewards, _ = independent.evaluate_build(
            self.task, self.gold, {MART_NAME: sql}, ws
        )
        self.assertEqual(set(rewards.values()), {0.0})
        self.assertFalse(target.exists())

    def test_reference_sql_still_agrees_under_the_sandbox(self) -> None:
        ws = self.fresh_workspace()
        rewards, errors = independent.evaluate_build(
            self.task, self.gold, {MART_NAME: REFERENCE_SQL}, ws
        )
        self.assertEqual(errors, {})
        self.assertEqual(set(rewards.values()), {1.0})


# ---------------------------------------------------------------------------
# CLI wiring: provider unavailability => red gates, never a crash or a pass
# ---------------------------------------------------------------------------

class TestCliEnsureIndependentBuild(IndependentTestCase):
    def test_replay_only_without_transcripts_records_nothing(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.engine import Engine
        from elt_taskgen.review import providers as providers_mod

        ws = self.fresh_workspace()
        engine = Engine(ws)
        self.addCleanup(engine.close)
        provider = providers_mod.RoutedProvider(
            providers_mod.load_role_routing(),
            providers_mod.TranscriptStore(ws / "transcripts"),
            providers_mod.CostMeter(),
            replay_only=True,
        )
        note = cli._ensure_independent_build(engine, self.task, self.gold, provider)
        self.assertIn("independent build not performed", note)
        self.assertIsNone(
            independent.load_build_result(ws, self.task.task_id)
        )
        # The consuming gates are RED, which is the fail-closed outcome.
        self.assertFalse(
            gates._gate_dual_build_agreement(self.task, ws).passed
        )

    def test_recorded_result_at_current_hash_is_reused(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.engine import Engine

        ws = self.fresh_workspace()
        engine = Engine(ws)
        self.addCleanup(engine.close)
        provider = ScriptedProvider([correct_response()])
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold
        )
        independent.record_build_result(ws, self.task, result)
        calls_before = len(provider.calls)
        note = cli._ensure_independent_build(engine, self.task, self.gold, provider)
        self.assertIn("reused", note)
        self.assertEqual(len(provider.calls), calls_before)  # no re-run

    def test_current_disagreement_is_pending_adjudication_not_task_failure(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.engine import (
            BLOCKED_ON_HUMAN,
            RETRY_GUARD_EXPLICIT,
            RETRY_GUARD_KEY,
            VERDICT_BLOCKED,
            Engine,
        )

        ws = self.fresh_workspace()
        engine = Engine(ws)
        self.addCleanup(engine.close)
        result = independent.IndependentBuildResult(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            status=independent.STATUS_NEEDS_ADJUDICATION,
            agreement={population.value: 0.5 for population in P},
            samples=(),
            detail="development agrees; hidden populations disagree",
        )
        independent.record_build_result(ws, self.task, result)

        outcome = cli._independent_adjudication_block(engine, self.task)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        data = outcome.payload.data
        self.assertEqual(data["failure_class"], "pending_adjudication")
        self.assertEqual(data["failure_code"], "independent_gold_disagreement")
        self.assertEqual(data["blocked_on"], BLOCKED_ON_HUMAN)
        self.assertEqual(data[RETRY_GUARD_KEY], RETRY_GUARD_EXPLICIT)
        self.assertIn("independent_build_sha256", data)

    def test_stale_disagreement_cannot_block_current_task(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.engine import Engine

        ws = self.fresh_workspace()
        engine = Engine(ws)
        self.addCleanup(engine.close)
        stale = independent.IndependentBuildResult(
            task_id=self.task.task_id,
            task_content_hash="0" * 64,
            status=independent.STATUS_NEEDS_ADJUDICATION,
            agreement={population.value: 0.5 for population in P},
            samples=(),
        )
        independent.record_build_result(ws, self.task, stale)
        self.assertIsNone(cli._independent_adjudication_block(engine, self.task))


class TestExecutionBounds(IndependentTestCase):
    """Roadmap Phase 0.A: the implementer's SQL runs under the semantic
    scorer's envelope (memory, threads, no temp spill, deterministic
    settings, row/byte caps) inside a spawned, killable worker."""

    def test_independent_and_calibration_pass_sandbox_caps(self) -> None:
        from unittest import mock

        from elt_taskgen.corpus import calibration as cal
        from elt_taskgen.models import TaskVariant as V
        from elt_taskgen.reference import duckdb_sandbox
        from elt_taskgen.semantic.models import SemanticLimits

        expected = {
            "memory_limit_mb": 512,
            "threads": 1,
            "disable_temp_spill": True,
            "deterministic_settings": True,
        }
        limits = SemanticLimits()
        # calibration cannot import semantic.models (it is imported BY it), so
        # its bounds are module constants pinned to the SoT anchors here.
        self.assertEqual(cal.SANDBOX_MEMORY_LIMIT_MB, limits.memory_limit_mb)
        self.assertEqual(cal.SANDBOX_THREADS, limits.threads)
        self.assertEqual(cal.MAX_RESULT_ROWS_PER_MART, limits.max_result_rows_per_mart)
        self.assertEqual(cal.MAX_RESULT_BYTES_PER_MART, limits.max_result_bytes_per_mart)

        real = duckdb_sandbox.sandboxed_memory_connection

        def recorder(seen: list[dict]):
            def factory(**kwargs):
                seen.append(dict(kwargs))
                con = real(**kwargs)
                duckdb_sandbox.assert_sandboxed(con)
                return con

            return factory

        ws = self.fresh_workspace()
        # independent.py:448 (_execute_population, the per-population body of
        # the build worker) and :938 (evaluate_load_build).
        correct_plan = {
            "customers": {"path": "postgres/customers.sql", "format": "postgres_sql"},
            "orders": {"path": "mongodb/orders.jsonl", "format": "jsonl"},
            "order_items": {"path": "files/order_items.csv", "format": "csv"},
        }
        load_plan = independent.parse_load_plan(
            self.task, json.dumps({"load_plan": correct_plan})
        )
        seen_independent: list[dict] = []
        with mock.patch.object(
            independent, "sandboxed_memory_connection", recorder(seen_independent)
        ):
            counts, rows = independent._execute_population(
                self.task, {MART_NAME: REFERENCE_SQL}, P.PRIMARY, ws
            )
            self.assertTrue(counts)
            self.assertTrue(rows[MART_NAME])
            load_rewards, load_errors = independent.evaluate_load_build(
                self.task, self.gold, load_plan, ws
            )
        self.assertEqual(load_errors, {})
        self.assertEqual(set(load_rewards.values()), {1.0})
        self.assertEqual(len(seen_independent), 1 + len(P))
        for kwargs in seen_independent:
            self.assertEqual(kwargs, expected)

        # calibration.py:874 (_preflight_harness) and :925 (_score_attempt).
        seen_calibration: list[dict] = []
        with mock.patch.object(
            cal, "sandboxed_memory_connection", recorder(seen_calibration)
        ):
            cal._preflight_harness(self.task, self.gold, ws, V.TRANSFORM)
            preflight_calls = len(seen_calibration)
            submission = cal.SolverSubmission(
                variant=V.TRANSFORM, sql_by_mart={MART_NAME: REFERENCE_SQL}
            )
            rewards, failed_stage, error = cal._score_attempt(
                self.task, self.gold, V.TRANSFORM, submission, ws
            )
        self.assertEqual(preflight_calls, len(P))
        self.assertEqual(len(seen_calibration), 2 * len(P))
        for kwargs in seen_calibration:
            self.assertEqual(kwargs, expected)
        # deterministic_settings and the caps change NO measured reward.
        self.assertEqual((failed_stage, error), ("", ""))
        self.assertEqual(set(rewards.values()), {1.0})
        rewards, errors = independent.evaluate_build(
            self.task, self.gold, {MART_NAME: REFERENCE_SQL}, ws
        )
        self.assertEqual(errors, {})
        self.assertEqual(set(rewards.values()), {1.0})

        # The caps bite: a mart that returns more rows than the cap is an
        # execution error scored 0.0, on both paths.
        flood = (
            "SELECT r.customer_id, r.completed_order_count, r.total_spend "
            f"FROM ({REFERENCE_SQL}) r CROSS JOIN range(200000)"
        )
        tight = SemanticLimits(max_result_rows_per_mart=10)
        with self.assertRaisesRegex(ValueError, "more than the allowed 10 rows"):
            independent._execute_population(
                self.task, {MART_NAME: REFERENCE_SQL}, P.PRIMARY, ws, limits=tight
            )
        rewards, errors = independent.evaluate_build(
            self.task, self.gold, {MART_NAME: flood}, ws
        )
        self.assertEqual(set(rewards.values()), {0.0})
        self.assertTrue(
            all("more than the allowed" in e for e in errors.values()), errors
        )

    def test_recursive_cte_in_implementer_sql_times_out_with_stable_code(self) -> None:
        """A recursive-CTE mart in the implementer path returns the stable
        ``execution_timeout`` code for every population instead of hanging;
        no DuckDB text, no reward above zero, bounded wall time."""
        import time

        from elt_taskgen.semantic.models import SemanticLimits

        ws = self.fresh_workspace()
        hang = (
            "WITH RECURSIVE forever(n) AS ("
            "SELECT 1 UNION ALL SELECT n + 1 FROM forever WHERE n < 1e18"
            ") SELECT customer_id, completed_order_count, total_spend "
            f"FROM ({REFERENCE_SQL}) WHERE customer_id IN (SELECT n FROM forever)"
        )
        limits = SemanticLimits(timeout_seconds=0.5)
        start = time.monotonic()
        rewards, errors = independent.evaluate_build(
            self.task, self.gold, {MART_NAME: hang}, ws, limits=limits
        )
        elapsed = time.monotonic() - start
        self.assertEqual(set(rewards), {p.value for p in P})
        self.assertEqual(set(rewards.values()), {0.0})
        self.assertEqual(
            errors, {p.value: independent.EXECUTION_TIMEOUT_CODE for p in P}
        )
        self.assertEqual(independent.EXECUTION_TIMEOUT_CODE, "execution_timeout")
        # Five populations, one second each, plus worker respawns.
        self.assertLess(elapsed, 5 * limits.timeout_seconds + 30.0)
        # The same submission through run_independent_build records the code
        # (never text) in the evidence and needs adjudication, not a crash.
        from unittest import mock

        provider = ScriptedProvider([json.dumps({MART_NAME: hang})] * 2)
        bounded = independent.evaluate_build
        with mock.patch.object(
            independent,
            "evaluate_build",
            lambda *a, **k: bounded(*a, limits=limits, **k),
        ):
            result = independent.run_independent_build(
                self.task, ws, provider, self.gold
            )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        for sample in result.samples:
            self.assertEqual(
                set(sample.errors.values()), {independent.EXECUTION_TIMEOUT_CODE}
            )


class _FakeWorker:
    """A stand-in for the spawned build worker so the supervisor loop can be
    driven from a test (a real spawn cannot be made to die on cue)."""

    pid = None

    def __init__(self, *, alive: bool = True):
        self.alive = alive
        self.closed = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False

    def join(self, timeout=None) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class TestBuildWorkerSupervision(IndependentTestCase):
    """Roadmap 0.A / taxonomy §1: the build worker's HARNESS stops (a worker
    death, the trusted load's own deadline) are infrastructure, never a
    candidate score; only stops on the CANDIDATE clock are codes."""

    def test_worker_death_is_a_harness_fault_not_a_zero_reward(self) -> None:
        import multiprocessing
        from unittest import mock

        from elt_taskgen import engine as engine_mod
        from elt_taskgen.review.session import SandboxFault
        from elt_taskgen.semantic.models import SemanticLimits

        limits = SemanticLimits(timeout_seconds=0.5)
        # 1. The pipe closes before `done` (OOM kill, SIGKILL, C++ abort).
        receive, send = multiprocessing.Pipe(duplex=False)
        send.send(("loaded", "development"))
        send.send(("population", "development", 1.0, ""))
        send.close()
        with self.assertRaises(SandboxFault) as ctx:
            independent._supervise_build_worker(_FakeWorker(alive=False), receive, limits)
        self.assertEqual(ctx.exception.code, independent.WORKER_FAILED_CODE)
        self.assertEqual(ctx.exception.boundary, "sandbox")
        self.assertEqual(engine_mod._infra_marker_for(ctx.exception), "SandboxFault")
        self.assertFalse(ctx.exception.label_eligible)
        # 2. The worker's own crash report.
        receive, send = multiprocessing.Pipe(duplex=False)
        send.send(("worker", independent.WORKER_FAILED_CODE))
        with self.assertRaises(SandboxFault):
            independent._supervise_build_worker(_FakeWorker(), receive, limits)
        # 3. Nothing downstream turns it into a 0.0 or an adjudication: the
        # fault propagates out of evaluate_build and run_independent_build,
        # and no build result is recorded.
        ws = self.fresh_workspace()
        with mock.patch.object(
            independent, "_run_build_worker",
            side_effect=SandboxFault("worker died", code=independent.WORKER_FAILED_CODE),
        ):
            with self.assertRaises(SandboxFault):
                independent.evaluate_build(self.task, self.gold, {MART_NAME: REFERENCE_SQL}, ws)
            provider = ScriptedProvider([json.dumps({MART_NAME: REFERENCE_SQL})])
            with self.assertRaises(SandboxFault):
                independent.run_independent_build(self.task, ws, provider, self.gold)
        self.assertIsNone(independent.load_build_result(ws, self.task.task_id))
        # A CANDIDATE stop is still a code: memory on the candidate clock.
        receive, send = multiprocessing.Pipe(duplex=False)
        send.send(("loaded", "development"))
        worker = _FakeWorker()
        worker.pid = 1
        with mock.patch.object(independent, "_process_rss_bytes", return_value=10 ** 12):
            finished, stopped = independent._supervise_build_worker(worker, receive, limits)
        self.assertEqual((finished, stopped), ([], independent.MEMORY_LIMIT_CODE))

    def test_candidate_deadline_starts_after_trusted_load(self) -> None:
        import multiprocessing
        import threading
        import time

        from elt_taskgen.review.session import ToolDeadlineExceeded
        from elt_taskgen.semantic.models import SemanticLimits

        limits = SemanticLimits(timeout_seconds=0.3)
        # Silence longer than the candidate deadline BEFORE `loaded` is not a
        # candidate timeout; once `loaded` arrives the candidate clock runs.
        receive, send = multiprocessing.Pipe(duplex=False)

        def feeder():
            time.sleep(0.6)
            send.send(("loaded", "development"))

        threading.Thread(target=feeder, daemon=True).start()
        start = time.monotonic()
        finished, stopped = independent._supervise_build_worker(
            _FakeWorker(), receive, limits, load_deadline_s=5.0
        )
        elapsed = time.monotonic() - start
        self.assertEqual((finished, stopped), ([], independent.EXECUTION_TIMEOUT_CODE))
        self.assertGreater(elapsed, 0.6 + limits.timeout_seconds - 0.05)
        # The load deadline itself is the harness's: a typed fault, no code.
        receive, send = multiprocessing.Pipe(duplex=False)
        with self.assertRaises(ToolDeadlineExceeded) as ctx:
            independent._supervise_build_worker(
                _FakeWorker(), receive, limits, load_deadline_s=0.2
            )
        self.assertEqual((ctx.exception.tool, ctx.exception.deadline_s), ("independent_build", 0.2))
        self.assertEqual(independent.DEFAULT_BUILD_LIMITS.timeout_seconds, 300.0)
        self.assertEqual(independent.TRUSTED_LOAD_DEADLINE_SECONDS, 300.0)
        # The real worker reports `loaded` between the trusted load and the
        # candidate SQL (so the report is what arms the clock).
        seen: list[str] = []
        counts, rows = independent._execute_population(
            self.task, {MART_NAME: REFERENCE_SQL}, P.DEVELOPMENT, self.fresh_workspace(),
            on_loaded=lambda: seen.append("loaded"),
        )
        self.assertEqual(seen, ["loaded"])
        self.assertTrue(counts and rows[MART_NAME])

    def test_evaluate_build_matches_in_process_scoring_under_limits(self) -> None:
        """Brief rule 5 / roadmap 0.A: for a build that finishes under the
        limits the spawned worker yields byte-identical `(rewards, errors)`
        to the in-process path — for a correct build and for a wrong one."""
        ws = self.fresh_workspace()
        wrong = "SELECT 1 AS customer_id"
        for label, sql in (("correct", REFERENCE_SQL), ("wrong", wrong)):
            with self.subTest(label):
                expected_rewards: dict[str, float] = {}
                expected_errors: dict[str, str] = {}
                for pop in P:
                    try:
                        counts, rows = independent._execute_population(
                            self.task, {MART_NAME: sql}, pop, ws
                        )
                        expected_rewards[pop.value] = float(
                            upstream_eval.evaluate(self.task, self.gold, pop, counts, rows).reward
                        )
                    except Exception as exc:  # noqa: BLE001 — mirrors the worker's recording
                        expected_rewards[pop.value] = 0.0
                        expected_errors[pop.value] = f"{type(exc).__name__}: {exc}"
                rewards, errors = independent.evaluate_build(self.task, self.gold, {MART_NAME: sql}, ws)
                self.assertEqual((rewards, errors), (expected_rewards, expected_errors))
        self.assertEqual(set(rewards), {p.value for p in P})


# ---------------------------------------------------------------------------
# The recorded prompt_sha256 is the key the PROVIDER files the exchange under
# ---------------------------------------------------------------------------

class TestTranscriptKeyProvenance(IndependentTestCase):
    """Phase-0 carry-over 2 (roadmap Phase 1): a sample's `prompt_sha256`
    must point at the exchange that produced it. A `RoutedProvider` keys
    over the agents document its routing was loaded from
    (`transcript_key_for`), so both builders ask the provider for the key
    and fall back to the module default only for a double without one."""

    @staticmethod
    def _custom_agents_config(root: Path) -> Path:
        import yaml

        from elt_taskgen.review import providers as providers_mod

        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        for role in (independent.ROLE_NAME, independent.LOADER_ROLE_NAME):
            doc["roles"].setdefault(role, {})["session"] = {"max_wall_s": 301}
        # Since Phase 2 the two witnesses are session-runner roles whose
        # declared-but-DISABLED block folds to `{}` in the manifest
        # (`providers.WITNESS_RUNNER_ROLES`): editing a disabled block moves
        # no key BY DESIGN, so the custom document must differ in something
        # the manifest hashes — the pinned sandbox declaration (roadmap R-F).
        doc.setdefault("metrology", {}).setdefault("sandbox", {})["image_digest"] = (
            "sha256:" + "f" * 64
        )
        path = root / "agents.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return path

    def test_recorded_prompt_sha_is_the_default_transcript_key_for_a_bare_double(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        ws = self.fresh_workspace()
        result = independent.run_independent_build(
            self.task, ws, ScriptedProvider([correct_response()]), self.gold
        )
        self.assertEqual(
            result.samples[0].prompt_sha256,
            providers_mod.transcript_key(
                independent.ROLE_NAME, independent.sample_prompt(self.task, 0)
            ),
        )
        double = ScriptedProvider([correct_response()])
        self.assertEqual(
            independent._transcript_key_for(double, independent.LOADER_ROLE_NAME, "p"),
            providers_mod.transcript_key(independent.LOADER_ROLE_NAME, "p"),
        )

    def test_recorded_prompt_sha_follows_the_provider_transcript_key(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)
        ws = self.fresh_workspace()
        custom = self._custom_agents_config(ws)

        class KeyedProvider(ScriptedProvider):
            """What a RoutedProvider on a custom --agents-config computes."""

            def transcript_key_for(self, role, prompt: str) -> str:
                role_name = getattr(role, "value", str(role))
                return providers_mod.transcript_key(role_name, prompt, agents_config=custom)

        prompt = independent.sample_prompt(self.task, 0)
        custom_key = providers_mod.transcript_key(
            independent.ROLE_NAME, prompt, agents_config=custom
        )
        self.assertNotEqual(
            custom_key, providers_mod.transcript_key(independent.ROLE_NAME, prompt)
        )
        result = independent.run_independent_build(
            self.task, ws, KeyedProvider([correct_response()]), self.gold
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(result.samples[0].prompt_sha256, custom_key)

        loader_prompt = "the loader view"
        keyed = KeyedProvider([correct_response()])
        loader_key = providers_mod.transcript_key(
            independent.LOADER_ROLE_NAME, loader_prompt, agents_config=custom
        )
        self.assertNotEqual(
            loader_key, providers_mod.transcript_key(independent.LOADER_ROLE_NAME, loader_prompt)
        )
        self.assertEqual(
            independent._transcript_key_for(keyed, independent.LOADER_ROLE_NAME, loader_prompt),
            loader_key,
        )
        # A real RoutedProvider on that document agrees with the double.
        routing = providers_mod.load_role_routing(custom)
        routed = providers_mod.RoutedProvider(
            routing,
            providers_mod.TranscriptStore(ws / "transcripts"),
            providers_mod.CostMeter(budget_per_task_usd=1.0),
        )
        self.assertEqual(
            independent._transcript_key_for(routed, independent.ROLE_NAME, prompt), custom_key
        )
        self.assertEqual(
            independent._transcript_key_for(routed, independent.LOADER_ROLE_NAME, loader_prompt),
            loader_key,
        )


# ---------------------------------------------------------------------------
# The witness SESSION (roadmap Phase 2, 2.a; SoT T1 IMP): one bounded session
# per sample, the certifier untouched, nothing measured fed back
# ---------------------------------------------------------------------------

try:  # `unittest discover -s tests` puts tests/ on sys.path; -m tests.x does not
    from test_bounded_session import ScriptedProvider as ScriptedTurns
    from test_bounded_session import tool_use as tool_use_block
except ImportError:  # pragma: no cover - depends on how the suite is invoked
    from tests.test_bounded_session import ScriptedProvider as ScriptedTurns
    from tests.test_bounded_session import tool_use as tool_use_block

#: The one-shot evidence shape of a sample (what a cold-resample build wrote
#: before Phase 2 and must still write, byte for byte).
_ONE_SHOT_SAMPLE_KEYS = frozenset(
    {"sample_index", "prompt_sha256", "sql_by_mart", "rewards", "errors", "dev_pass"}
)


def _session_limits(block: dict | None = None):
    from elt_taskgen.review.tools import validators as witness_tools

    return witness_tools.implementer_limits(dict(block or _IMPLEMENTER_SESSION_BLOCK))


def _session_policy(block: dict | None = None):
    from elt_taskgen.review.tools import validators as witness_tools

    return witness_tools.implementer_policy(_session_limits(block))


def submit_call(sql: str, id_: str = "tsub") -> dict:
    return tool_use_block(
        "submit_sql_by_mart", {"sql_by_mart": [{"mart": MART_NAME, "sql": sql}]}, id_
    )


class SessionDouble:
    """An offline provider that runs the REAL bounded runner over a scripted
    FIFO of turns the way `RoutedProvider.run_session` does: the witness's
    tools dispatch in-process against the real projections on the fixture
    workspace (04 §7: no new doubling mechanism). `complete` is refused so a
    test proves the session path was taken. A bare double: it opts into the
    cross-family guard explicitly (`unrouted_test_double`)."""

    unrouted_test_double = True
    agents_config = None

    def __init__(self, *scripts):
        self.scripts = [list(s) for s in scripts]
        self.transports: list[ScriptedTurns] = []
        self.sessions: list[dict] = []
        self.completes = 0

    def complete(self, role, prompt):
        self.completes += 1
        raise AssertionError("one-shot complete() must not run when the session is enabled")

    def run_session(self, role, view, policy, ctx, **kwargs):
        from elt_taskgen.review import session as S

        role_name = getattr(role, "value", str(role))
        if not self.scripts:
            raise AssertionError("SessionDouble ran out of scripted sessions")
        transport = ScriptedTurns(self.scripts.pop(0))
        self.transports.append(transport)
        self.sessions.append({"role": role_name, "view": view, "policy": policy, "ctx": ctx})
        kwargs.setdefault("worker", None)
        return S.run_bounded_session(
            role_name, view, policy.tools, policy, policy.limits,
            provider=transport, ctx=ctx, **kwargs,
        )

    def messages_seen(self) -> list[str]:
        """Every model-bound message prefix of every session, as JSON text,
        WITHOUT the initial view (the factory prompt, pinned by the prompt
        contract tests): the assistant turns and the harness's tool results
        — everything the session itself put in front of the model."""
        out: list[str] = []
        for transport in self.transports:
            for call in transport.calls:
                out.append(json.dumps(call["messages"][1:]))
        return out

    def views_seen(self) -> list[str]:
        """The initial view of every session, in order."""
        return [str(s["view"]) for s in self.sessions]


def _concurrent_implementer_session_worker(
    task_json: str,
    workspace: str,
    start,
    results,
    index: int,
) -> None:
    """Spawn-safe real-session worker for the scratch-isolation regression."""
    task = TaskIR.model_validate_json(task_json)
    provider = SessionDouble([[submit_call(REFERENCE_SQL, f"submit-{index}")]])
    if not start.wait(timeout=20):
        raise TimeoutError("concurrent implementer start was not released")
    response, _key, _provenance = independent._implementer_session(  # noqa: SLF001
        task,
        Path(workspace),
        provider,
        _session_policy(),
        0,
    )
    ctx = provider.sessions[0]["ctx"]
    results.put(
        {
            "index": index,
            "submitted": response is not None,
            "root": str(ctx.root),
            "warehouse": str(ctx.session.dev_warehouse),
            "root_exists": ctx.root.exists(),
            "warehouse_exists": ctx.session.dev_warehouse.exists(),
        }
    )


class OneShotOnlyDouble(ScriptedProvider):
    """A one-shot double that COULD run a session (`run_session` exists, so
    the enablement gate is consulted) but must never enter one under the
    shipped configuration."""

    agents_config = None

    def __init__(self, responses):
        super().__init__(responses)
        self.sessions = 0

    def run_session(self, *args, **kwargs):
        self.sessions += 1
        raise AssertionError("the witness session must not run under the shipped config")


def _chat_tool_call_body(name: str, arguments: dict, *, call_id: str = "call_01",
                         model: str = "test-oss-model") -> dict:
    """One chat-completions turn making ONE tool call (the openai_compat
    wire the cross-family witnesses are routed to)."""
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 800, "completion_tokens": 150},
    }


class _FakeTransport:
    """Transport double: canned HTTP bodies, call recording, no network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, dict(headers), json.loads(json.dumps(payload))))
        if not self.responses:
            raise AssertionError("unexpected HTTP call (transport exhausted)")
        return self.responses.pop(0)


def _witness_routing(*, implementer_provider: str = "openai_compat",
                     implementer_model: str = "test-oss-model"):
    """A routing with the two witnesses on the openai_compat wire (or the
    implementer on a same-family route, for the refusal case)."""
    from elt_taskgen.review import providers as providers_mod

    roles = {
        "semantic_author": providers_mod.RoleRoute(
            "semantic_author", "anthropic", "claude-opus-5", 1024, "high"
        ),
        independent.ROLE_NAME: providers_mod.RoleRoute(
            independent.ROLE_NAME, implementer_provider, implementer_model, 4096, None
        ),
        independent.LOADER_ROLE_NAME: providers_mod.RoleRoute(
            independent.LOADER_ROLE_NAME, "openai_compat", "test-oss-model", 4096, None
        ),
    }
    return providers_mod.RoleRouting(
        roles=roles,
        provider_config={
            "anthropic": {"api_key": "sk-test"},
            "openai_compat": {
                "base_url": "http://gateway.test/v1",
                "api_key": "k",
                "model": "test-oss-model",
                "usd_per_mtok_input": 0.70,
                "usd_per_mtok_output": 3.50,
            },
        },
        source="(test routing)",
    )


def _routed_session_provider(task: TaskIR, store_dir: Path, transport, *,
                             replay_only: bool = False, routing=None):
    from elt_taskgen.review import providers as providers_mod

    provider = providers_mod.RoutedProvider(
        routing or _witness_routing(),
        providers_mod.TranscriptStore(store_dir),
        providers_mod.CostMeter(budget_per_task_usd=100.0),
        task_id=task.task_id,
        replay_only=replay_only,
        transports={"openai_compat": transport, "anthropic": transport},
    )
    provider.begin_task_evidence(task.task_id, task.content_hash())
    return provider


def _tree_digest(root: Path) -> dict[str, str]:
    import hashlib

    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestImplementerSession(IndependentTestCase):
    """The implementer witness as a bounded session: `MAX_SAMPLES` sessions
    per build, the session salt as the sample index, the artifact handed to
    the UNCHANGED certifier after the session closed, nothing the certifier
    measures ever inside a message, cross-family on every turn (C6), a
    harness fault a could-not-measure (C7), and an explicit disabled rollback
    byte-identical to the cold-resample loop."""

    def setUp(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)

    def test_pipeline_workspace_under_repository_runs_uses_scratch_tool_root(self) -> None:
        """The shipped default workspace is ``<checkout>/runs/...`` while
        A22 deliberately refuses that tree as a ToolContext root.  The
        witness must keep the refusal intact and put only the model-facing
        generic path root in disposable scratch."""
        from unittest import mock

        from elt_taskgen import workspace as workspace_mod
        from elt_taskgen.review.tools import registry as registry_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        checkout = Path(tmp.name) / "checkout"
        ws = checkout / "runs" / "pipeline"
        shutil.copytree(self.base_workspace, ws)
        provider = SessionDouble([[submit_call(REFERENCE_SQL)]])

        with mock.patch.object(workspace_mod, "repo_root", lambda: checkout):
            self.assertTrue(registry_mod.path_under_runs(ws))
            with self.assertRaises(registry_mod.ToolPathDenied):
                registry_mod.ToolContext(
                    root=ws, task_id=self.task.task_id, role=independent.ROLE_NAME
                )
            result = independent.run_independent_build(
                self.task, ws, provider, self.gold, session_policy=_session_policy()
            )
            ctx = provider.sessions[0]["ctx"]
            self.assertFalse(registry_mod.path_under_runs(ctx.root))

        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertNotEqual(ctx.root, ws.resolve())
        self.assertFalse(ctx.root.exists(), "per-session tool scratch must be removed")

    def test_session_disabled_witness_is_byte_identical_to_cold_resample(self) -> None:
        from unittest import mock

        from elt_taskgen.review import providers as providers_mod

        ws = self.fresh_workspace()
        bad = json.dumps({MART_NAME: "SELECT 1 AS nope"})
        # The explicit rollback: `roles.independent_implementer.session.enabled`
        # is false, so a provider that COULD run a session never does, the
        # one-shot exchanges are exactly the salted `sample_prompt`s, and the
        # recorded evidence is byte for byte the cold-resample loop's.
        self.assertTrue(providers_mod.role_loop_limits(independent.ROLE_NAME)["enabled"])
        disabled = json.loads(json.dumps(providers_mod._agents_doc()))
        disabled["roles"][independent.ROLE_NAME]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: disabled):
            providers_mod.clear_behavior_caches()
            self.assertFalse(providers_mod.role_loop_limits(independent.ROLE_NAME)["enabled"])
            gated = OneShotOnlyDouble([bad, correct_response()])
            self.assertIsNone(independent._witness_session_block(gated, independent.ROLE_NAME))
            result = independent.run_independent_build(self.task, ws, gated, self.gold)
            self.assertEqual(gated.sessions, 0)
            self.assertEqual(
                gated.calls,
                [(independent.ROLE_NAME, independent.sample_prompt(self.task, i)) for i in range(2)],
            )
            self.assertEqual(result.status, independent.STATUS_AGREED)
            plain = ScriptedProvider([bad, correct_response()])
            reference = independent.run_independent_build(self.task, ws, plain, self.gold)
            self.assertEqual(result, reference)
            path = independent.record_build_result(ws, self.task, result)
            recorded = path.read_bytes()
            independent.record_build_result(ws, self.task, reference)
            self.assertEqual(recorded, path.read_bytes())
            document = json.loads(recorded)
            for sample in document["samples"]:
                self.assertEqual(set(sample), _ONE_SHOT_SAMPLE_KEYS)
                for key in independent.SESSION_SAMPLE_KEYS:
                    self.assertNotIn(key, sample)
        # The record shape the gates read is unchanged (no result-level field).
        self.assertEqual(
            set(independent.IndependentBuildResult.model_fields),
            {"task_id", "task_content_hash", "role", "status", "agreement",
             "samples", "detail", "provider", "model", "endpoint_host"},
        )
        # Re-loading the one-shot record yields defaulted session fields.
        loaded = independent.IndependentBuildResult.model_validate(document)
        self.assertEqual(loaded.samples[0].terminal, "")
        self.assertEqual(loaded.samples[0].tool_calls, ())
        self.assertEqual(loaded.samples[0].limits, {})
        # The gate is decided by the CONFIG alone: enabling the block in the
        # agents document flips the same provider into the session path.
        providers_mod.clear_behavior_caches()
        block = independent._witness_session_block(gated, independent.ROLE_NAME)
        self.assertIsNotNone(block)
        self.assertTrue(block["enabled"])
        self.assertEqual(block["max_turns"], 12)  # 12 since 2026-09-11 run Q (was 4, 6, 8)
        with self.assertRaises(AssertionError):
            independent.run_independent_build(
                self.task, ws, OneShotOnlyDouble([correct_response()]), self.gold
            )
        # A provider WITHOUT run_session stays one-shot even under the default.
        self.assertIsNone(
            independent._witness_session_block(ScriptedProvider([]), independent.ROLE_NAME)
        )

    def test_implementer_never_sees_dev_pass_or_expected_rows(self) -> None:
        from unittest import mock

        from elt_taskgen.review import session as S
        from elt_taskgen.review.tools.projection import DIAGNOSTICS_VERSION

        ws = self.fresh_workspace()
        script = [
            [tool_use_block("list_schemas", {}, "t0")],
            [tool_use_block("dev_query", {"sql": "SELECT * FROM customers ORDER BY customer_id"}, "t1")],
            [tool_use_block("dry_run_sql", {"mart": MART_NAME, "sql": REFERENCE_SQL}, "t2")],
            [tool_use_block("run_mart_sql_dev", {"sql_by_mart": [{"mart": MART_NAME, "sql": REFERENCE_SQL}]}, "t3")],
            [submit_call(REFERENCE_SQL)],
        ]
        provider = SessionDouble(script)
        policy = _session_policy({**_IMPLEMENTER_SESSION_BLOCK, "max_turns": 6})
        certified: list[dict] = []
        real_evaluate = independent.evaluate_build

        def evaluate_after_the_session(task, gold, sql_by_mart, workspace, **kwargs):
            # CERTIFY is entered only after the session has closed (04 §1): by
            # now every scripted turn has been consumed and no message can
            # still be sent.
            self.assertEqual(provider.transports[-1].script, [])
            certified.append(dict(sql_by_mart))
            return real_evaluate(task, gold, sql_by_mart, workspace, **kwargs)

        with mock.patch.object(independent, "evaluate_build", evaluate_after_the_session):
            result = independent.run_independent_build(
                self.task, ws, provider, self.gold, session_policy=policy
            )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(provider.completes, 0)
        self.assertEqual(certified, [{MART_NAME: REFERENCE_SQL}])
        self.assertEqual(len(result.samples), 1)
        sample = result.samples[0]
        self.assertTrue(sample.dev_pass)
        self.assertEqual(sample.terminal, S.TerminalState.SUBMITTED.name)
        self.assertEqual(sample.turns, 5)
        self.assertEqual(
            [c.name for c in sample.tool_calls],
            ["list_schemas", "dev_query", "dry_run_sql", "run_mart_sql_dev", "check_submission"],
        )
        for call in sample.tool_calls:
            self.assertRegex(call.args_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(call.output_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual(call.sanitizer_version, DIAGNOSTICS_VERSION)
        # NOTHING the certifier measures reaches a message: not `dev_pass`,
        # not a reward, not an `expected N rows, got M`, not a gold value,
        # not the answer key — in any turn's prefix beyond the initial view
        # (the factory prompt, pinned by the prompt-contract tests), tool
        # results included; and the view itself names no grading result.
        messages = provider.messages_seen()
        self.assertEqual(len(messages), 5)
        forbidden = (
            "dev_pass", "expected", "reward", "answer_key", "answer key", "gold",
            "stage1", "rows, got", "agreement",
        )
        for text in messages:
            lowered = text.lower()
            for token in forbidden:
                self.assertNotIn(token, lowered, token)
        view = provider.views_seen()[0].lower()
        for token in ("dev_pass", "reward", "answer_key", "stage1", "rows, got", "gold"):
            self.assertNotIn(token, view, token)
        # Every tool result is a code-only projection (or DEVELOPMENT rows).
        last = provider.transports[-1].calls[-1]["messages"]
        results = [
            block["content"]
            for message in last
            if message.get("role") == "user" and isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(results), 4)
        for content in results:
            self.assertNotIn("dev_pass", content)
            self.assertNotIn("expected", content)
        # The one dev_query answered with DEVELOPMENT rows (the rendered
        # `DevRows` template), the rest with rendered code-only diagnostics.
        self.assertTrue(results[1].startswith("[dev_rows] "), results[1])
        for content in (results[0], results[2], results[3]):
            self.assertRegex(content, r"^\[[a-z_]+\] [a-z_]+\b")
            self.assertNotIn("[dev_rows]", content)
            self.assertIn(" ok=true", content)

    def test_every_implementer_transcript_is_cross_family_on_every_turn(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        ws = self.fresh_workspace()
        policy = _session_policy()
        dev_query = {"sql": "SELECT * FROM customers ORDER BY customer_id"}
        submit = {"sql_by_mart": [{"mart": MART_NAME, "sql": REFERENCE_SQL}]}
        # (1) A cross-family route served by the model it names: every model
        # turn's route block and served model pass the family rule.
        transport = _FakeTransport([
            _chat_tool_call_body("dev_query", dev_query, call_id="c0"),
            _chat_tool_call_body("submit_sql_by_mart", submit, call_id="c1"),
        ])
        provider = _routed_session_provider(self.task, ws / "transcripts", transport)
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold, session_policy=policy
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual((result.provider, result.model), ("openai_compat", "test-oss-model"))
        self.assertEqual(len(transport.calls), 2)
        record = provider.store.lookup_session(independent.ROLE_NAME, result.samples[0].prompt_sha256)
        self.assertIsNotNone(record)
        self.assertEqual(len(record["turn_keys"]), 2)
        for key in record["turn_keys"]:
            entry = provider.store.lookup(independent.ROLE_NAME, key)
            self.assertEqual(entry["route"]["provider"], "openai_compat")
            self.assertEqual(entry["model"], "test-oss-model")
            self.assertEqual(entry["served_model"], "test-oss-model")
        # (2) The SERVED model of one turn resolves to the Anthropic family
        # (a gateway quietly serving Claude behind a cross-family alias): the
        # session's artifact is refused BEFORE it is certified, naming the
        # turn, and nothing is recorded.
        ws2 = self.fresh_workspace()
        served_by_claude = _FakeTransport([
            _chat_tool_call_body("dev_query", dev_query, call_id="c0"),
            _chat_tool_call_body("submit_sql_by_mart", submit, call_id="c1", model="anthropic/claude-opus-5"),
        ])
        provider2 = _routed_session_provider(self.task, ws2 / "transcripts", served_by_claude)
        with self.assertRaises(ValueError) as ctx:
            independent.run_independent_build(
                self.task, ws2, provider2, self.gold, session_policy=policy
            )
        message = str(ctx.exception)
        self.assertIn("cross-family", message)
        self.assertIn("anthropic/claude-opus-5", message)
        self.assertIn("session turn 1", message)
        self.assertIsNone(independent.load_build_result(ws2, self.task.task_id))
        # (3) A same-family ROUTE is refused before any session: zero turns.
        ws3 = self.fresh_workspace()
        untouched = _FakeTransport([])
        same_family = _routed_session_provider(
            self.task, ws3 / "transcripts", untouched,
            routing=_witness_routing(implementer_provider="anthropic", implementer_model="claude-opus-5"),
        )
        with self.assertRaises(ValueError) as ctx:
            independent.run_independent_build(
                self.task, ws3, same_family, self.gold, session_policy=policy
            )
        self.assertIn("cross-family", str(ctx.exception))
        self.assertEqual(untouched.calls, [])
        # (4) The per-turn rule is the same family rule the route obeys, and
        # it reads the ROUTE BLOCK of each recorded turn: a turn recorded
        # under an anthropic route block is refused whatever the store says.
        from elt_taskgen.review import session as S

        clean = provider.store.lookup_session(independent.ROLE_NAME, result.samples[0].prompt_sha256)
        turns = []
        for index, key in enumerate(clean["turn_keys"]):
            entry = provider.store.lookup(independent.ROLE_NAME, key)
            turns.append(S.TurnRecord(
                turn_index=index, kind="model", model_turn=index, category="tool_call",
                memo_key=key, route=dict(entry["route"]),
            ))
        fake = types.SimpleNamespace(turns=tuple(turns))
        independent._assert_turns_cross_family(provider, independent.ROLE_NAME, fake)  # no raise
        bad_route = dict(turns[-1].route)
        bad_route["provider"] = "anthropic"
        bad_turn = S.TurnRecord(
            turn_index=1, kind="model", model_turn=1, category="terminal", route=bad_route,
        )
        with self.assertRaises(ValueError) as ctx:
            independent._assert_turns_cross_family(
                provider, independent.ROLE_NAME, types.SimpleNamespace(turns=(turns[0], bad_turn))
            )
        self.assertIn("session turn 1", str(ctx.exception))
        # An opted-in unrouted double records no route block and is admitted
        # by `_assert_cross_family` alone.
        independent._assert_turns_cross_family(
            SessionDouble(), independent.ROLE_NAME, types.SimpleNamespace(turns=(bad_turn,))
        )
        providers_mod.clear_behavior_caches()

    def test_implementer_certifier_unchanged_parse_sql_by_mart_then_evaluate_build(self) -> None:
        from unittest import mock

        from elt_taskgen.models import canonical_json
        from elt_taskgen.review import session as S

        ws = self.fresh_workspace()
        policy = _session_policy()
        order: list[str] = []
        real_parse, real_evaluate = independent.parse_sql_by_mart, independent.evaluate_build

        def parse(task, text):
            order.append("parse_sql_by_mart")
            parse.seen.append(text)
            return real_parse(task, text)

        def evaluate(task, gold, sql_by_mart, workspace, **kwargs):
            order.append("evaluate_build")
            evaluate.seen.append(dict(sql_by_mart))
            return real_evaluate(task, gold, sql_by_mart, workspace, **kwargs)

        parse.seen, evaluate.seen = [], []  # type: ignore[attr-defined]
        provider = SessionDouble([[submit_call(REFERENCE_SQL)]])
        with mock.patch.object(independent, "parse_sql_by_mart", parse), mock.patch.object(
            independent, "evaluate_build", evaluate
        ):
            result = independent.run_independent_build(
                self.task, ws, provider, self.gold, session_policy=policy
            )
        # The certifier: `parse_sql_by_mart` over the submitted artifact AS
        # TEXT (exactly the one-shot input shape), then `evaluate_build` over
        # the parsed mapping — nothing else, in that order, once.
        self.assertEqual(order, ["parse_sql_by_mart", "evaluate_build"])
        self.assertEqual(parse.seen, [canonical_json({MART_NAME: REFERENCE_SQL})])
        self.assertEqual(evaluate.seen, [{MART_NAME: REFERENCE_SQL}])
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(result.agreement, {p.value: 1.0 for p in P})
        self.assertEqual(result.samples[0].sql_by_mart, {MART_NAME: REFERENCE_SQL})
        self.assertEqual(result.samples[0].terminal, S.TerminalState.SUBMITTED.name)
        self.assertEqual(result.samples[0].turns, 1)
        # The harness dry-runs the submission (`check_submission`, 2026-09-11).
        self.assertEqual([c.name for c in result.samples[0].tool_calls], ["check_submission"])
        # The session-mode evidence carries the provenance and still passes
        # the gates' readers (which consume only the one-shot keys).
        independent.record_build_result(ws, self.task, result)
        loaded = independent.load_build_result(ws, self.task.task_id)
        sample = loaded["samples"][0]
        self.assertTrue(_ONE_SHOT_SAMPLE_KEYS <= set(sample))
        self.assertEqual(sample["terminal"], "SUBMITTED")
        self.assertEqual(sample["limits"], policy.limits.as_manifest())
        self.assertEqual(sample["manifest_sha256"], policy.tools_sha256())
        self.assertEqual(sample["policy_sha256"], policy.sha256())
        self.assertRegex(sample["session_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(gates._gate_dual_build_agreement(self.task, ws).passed)
        # The adjudication rule is the one-shot rule, unchanged: a build that
        # passes the public examples and disagrees on hidden gold is ONE
        # session and NEEDS_ADJUDICATION, never resampled away ...
        wrong_gold = corrupt_gold_value(self.gold, P.PRIMARY.value, MART_NAME)
        provider = SessionDouble([[submit_call(REFERENCE_SQL)]], [[submit_call(REFERENCE_SQL)]])
        result = independent.run_independent_build(
            self.task, ws, provider, wrong_gold, session_policy=policy
        )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(result.samples), 1)
        self.assertTrue(result.samples[0].dev_pass)
        self.assertLess(result.agreement[P.PRIMARY.value], 1.0)
        self.assertEqual(len(provider.transports), 1)
        # ... while an implementer-side failure (fails the public examples
        # too) is resampled as a SECOND session under the next salt, with its
        # own session key, bounded by MAX_SAMPLES.
        provider = SessionDouble(
            [[submit_call("SELECT customer_id, 0 AS completed_order_count, 0 AS total_spend FROM customers")]], [[submit_call(REFERENCE_SQL)]]
        )
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold, session_policy=policy
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(len(result.samples), 2)
        self.assertFalse(result.samples[0].dev_pass)
        self.assertEqual(
            [s["policy"].session_salt for s in provider.sessions], [0, 1]
        )
        self.assertNotEqual(result.samples[0].prompt_sha256, result.samples[1].prompt_sha256)
        self.assertTrue(provider.sessions[1]["view"].endswith(
            independent.sample_prompt(self.task, 1)[len(independent.implementer_view(self.task)):]
        ))
        # An ABSTAINED session is recorded (terminal, reason, no artifact) and
        # resampled; when every session ends so, the build needs adjudication
        # with the terminal named — a red gate, never a crash.
        provider = SessionDouble(
            [[tool_use_block("abort", {"reason_code": "infeasible"}, "ta")]],
            [[tool_use_block("abort", {"reason_code": "spec_conflict"}, "tb")]],
            # MAX_SAMPLES is 3 since 2026-09-11: a third session ends the same way.
            [[tool_use_block("abort", {"reason_code": "spec_conflict"}, "tc")]],
        )
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold, session_policy=policy
        )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual([s.terminal for s in result.samples], ["ABSTAINED", "ABSTAINED", "ABSTAINED"])
        self.assertEqual([s.abort_reason for s in result.samples], ["infeasible", "spec_conflict", "spec_conflict"])
        self.assertEqual(result.samples[-1].sql_by_mart, {})
        self.assertEqual(result.agreement, {})
        self.assertIn("ABSTAINED", result.detail)
        self.assertIn("spec_conflict", result.detail)
        independent.record_build_result(ws, self.task, result)
        self.assertFalse(gates._gate_dual_build_agreement(self.task, ws).passed)
        self.assertIsNotNone(independent.load_adjudication(ws, self.task.task_id))

    def test_implementer_session_gate_then_generate_has_no_artifact_drift(self) -> None:
        """Regression for the live Twitter repair loop (reports 140/166/184).

        The real bounded implementer session used to leave
        ``populations/development/dev_warehouse.duckdb`` behind.  The EL gate
        then correctly called it a non-derived population artifact, routed a
        runtime repair, and the next implementer session recreated it forever.
        Run the witness, persist/read its real gate evidence, and re-run the
        real generator: no workspace artifact or repair surface may move.
        """
        from elt_taskgen import cli, repair
        from elt_taskgen.engine import Engine

        ws = self.fresh_workspace()
        engine = Engine(ws)
        self.addCleanup(engine.close)
        engine.register(self.task)
        before = _tree_digest(ws)
        before_repair = repair.repair_fingerprint(ws, self.task)
        before_revision = engine.load_task(self.task.task_id).revisions
        script = [
            [tool_use_block("list_schemas", {}, "t0")],
            [tool_use_block("dev_query", {"sql": "SELECT * FROM orders ORDER BY order_id"}, "t1")],
            [tool_use_block("dry_run_sql", {"mart": MART_NAME, "sql": REFERENCE_SQL}, "t2")],
            [tool_use_block("run_mart_sql_dev", {"sql_by_mart": [{"mart": MART_NAME, "sql": REFERENCE_SQL}]}, "t3")],
            [submit_call(REFERENCE_SQL)],
        ]
        provider = SessionDouble(script)
        result = independent.run_independent_build(
            self.task, ws, provider, self.gold,
            session_policy=_session_policy({**_IMPLEMENTER_SESSION_BLOCK, "max_turns": 6}),
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        after = _tree_digest(ws)
        # The warehouse lived beside (not inside) the empty generic tool root
        # and the owning TemporaryDirectory removed both before certification.
        ctx = provider.sessions[0]["ctx"]
        warehouse = ctx.session.dev_warehouse
        populations = ws / "tasks" / self.task.task_id / "populations"
        self.assertNotEqual(warehouse, ctx.root)
        self.assertNotIn(populations.resolve(), warehouse.resolve().parents)
        self.assertFalse(ctx.root.exists())
        self.assertFalse(warehouse.exists())
        self.assertEqual(after, before)
        # The session's tools declare no write surface and take no path.
        for tool in provider.sessions[0]["policy"].tools:
            self.assertFalse(getattr(tool, "surface_write", False), tool.name)
        # The recorder is the only writer.  Its report is genuine current
        # witness evidence and makes the dual-build gate green.
        independent.record_build_result(ws, self.task, result)
        written = set(_tree_digest(ws)) - set(after)
        self.assertEqual(
            written,
            {(Path("tasks") / self.task.task_id / independent.INDEPENDENT_BUILD_EVIDENCE_REL).as_posix()},
        )
        self.assertTrue(gates._gate_dual_build_agreement(self.task, ws).passed)
        # The exact drift check used before the EL battery stays green, and a
        # subsequent generate reuses all five byte-identical populations.  No
        # runtime repair/revision was manufactured by session scratch.
        self.assertIsNone(cli._population_drift_failure(engine, self.task))
        generated = cli.run_generate(engine, self.task)
        self.assertEqual(generated.verdict, "pass")
        self.assertEqual(generated.payload.data["built"], "")
        self.assertEqual(
            set(generated.payload.data["reused"].split(",")),
            {population.value for population in P},
        )
        self.assertEqual(repair.repair_fingerprint(ws, self.task), before_repair)
        self.assertEqual(engine.load_task(self.task.task_id).revisions, before_revision)
        self.assertEqual(list(populations.rglob("dev_warehouse.duckdb*")), [])

    def test_implementer_session_scratch_is_cleaned_after_fault(self) -> None:
        """An exception unwinds the owning context before it escapes."""
        ws = self.fresh_workspace()
        provider = SessionDouble([])

        def fail(role, view, policy, ctx, **kwargs):
            provider.sessions.append(
                {"role": role, "view": view, "policy": policy, "ctx": ctx}
            )
            raise RuntimeError("simulated session fault")

        provider.run_session = fail
        before = _tree_digest(ws)
        with self.assertRaisesRegex(RuntimeError, "simulated session fault"):
            independent.run_independent_build(
                self.task, ws, provider, self.gold, session_policy=_session_policy()
            )
        ctx = provider.sessions[0]["ctx"]
        self.assertFalse(ctx.root.exists())
        self.assertFalse(ctx.session.dev_warehouse.exists())
        self.assertEqual(_tree_digest(ws), before)

    def test_concurrent_implementer_sessions_have_disjoint_disposable_warehouses(self) -> None:
        """Concurrent workers share only read-only rendered source artifacts."""
        import multiprocessing

        ws = self.fresh_workspace()
        before = _tree_digest(ws)
        methods = multiprocessing.get_all_start_methods()
        mp = multiprocessing.get_context("spawn" if "spawn" in methods else methods[0])
        start = mp.Event()
        results = mp.Queue()
        processes = [
            mp.Process(
                target=_concurrent_implementer_session_worker,
                args=(self.task.model_dump_json(), str(ws), start, results, index),
            )
            for index in range(2)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=30)
            self.assertEqual([process.exitcode for process in processes], [0, 0])
            completed = [results.get(timeout=5) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
            results.join_thread()
        self.assertTrue(all(item["submitted"] for item in completed))
        roots = {Path(item["root"]) for item in completed}
        warehouses = {Path(item["warehouse"]) for item in completed}
        self.assertEqual(len(roots), 2)
        self.assertEqual(len(warehouses), 2)
        self.assertTrue(all(not item["root_exists"] for item in completed))
        self.assertTrue(all(not item["warehouse_exists"] for item in completed))
        self.assertTrue(all(not path.exists() for path in roots | warehouses))
        self.assertEqual(_tree_digest(ws), before)
        self.assertEqual(
            list((ws / "tasks" / self.task.task_id / "populations").rglob("dev_warehouse.duckdb*")),
            [],
        )

    def test_record_transcripts_seeds_whole_trajectories(self) -> None:
        """The provider-level half of the roadmap's `cmd_record_transcripts`
        seam (the CLI half — the same entry point run against the frozen
        reference when the witness block is enabled — is pinned in
        tests/test_providers.py under this name): a session build through a
        recording `RoutedProvider` files EVERY turn and the session record
        under the keys the gates stage looks up, so a replay-only provider on
        the same store reproduces the build with zero HTTP."""
        from elt_taskgen.review import providers as providers_mod

        ws = self.fresh_workspace()
        store_dir = ws / "transcripts"
        policy = _session_policy()
        live = _FakeTransport([
            _chat_tool_call_body("list_schemas", {}, call_id="c0"),
            _chat_tool_call_body("dev_query", {"sql": "SELECT * FROM customers ORDER BY customer_id"}, call_id="c1"),
            _chat_tool_call_body("submit_sql_by_mart", {"sql_by_mart": [{"mart": MART_NAME, "sql": REFERENCE_SQL}]}, call_id="c2"),
        ])
        recorder = _routed_session_provider(self.task, store_dir, live)
        recorded = independent.run_independent_build(
            self.task, ws, recorder, self.gold, session_policy=policy
        )
        self.assertEqual(recorded.status, independent.STATUS_AGREED)
        self.assertEqual(len(live.calls), 3)
        role_dir = store_dir / independent.ROLE_NAME
        turn_entries = sorted(p for p in role_dir.glob("*.json"))
        self.assertEqual(len(turn_entries), 3, "one recorded entry per model turn")
        session_key = recorded.samples[0].prompt_sha256
        record = recorder.store.lookup_session(independent.ROLE_NAME, session_key)
        self.assertIsNotNone(record, "the session record is filed under the sample's key")
        self.assertEqual(record["session_sha256"], recorded.samples[0].session_sha256)
        self.assertEqual(len(record["turn_keys"]), 3)
        self.assertEqual({p.stem for p in turn_entries}, set(record["turn_keys"]))
        # Two tool observations plus the harness dry run of the submission.
        self.assertEqual(len(record["observations_sha256"]), 3)
        self.assertEqual(
            record["observations_sha256"],
            [c.output_sha256 for c in recorded.samples[0].tool_calls],
        )
        self.assertEqual(len(recorder.exchange_evidence), 1)
        # No one-shot exchange exists for an enabled witness: the seed IS the
        # trajectory, and a `complete()` key would never be looked up.
        self.assertIsNone(recorder.store.lookup(
            independent.ROLE_NAME, providers_mod.transcript_key(
                independent.ROLE_NAME, independent.sample_prompt(self.task, 0)
            )
        ))
        # REPLAY: the same build on the same store, zero HTTP, the same
        # trajectory digest, the same certified result.
        silent = _FakeTransport([])
        replayer = _routed_session_provider(self.task, store_dir, silent, replay_only=True)
        replayed = independent.run_independent_build(
            self.task, ws, replayer, self.gold, session_policy=policy
        )
        self.assertEqual(silent.calls, [])
        self.assertEqual(replayed.status, independent.STATUS_AGREED)
        self.assertEqual(replayed.samples[0].prompt_sha256, session_key)
        self.assertEqual(replayed.samples[0].session_sha256, recorded.samples[0].session_sha256)
        self.assertEqual(replayed.samples[0].tool_calls, recorded.samples[0].tool_calls)
        self.assertEqual(replayed.samples[0].sql_by_mart, recorded.samples[0].sql_by_mart)
        self.assertEqual(replayed.agreement, recorded.agreement)
        self.assertEqual(replayer.exchange_evidence[0]["trajectory_sha256"],
                         recorder.exchange_evidence[0]["trajectory_sha256"])
        self.assertTrue(replayer.exchange_evidence[0]["replayed"])
        # An unseeded store under replay-only is refused at the first turn:
        # a replay never invents a turn (C3), and the refusal is the
        # transcript-missing class the CLI lifts to could-not-measure.
        empty = _routed_session_provider(self.task, ws / "empty", _FakeTransport([]), replay_only=True)
        with self.assertRaises(providers_mod.TranscriptMissingError):
            independent.run_independent_build(
                self.task, ws, empty, self.gold, session_policy=policy
            )
        providers_mod.clear_behavior_caches()

    def test_a_truncated_witness_note_leads_with_its_canonical_marker(self) -> None:
        """batch10 run N (2026-09-11): the reasoning witness overran
        max_tokens on four tasks; `OutputTruncated` is a `ProviderFault`
        but not itself a canonical infrastructure name, so `_transport_marker`
        lifted nothing from the note, the dual-build gate went red and the
        truncated witness was routed to the SPECIFICATION proposer, which
        abstained. The note now leads with the MRO's canonical marker."""
        from unittest import mock

        from elt_taskgen import cli
        from elt_taskgen.engine import Engine
        from elt_taskgen.review import session as S

        ws = self.fresh_workspace()
        engine = Engine(ws)
        self.addCleanup(engine.close)
        with mock.patch.object(
            independent, "run_independent_build",
            side_effect=S.OutputTruncated("independent_implementer", turn_index=3),
        ):
            note = cli._ensure_independent_build(engine, self.task, self.gold, None)
        self.assertTrue(
            note.startswith("independent build not performed: ProviderFault: OutputTruncated:"), note
        )
        self.assertEqual(cli._transport_marker([note]), "ProviderFault")
        self.assertIsNone(independent.load_build_result(ws, self.task.task_id))
        # A canonical class keeps its plain lead.
        with mock.patch.object(
            independent, "run_independent_build",
            side_effect=S.ToolDeadlineExceeded("dev_query", deadline_s=10.0),
        ):
            plain = cli._ensure_independent_build(engine, self.task, self.gold, None)
        self.assertTrue(plain.startswith("independent build not performed: ToolDeadlineExceeded:"), plain)

    def test_session_fault_in_build_is_could_not_measure_not_red_gate(self) -> None:
        from unittest import mock

        from elt_taskgen import cli
        from elt_taskgen import engine as engine_mod
        from elt_taskgen.engine import Engine
        from elt_taskgen.review import session as S
        from elt_taskgen.training import dev_tool

        ws = self.fresh_workspace()
        policy = _session_policy()
        # (1) A harness fault raised at the transport boundary mid-session
        # (a worker death, a deadline): the session halts and the SAME class
        # propagates out of the build — never a ValueError, never a protocol
        # error, never a 0.0 — with nothing recorded.
        faults = (
            S.SandboxFault("worker died", code=independent.WORKER_FAILED_CODE),
            S.ToolDeadlineExceeded("dev_query", deadline_s=10.0),
        )
        for fault in faults:
            provider = SessionDouble([[tool_use_block("list_schemas", {}, "t0")], fault])
            with self.assertRaises(type(fault)) as ctx:
                independent.run_independent_build(
                    self.task, ws, provider, self.gold, session_policy=policy
                )
            self.assertIs(ctx.exception, fault)
            self.assertIsNone(independent.load_build_result(ws, self.task.task_id))
            self.assertEqual(engine_mod._infra_marker_for(fault), type(fault).__name__)
        # (2) A tool crashing inside the worker is a `ToolHarnessFault` (its
        # raw output withheld), the same disposition.
        provider = SessionDouble([
            [tool_use_block("dev_query", {"sql": "SELECT * FROM customers"}, "t0")],
            [submit_call(REFERENCE_SQL)],
        ])
        with mock.patch.object(dev_tool, "run_dev_query", side_effect=RuntimeError("boom")):
            with self.assertRaises(S.ToolHarnessFault) as ctx:
                independent.run_independent_build(
                    self.task, ws, provider, self.gold, session_policy=policy
                )
        self.assertEqual(ctx.exception.tool, "dev_query")
        self.assertNotIn("boom", str(ctx.exception))
        self.assertIsNone(independent.load_build_result(ws, self.task.task_id))
        # (3) The gates stage: `_ensure_independent_build` records NOTHING and
        # its note leads with the class name, which `_transport_marker` lifts
        # so the engine halts as could-not-measure (exit 2, no repair round,
        # nothing rejected) instead of a red dual-build gate.
        engine = Engine(ws)
        self.addCleanup(engine.close)
        with mock.patch.object(
            independent, "run_independent_build", side_effect=S.ToolDeadlineExceeded("dev_query", deadline_s=10.0)
        ):
            note = cli._ensure_independent_build(engine, self.task, self.gold, provider)
        self.assertTrue(note.startswith("independent build not performed: ToolDeadlineExceeded:"), note)
        self.assertEqual(cli._transport_marker([note]), "ToolDeadlineExceeded")
        self.assertIsNone(independent.load_build_result(ws, self.task.task_id))
        self.assertNotIn("answer_key", note)
        self.assertNotIn("rows", note)
        # A model-caused stop is NOT a harness fault: an abstained session is
        # a recorded, measured outcome (a red gate), never lifted.
        self.assertEqual(cli._transport_marker(["ForbiddenArgument: forbidden_argument"]), "")
        self.assertEqual(cli._transport_marker(["session ended ABSTAINED"]), "")


# ---------------------------------------------------------------------------
# Batch D6 (report 312): the dual-build disagreement is the WITNESS's, not the
# pipeline's — pinned from the on-disk evidence (READ-ONLY; skips when absent)
# ---------------------------------------------------------------------------

_BATCH = Path(__file__).resolve().parent.parent / "runs" / "authorized_batch_50_20260908" / "workspace-final"
_D6_TASK_ID = "synsql__cache_management_systems__caches_cache_usage_distribution"
_D6_TASK_DIR = _BATCH / "tasks" / _D6_TASK_ID
_D6_DISTRIBUTION = "caches_cache_usage_distribution"
_D6_TOP = "caches_cache_usage_top"


class TestBatchD6DualBuildDiagnosis(unittest.TestCase):
    """Report 312: the implementer session disagreed with the frozen gold on
    primary (0.5), resampled (0.5) and counterfactual (0.0). Diagnosis from
    the task IR, the reference SQL, the session transcript and the recorded
    build, all replayed through the REAL harness:

      * the session VIEW lost nothing: regenerated offline it keys to the
        recorded `prompt_sha256` (byte-identical to what the model saw), it
        carries the two decisive MartSpec sentences verbatim, and its task
        material is exactly the cold one-shot view's;
      * the TOOLS lost nothing: every recorded observation (`dev_query` x2,
        `dry_run_sql`) reproduces byte-for-byte on a rebuilt DEVELOPMENT
        warehouse (`args_sha256` and `output_sha256`);
      * the SUBMISSION SHAPE lost nothing: the tool-call `sql_by_mart` list
        coerces to the recorded sample's SQL, whose digests are the audit
        analysis's `witness_sql_sha256`, and it round-trips the certifier's
        parser;
      * the CERTIFIER reproduces the recorded rewards exactly; and
      * the SPEC is literal and implementable: the witness's own SQL with two
        one-token count corrections (`COUNT(*)` -> `COUNT(u.usage_id)` for the
        distribution's `row_count`, which the spec fixes at 0 for a
        no-activity absent cell; `COUNT(*) FILTER (WHERE hits IS NOT NULL)`
        -> `COUNT(usage_id)` for the top mart's `child_count`, "Number of
        cache_usage rows for this caches row") scores 1.0 on all five
        populations. The model's own turn-0 reasoning states "absent state
        row with row_count=0" and then wrote `COUNT(*)`.

    So: no pipeline defect, no specification ambiguity — a witness error,
    correctly recorded as NEEDS_ADJUDICATION (never resampled away), for
    which the adjudication route is a fresh blind build."""

    @classmethod
    def setUpClass(cls) -> None:
        build_path = _D6_TASK_DIR / "reports" / "independent_build.json"
        if not build_path.is_file():
            raise unittest.SkipTest("batch evidence not on disk")
        cls.task = TaskIR.model_validate(json.loads((_D6_TASK_DIR / "task_ir.json").read_bytes()))
        cls.build = json.loads(build_path.read_bytes())
        cls.sample = cls.build["samples"][0]
        session_path = _BATCH / "transcripts" / "independent_implementer" / "sessions" / (cls.sample["prompt_sha256"] + ".json")
        if not session_path.is_file():
            raise unittest.SkipTest("batch session transcript not on disk")
        cls.session_record = json.loads(session_path.read_bytes())
        cls.turns = [
            json.loads((_BATCH / "transcripts" / "independent_implementer" / (key + ".json")).read_bytes())
            for key in cls.session_record["turn_keys"]
        ]
        cls.tool_calls = [
            (call["function"]["name"], json.loads(call["function"]["arguments"]))
            for turn in cls.turns
            for call in (turn["raw_attempts"][0]["choices"][0]["message"].get("tool_calls") or [])
        ]
        cls.submitted = next(args for name, args in cls.tool_calls if name == "submit_sql_by_mart")

    def _policy(self):
        from elt_taskgen.review import providers as providers_mod
        from elt_taskgen.review.tools import validators as witness_tools

        block = dict(providers_mod.DEFAULT_ROUTING_DOC["roles"][witness_tools.IMPLEMENTER_ROLE]["session"])
        return witness_tools.implementer_policy(witness_tools.implementer_limits(block))

    def test_recorded_evidence_binds_to_this_task(self) -> None:
        self.assertEqual(self.build["task_content_hash"], self.task.content_hash())
        self.assertEqual(self.build["status"], independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(self.sample["rewards"], {"development": 1.0, "primary": 0.5, "resampled": 0.5, "counterfactual": 0.0, "stress": 1.0})
        self.assertEqual(len(self.build["samples"]), 1, "a dev_pass sample failing hidden populations is never resampled away")
        self.assertTrue(self.sample["dev_pass"])
        self.assertEqual(self.sample["terminal"], "SUBMITTED")
        self.assertEqual([name for name, _ in self.tool_calls], ["dev_query", "dev_query", "dry_run_sql", "submit_sql_by_mart"])

    def test_session_view_is_byte_identical_and_carries_the_decisive_spec_lines(self) -> None:
        from elt_taskgen.review import providers as providers_mod
        from elt_taskgen.review.tools import validators as witness_tools

        policy = self._policy()
        # Validate D6 under its recorded limits; the shipped limits widened
        # later, so only those limit fields may differ.
        from elt_taskgen.review.tools import validators as witness_tools

        recorded_policy = witness_tools.implementer_policy(
            witness_tools.implementer_limits(self.sample["limits"])
        ) if hasattr(witness_tools, "implementer_policy") else None
        if recorded_policy is not None:
            if "no catalogue queries" in witness_tools.DevQueryTool.description:
                # The policy digest covers the tool descriptions, so it moved
                # with the dev_query warning below (2026-09-11, run M).
                self.assertNotEqual(recorded_policy.sha256(), self.session_record["policy_sha256"])
            else:
                self.assertEqual(recorded_policy.sha256(), self.session_record["policy_sha256"])
            if "no catalogue queries" in witness_tools.DevQueryTool.description:
                # The dev_query description gained the catalogue-query
                # warning on 2026-09-11 (batch10 run M: an
                # `information_schema` query ended the personio witness
                # session as a policy event); the record predates it.
                self.assertNotEqual(recorded_policy.tools_sha256(), self.session_record["tools_sha256"])
            else:
                self.assertEqual(recorded_policy.tools_sha256(), self.session_record["tools_sha256"])
            self.assertEqual(recorded_policy.limits.as_manifest(), self.sample["limits"])
        else:
            self.assertEqual(policy.tools_sha256(), self.session_record["tools_sha256"])
        view = independent.implementer_session_view(self.task, 0, limits=policy.limits)
        key = providers_mod.transcript_key_v3(
            witness_tools.IMPLEMENTER_ROLE, policy, [{"role": "user", "content": view}]
        )
        if "DUCKDB PITFALLS" in independent._IMPLEMENTER_SESSION_PREAMBLE:
        # D6 predates the DuckDB-pitfalls preamble and re-keys under today's
        # view; policy, tool, and decisive-spec pins must still match.
            self.assertNotEqual(key, self.sample["prompt_sha256"])
        else:
            self.assertEqual(key, self.sample["prompt_sha256"], "the regenerated session view is not what the model saw")
        cold = independent.implementer_view(self.task)
        decisive = (
            "- row_count: bigint — Number of linked cache_usage rows in this entity/state cell; 0 for a no-activity absent cell.",
            "- child_count: bigint — Number of cache_usage rows for this caches row; 0 when there are none.",
            "grain: One row per caches (cache_id), INCLUDING caches rows with no linked cache_usage rows.",
        )
        for sentence in decisive:
            self.assertIn(sentence, view)
            self.assertIn(sentence, cold)
        # The session view's task material IS the cold-resample view's.
        for line in independent._implementer_material_lines(self.task):
            self.assertIn(line, view)
            self.assertIn(line, cold)
        # And the model read it: its own reasoning states the rule it then broke.
        reasoning = str(self.turns[0]["raw_attempts"][0]["choices"][0]["message"].get("reasoning") or "")
        self.assertIn("row_count=0", reasoning)

    def test_submission_shape_is_lossless_and_matches_the_audit_digests(self) -> None:
        import hashlib

        from elt_taskgen.review.tools import validators as witness_tools

        coerced = witness_tools._coerce_sql_by_mart(self.submitted["sql_by_mart"])
        self.assertEqual(coerced, self.sample["sql_by_mart"])
        self.assertEqual(independent.parse_sql_by_mart(self.task, json.dumps(coerced)), coerced)
        analyses = sorted((_BATCH / "audit").glob(f"{_D6_TASK_ID}.dual_build_analysis.*.json"))
        if analyses:
            analysis = json.loads(analyses[-1].read_bytes())
            for mart, sql in coerced.items():
                self.assertEqual(hashlib.sha256(sql.encode("utf-8")).hexdigest(), analysis["witness_sql_sha256"][mart])
        # The two defects, in the witness's own text.
        self.assertIn("COUNT(*) AS row_count", coerced[_D6_DISTRIBUTION])
        self.assertIn("COUNT(*) FILTER (WHERE hits IS NOT NULL) AS child_count", coerced[_D6_TOP])
        # The reference counts the LINK, never the outer-join placeholder.
        self.assertIn('COUNT("link_key") AS "row_count"', self.task.reference.sql_by_mart[_D6_DISTRIBUTION])
        self.assertIn('COUNT("f_id") AS "m_2"', self.task.reference.sql_by_mart[_D6_TOP])

    def test_tools_and_certifier_reproduce_and_a_literal_reading_agrees(self) -> None:
        import hashlib

        from elt_taskgen.review.tools import projection as PJ
        from elt_taskgen.review.tools import validators as witness_tools

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name) / "taskgen-workspace"
        gold = build_workspace(self.task, workspace)
        # The tools: every recorded observation reproduces byte-for-byte.
        session = witness_tools.ImplementerSession.open(workspace, self.task, warehouse_root=Path(tmp.name) / "wh")
        ctx = session.context(root=Path(tmp.name) / "scratch")
        registry = witness_tools.implementer_registry()
        recorded = [c for c in self.sample["tool_calls"]]
        self.assertEqual([c["name"] for c in recorded], ["dev_query", "dev_query", "dry_run_sql"])
        for (name, args), record in zip(self.tool_calls, recorded):
            self.assertEqual(name, record["name"])
            self.assertEqual(hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), record["args_sha256"])
            observation = registry.dispatch(ctx, name, args)
            payload = PJ.serialize_for_transport(observation, task=self.task)
            PJ.assert_value_free(payload.encode("utf-8"), task=self.task)
            self.assertEqual(hashlib.sha256(payload.encode("utf-8")).hexdigest(), record["output_sha256"], name)
        # The public sample cannot show the two edge cases (its own conditions:
        # full link coverage, no NULLs): one linked row with a hits value per cache.
        coverage = registry.dispatch(ctx, "dev_query", {"sql": (
            "SELECT c.cache_id, COUNT(u.usage_id) AS usage_id, COUNT(u.hits) AS hits "
            "FROM caches c LEFT JOIN cache_usage u ON u.cache_id = c.cache_id GROUP BY c.cache_id ORDER BY c.cache_id"
        )})
        self.assertTrue(all(row[1] >= 1 and row[2] == row[1] for row in coverage.rows), coverage.rows)
        # The certifier reproduces the recorded rewards exactly ...
        witness = dict(self.sample["sql_by_mart"])
        rewards, errors = independent.evaluate_build(self.task, gold, witness, workspace)
        self.assertEqual(errors, {})
        self.assertEqual(rewards, self.sample["rewards"])
        # ... and the witness's own SQL under the literal reading of the two
        # count sentences agrees on all five populations: no ambiguity.
        literal = {
            _D6_DISTRIBUTION: witness[_D6_DISTRIBUTION].replace("COUNT(*) AS row_count", "COUNT(u.usage_id) AS row_count"),
            _D6_TOP: witness[_D6_TOP].replace("COUNT(*) FILTER (WHERE hits IS NOT NULL) AS child_count", "COUNT(usage_id) AS child_count"),
        }
        self.assertNotEqual(literal, witness)
        rewards, errors = independent.evaluate_build(self.task, gold, literal, workspace)
        self.assertEqual(errors, {})
        self.assertEqual(rewards, {pop.value: 1.0 for pop in P})
        # Each defect alone accounts for its populations: the placeholder count
        # only where a cache has no links (counterfactual), the non-NULL filter
        # wherever hits is NULL (primary, resampled, counterfactual).
        rewards, _ = independent.evaluate_build(self.task, gold, {**witness, _D6_TOP: literal[_D6_TOP]}, workspace)
        self.assertEqual(rewards, {"development": 1.0, "primary": 1.0, "resampled": 1.0, "counterfactual": 0.5, "stress": 1.0})
        rewards, _ = independent.evaluate_build(self.task, gold, {**witness, _D6_DISTRIBUTION: literal[_D6_DISTRIBUTION]}, workspace)
        self.assertEqual(rewards, {"development": 1.0, "primary": 0.5, "resampled": 0.5, "counterfactual": 0.5, "stress": 1.0})


if __name__ == "__main__":
    unittest.main()

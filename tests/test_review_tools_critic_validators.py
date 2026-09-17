"""The critic seats' harness validators (roadmap Phase 3 item 2; SoT T1.1,
T3; trust boundary "Mutation tests" row; constraint addendum A24), declared
fail-closed in code and enabled for POP/SHC by the shipped agent profile.

What is pinned here: the projections carry codes, booleans and public
identifiers only — never the compiled key (it embeds `case.mutation`), never
an inert or inapplicable sentence (`materialize_mutation` is never called
inside a session), never the post-session screen's lexicon; `compile_probe`
emits a problem only when NO probe compiles; `project_proposal_matrix` is a
post-session record and never a tool of any manifest; `measured_match_bit`
is off by default, one aggregate bit, at most one call; and under an explicit
`session.enabled: false` rollback nothing reaches the registry or fingerprint
(the validators are harness-only and never reach the wire). The
flag-discipline pin itself lives in
tests/test_council_efficacy.py (`test_disabled_critic_tools_do_not_enter_
the_wire_manifest_or_fingerprint`); the correction channel's budget rule in
tests/test_bounded_session.py (`test_semantic_corrections_share_schema_
retries_budget`).
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import (
    AttackKind,
    CouncilRole,
    Finding,
    PopulationName,
    RepairRoute,
    Severity,
)
from elt_taskgen.review import council
from elt_taskgen.review import metrology as M
from elt_taskgen.review import providers as P
from elt_taskgen.review import session as S
from elt_taskgen.review.tools import critic_validators as CV
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG
from elt_taskgen.verification import attacks
from tests import test_attack_promotion as tap
from tests.test_bounded_session import ScriptedProvider, text_block, tool_use

TASK = demo_task()
POP = CV.ADVERSARY_ROLE
SHC = CV.ATTACKER_ROLE
SUBMIT = P.FINDINGS_TOOL_NAME
GROUNDED = (
    "Rule 2 never says whether duplicate order_items rows for a customer_id "
    "should be summed twice in total_spend."
)

#: The SoT T1.1 POP / SHC blocks, ENABLED as shipped and as a
#: `RoutedProvider` runs them.
POP_BLOCK = {
    "enabled": True, "mode": "harness_validated", "harness_validators": ["compile_proposal"],
    "max_model_calls": 3, "max_compile_corrections": 1, "max_tool_calls": 0,
    "max_wall_s": 300, "max_usd": 1.0, "max_oracle_bits": 6, "measured_match_bit": False,
}
SHC_BLOCK = {
    "enabled": True, "mode": "harness_validated", "harness_validators": ["compile_probe"],
    "max_model_calls": 3, "max_compile_corrections": 1, "max_tool_calls": 0,
    "max_wall_s": 300, "max_usd": 1.0, "max_oracle_bits": 6,
}


def _expected(passes=("development", "stress")) -> dict:
    return {p.value: (p.value in passes) for p in PopulationName}


def _proposal(kind="inner_join", params=None, *, passes=("development", "stress"),
              rationale="development and stress cannot distinguish INNER from LEFT") -> dict:
    expected = _expected(passes)
    return {
        "kind": kind,
        "params": json.dumps(params or {}),
        "expected_pass_by_stage": {"extract_load": {p.value: True for p in PopulationName}, "transform": expected},
        "rationale": rationale,
    }


def _finding(summary="an INNER join is indistinguishable on the stated populations",
             detail="every development customer has a completed order", attack="inner_join",
             proposed=None, severity="major") -> dict:
    return {"severity": severity, "summary": summary, "detail": detail, "route_hint": None,
            "suggested_attack": attack, "proposed_case": proposed}


def _payload(*findings: dict) -> dict:
    return {"findings": list(findings)}


def _doc(**session_updates) -> dict:
    """The loaded agents document with `roles.<role>.session` keys updated."""
    doc = json.loads(json.dumps(P._agents_doc()))
    for role, keys in session_updates.items():
        doc["roles"][role].setdefault("session", {}).update(keys)
    return doc


@contextlib.contextmanager
def _agents(doc: dict):
    with mock.patch.object(P, "_agents_doc", lambda: doc):
        P.clear_behavior_caches()
        try:
            yield
        finally:
            P.clear_behavior_caches()


def _texts(diag: PJ.Diagnostic) -> tuple[str, str]:
    """The rendered text and the transport bytes of one projection, after
    the projector and the D1 gatekeeper both passed it."""
    transport = PJ.serialize_for_transport(diag, task=TASK)
    PJ.assert_value_free(transport.encode("utf-8"), task=TASK)
    return diag.render(), transport


class _CriticCase(unittest.TestCase):
    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "scratch"
        self.root.mkdir()

    def session(self, role=POP, **kw) -> tuple[CV.CriticSession, CV.CriticToolContext]:
        session = CV.CriticSession(task=TASK, role=role, **kw)
        return session, session.context(self.root)

    def run_critic_session(self, role, script, *, block, session=None, worker=None):
        policy = CV.critic_policy(role, S.SessionLimits.from_block(block))
        session = session or CV.CriticSession(task=TASK, role=role)
        ctx = session.context(self.root)
        provider = ScriptedProvider(script)
        result = S.run_bounded_session(
            role, "the view", policy.tools, policy, policy.limits,
            provider=provider, ctx=ctx, worker=worker or CV.critic_validator_worker(),
        )
        return result, provider, session

    def assert_identities(self, result: S.SessionResult):
        self.assertEqual(
            result.model_call_count,
            result.tool_call_count + result.refused_count + result.nudge_count
            + result.correction_count + result.terminal_count + result.limit_stop_count,
        )
        self.assertEqual(
            len(result.turns),
            result.model_call_count + result.tool_call_count + result.refused_count
            + result.nudge_count + result.validator_run_count,
        )
        self.assertTrue(result.verify_chain())


# ---------------------------------------------------------------------------
# Mutation feedback and no-leak
# ---------------------------------------------------------------------------

class MutationFeedbackTest(_CriticCase):
    def test_mutant_probe_never_returns_key_text_or_duplicate_bit(self):
        """SoT T3 `compile_probe`: `{compiles, kind, is_directive}` and nothing
        else — never the compiled key (it embeds `case.mutation`), never the
        directive string, and NO duplicate bit (the in-payload same-mutant
        bit belongs to `compile_proposal` alone; the screen's coalescing is
        post-session)."""
        session, ctx = self.session(SHC)
        tool = CV.critic_validator(CV.COMPILE_PROBE_TOOL)
        # Every non-informational probe carries the exact case its named
        # attack promises (a bare attack is a `proposal_missing` correction
        # since batch-repair round 2, review finding 1-2).
        payload = _payload(_finding(attack="no_dedup", proposed=_proposal("no_dedup", {})),
                           _finding(attack="no_dedup", proposed=_proposal("no_dedup", {})),
                           _finding(attack="inner_join", proposed=_proposal()))
        folded = tool.run(ctx, payload)
        findings = session.findings
        self.assertEqual(len(findings), 3)
        keys = [council._compiled_mutant_key(TASK, f) for f in findings]
        self.assertTrue(all(keys), keys)
        self.assertEqual(keys[0], keys[1])  # two identical probes: one mutant
        bare = TASK.model_copy(update={"attack_cases": ()})
        mutations = [
            c.mutation
            for c in attacks.compile_attacks(bare, [f.model_copy(update={"proposed_case": None}) for f in findings])
        ]
        self.assertTrue(mutations)
        diags = [folded] + [CV.compile_probe(ctx, {"finding_index": i}) for i in range(len(findings))]
        for diag in diags:
            render, transport = _texts(diag)
            for text in (render, transport):
                for key in keys:
                    self.assertNotIn(key, text)
                for mutation in mutations:
                    self.assertNotIn(mutation, text)
                self.assertNotIn("directive:", text)
                self.assertNotIn("|", text)
            self.assertIn(diag.code, ("compiles", "uncompilable"))
            self.assertNotIn(CV.SAME_MUTANT_FLAG, diag.flags)
            self.assertFalse(any("duplicate" in key or "mutant" in key for key in diag.flags), diag.flags)
            self.assertLessEqual(
                set(diag.flags),
                {CV.IS_DIRECTIVE_FLAG, CV.PROBE_PRESENT_FLAG, CV.EVERY_PROBE_COMPILES_FLAG,
                 CV.EXECUTABLE_PROBE_FLAG},
            )
        self.assertTrue(folded.ok)
        self.assertEqual(folded.names, ("inner_join", "no_dedup"))
        # Per finding: both identical probes compile, each reported alike.
        self.assertEqual(diags[1].render(), diags[2].render())
        self.assertTrue(diags[1].ok and diags[2].ok and diags[3].ok)

    def test_compile_projection_never_surfaces_inert_or_inapplicable_sentences(self):
        """Trust boundary row 7: an inert or inapplicable verdict needs gold
        and is decided by `materialize_mutation` / `run_attack` AFTER the
        session; the compile projections never call them, and their closed
        code vocabulary has no member for either, so the sentences cannot
        appear whatever the mutator would say."""
        inert = attacks.InertAstMutationError("the mutation changed nothing on mart customer_summary")
        inapplicable = attacks.InapplicableLoadMutationError(
            "this task/data offers no surface for the requested load mutation skip_extraction"
        )
        forbidden = ("inert", "inapplicable", "changed nothing", "no surface",
                     "InertAstMutationError", "InapplicableLoadMutationError", "Error")
        pop_session, pop_ctx = self.session(POP)
        shc_session, shc_ctx = self.session(SHC)
        with mock.patch.object(attacks, "materialize_mutation", side_effect=inert) as mm, \
                mock.patch.object(attacks, "run_attack", side_effect=inapplicable) as ra:
            outputs = [
                CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL).run(pop_ctx, _payload(
                    _finding(proposed=_proposal()),
                    _finding(attack="skip_extraction", proposed=_proposal(
                        "skip_extraction", {"skip_tables": ["orders"]},
                        rationale="skipping the orders extraction keeps the mart empty")),
                    _finding(attack="no_null_default", proposed=_proposal("no_null_default", {"zero_is_missing": True})),
                )),
                CV.critic_validator(CV.COMPILE_PROBE_TOOL).run(shc_ctx, _payload(
                    _finding(attack="skip_extraction"), _finding(attack="no_dedup"),
                    _finding(attack="constants", summary="emit the development population's outputs verbatim"),
                )),
            ]
            outputs += [CV.compile_proposal(pop_ctx, {"finding_index": i}) for i in range(len(pop_session.findings))]
            outputs += [CV.compile_probe(shc_ctx, {"finding_index": i}) for i in range(len(shc_session.findings))]
            self.assertEqual((mm.call_count, ra.call_count), (0, 0))
        for diag in outputs:
            self.assertIn(diag.code, PJ.COMPILE_CODES)
            render, transport = _texts(diag)
            for text in (render, transport):
                for word in forbidden:
                    self.assertNotIn(word.lower(), text.lower(), (word, text))
        self.assertTrue(set(PJ.COMPILE_CODES).isdisjoint({"inert", "inapplicable"}))

    def test_compile_projection_never_echoes_screen_lexicon(self):
        """The screen (`council.screen_findings`) is pure and post-session:
        its VOID / NOTED / DUPLICATE / REPRESENTATIVE verdicts never reach a
        seat in-session (a seat that learned the lexicon could write around
        it — `test_a_rephrased_retraction_is_missed_by_design` records that
        the lexicon is not semantic), and the validators never call it."""
        lexicon = CV.SCREEN_LEXICON
        self.assertEqual(set(lexicon), {"void", "noted", "duplicate", "representative",
                                        "VOID", "NOTED", "DUPLICATE", "REPRESENTATIVE"})
        for name in (CV.SAME_MUTANT_FLAG, *CV.GRAMMAR_CODES, CV.CLAIM_FIDELITY_FLAG,
                     CV.PROBE_PRESENT_FLAG, CV.PROPOSAL_PRESENT_FLAG, CV.EXACT_MATCH_FLAG):
            for word in lexicon:
                self.assertIsNone(re.search(rf"\b{word}\b", name, re.IGNORECASE), (name, word))
        pop_session, pop_ctx = self.session(POP)
        shc_session, shc_ctx = self.session(SHC)
        retraction = ("On further thought the order_items grain is unambiguous and I am "
                      "content to leave rule 2 as written.")
        outputs: list[PJ.Diagnostic] = []
        with mock.patch.object(council, "screen_findings", side_effect=AssertionError("post-session only")):
            outputs.append(CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL).run(pop_ctx, _payload(
                _finding(proposed=_proposal()),
                _finding(proposed=_proposal()),                         # the screen's DUPLICATE
                _finding(detail="n/a", proposed=_proposal()),           # the screen's VOID
                _finding(summary=retraction, detail=GROUNDED, attack=None),  # a rephrased retraction
            )))
            outputs += [CV.compile_proposal(pop_ctx, {"finding_index": i}) for i in range(len(pop_session.findings))]
            outputs.append(CV.critic_validator(CV.COMPILE_PROBE_TOOL).run(shc_ctx, _payload(
                _finding(attack="no_dedup"), _finding(attack="no_dedup"), _finding(detail="n/a", attack="no_dedup"),
            )))
            outputs += [CV.compile_probe(shc_ctx, {"finding_index": i}) for i in range(len(shc_session.findings))]
        for diag in outputs:
            render, transport = _texts(diag)
            for text in (render, transport):
                for word in lexicon:
                    self.assertIsNone(re.search(rf"\b{word}\b", text, re.IGNORECASE), (word, text))
        # The in-payload same-mutant bit is reported, without the lexicon.
        self.assertTrue(outputs[0].flags[CV.SAME_MUTANT_FLAG])
        self.assertFalse(outputs[1].flags[CV.SAME_MUTANT_FLAG])
        self.assertTrue(outputs[2].flags[CV.SAME_MUTANT_FLAG])
        # The screen itself is untouched and still misses the rephrased
        # retraction by design (tests/test_council_screen.py).
        sneaky = Finding(finding_id="a-00", role=CouncilRole.AMBIGUITY_CRITIC, severity=Severity.MAJOR,
                         summary=retraction, detail=GROUNDED)
        self.assertIsNone(council.screen_findings(TASK, [sneaky])[0].screen)

    def test_compile_probe_emits_problem_only_when_no_probe_compiles(self):
        """Mirrors the review stage's diligence check on the shortcut
        attacker (`cli.py`: zero executable probes fail the stage): the
        payload-level result is red ONLY when no probe of the payload
        compiles — one compiling probe among non-compiling ones is green
        (`every_probe_compiles` says the rest), an empty payload is red."""
        session, ctx = self.session(SHC)
        tool = CV.critic_validator(CV.COMPILE_PROBE_TOOL)
        # The probe-side rule is exercised with every named attack carrying
        # its exact case (an INFO suggestion or a bare MINOR/MAJOR attack is
        # the PROPOSAL side's business: `test_attacker_bare_attack_is_a_
        # proposal_missing_correction_then_voided`); `custom` has no default
        # realization, so its probe never compiles whatever it carries.
        cases = [
            ("one of three compiles", _payload(_finding(attack=None, severity="minor"),
                                               _finding(attack="custom", severity="info"),
                                               _finding(attack="no_dedup", proposed=_proposal("no_dedup", {}))), True, False),
            ("all compile", _payload(_finding(attack="no_dedup", proposed=_proposal("no_dedup", {})),
                                     _finding(attack="inner_join", proposed=_proposal())), True, True),
            ("no probe at all", _payload(_finding(attack=None, severity="minor")), False, False),
            ("proposal only", _payload(_finding(attack=None, proposed=_proposal())), False, False),
            ("custom without payload", _payload(_finding(attack="custom", proposed=_proposal("custom", {}))), False, False),
            ("empty payload", _payload(), False, False),
        ]
        for label, payload, ok, every in cases:
            with self.subTest(label=label):
                diag = tool.run(ctx, payload)
                _texts(diag)
                self.assertEqual((diag.ok, diag.code), (ok, "compiles" if ok else "uncompilable"))
                self.assertEqual(diag.flags[CV.EVERY_PROBE_COMPILES_FLAG], every)
                if ok:
                    self.assertTrue(diag.names)
                    self.assertIn(diag.subject, diag.names)
                elif label == "custom without payload":
                    # A kind with no default realization: the compiler refuses
                    # to guess, and says so in the closed grammar (D2) — the
                    # kind as subject, `variant_invalid`, and the registry's
                    # variants as names once the vocabulary publishes them
                    # (`test_variant_names_travel_once_public`).
                    self.assertEqual(diag.subject, "custom")
                    self.assertTrue(diag.flags["variant_invalid"])
                    # The vocabulary publishes the variants now, so the
                    # correction NAMES the registry's members for the kind.
                    from elt_taskgen.models import AttackKind
                    self.assertTrue(diag.names)
                    self.assertEqual(
                        set(diag.names), set(attacks.allowed_kind_variants(AttackKind.CUSTOM))
                    )
                else:
                    self.assertEqual((diag.subject, diag.names), ("", ()))
        # The per-finding view is honest about each probe on its own.
        tool.run(ctx, cases[0][1])
        per = [CV.compile_probe(ctx, {"finding_index": i}) for i in range(3)]
        self.assertEqual([d.ok for d in per], [False, False, True])
        self.assertEqual(CV.fold_compile_probe(per).ok, True)
        self.assertEqual(CV.fold_compile_probe(per[:2]).ok, False)
        self.assertEqual(CV.fold_compile_probe(()).ok, False)

    def test_compile_proposal_reports_grammar_codes_and_public_identifiers_only(self):
        """SoT T3 `compile_proposal`: one grammar flag per red proposal, the
        PUBLIC identifiers the claim failed to name, the same-mutant bit;
        a hidden population a `hardcode_population` proposal targets is
        withheld from `names` even when it is the missing identifier."""
        session, ctx = self.session(POP)
        tool = CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL)
        cases = {
            "good": (_payload(_finding(proposed=_proposal())), True, None),
            # A MAJOR adversary observation is executable by contract (D1):
            # "major adversary finding requires an executable proposed_case"
            # is the `proposal_missing` compile correction, in-session.
            "observation": (_payload(_finding(attack=None)), False, "proposal_missing"),
            "minor_observation": (
                _payload(_finding(attack=None, severity="minor")), True, None,
            ),
            "empty": (_payload(), True, None),
            "minor_missing_proposal": (
                _payload(_finding(severity="minor", proposed=None)),
                False,
                "proposal_missing",
            ),
            "minor_invalid_proposal": (
                _payload(_finding(
                    severity="minor",
                    proposed=_proposal(params={"bogus": 1}),
                )),
                False,
                "param_unknown",
            ),
            "info_cannot_carry_proposal": (
                _payload(_finding(severity="info", proposed=_proposal())),
                False,
                "severity_incompatible",
            ),
            "param_unknown": (_payload(_finding(proposed=_proposal(params={"bogus": 1}))), False, "param_unknown"),
            "kind_operation_mismatch": (
                _payload(_finding(
                    attack="custom",
                    proposed=_proposal("custom", {"skip_backend": "postgres"}),
                )),
                False, "kind_operation_mismatch"),
            "param_conflict": (
                _payload(_finding(attack="custom", proposed=_proposal(
                    "custom", {"copy_mart": "customer_summary->other", "add_dedup": True}))),
                False, "param_conflict"),
            "claim_missing_identifier": (
                _payload(_finding(
                    summary="one source omission may be indistinguishable",
                    detail="customer_summary may survive the omitted source",
                    attack="skip_extraction",
                    proposed=_proposal(
                        "skip_extraction", {"skip_tables": ["orders"]}
                    ),
                )),
                False, "claim_missing_identifier"),
        }
        for label, (payload, ok, code) in cases.items():
            with self.subTest(label=label):
                diag = tool.run(ctx, payload)
                _texts(diag)
                self.assertEqual((diag.ok, diag.code), (ok, "compiles" if ok else "uncompilable"))
                fired = [c for c in CV.GRAMMAR_CODES if diag.flags.get(c)]
                self.assertEqual(fired, [code] if code else [])
                if code and session.findings[0].proposed_case is not None:
                    self.assertEqual(diag.subject, session.findings[0].proposed_case.kind.value)
        missing = tool.run(ctx, cases["claim_missing_identifier"][0])
        self.assertEqual(missing.names, ("orders",))
        self.assertFalse(missing.flags[CV.CLAIM_FIDELITY_FLAG])
        # A hidden population the claim does not name is withheld.
        hidden = tool.run(ctx, _payload(_finding(
            attack="constants", proposed=_proposal("constants", {"hardcode_population": "primary"}),
        )))
        self.assertFalse(hidden.ok)
        self.assertTrue(hidden.flags["claim_missing_identifier"])
        self.assertEqual(hidden.names, ())
        self.assertNotIn("primary", hidden.render())
        _texts(hidden)
        # The per-finding interface and its harness-side faults.
        tool.run(ctx, _payload(_finding(proposed=_proposal()), _finding(proposed=_proposal(params={"bogus": 1}))))
        first = CV.compile_proposal(ctx, {"finding_index": 0})
        second = CV.compile_proposal(ctx, {"finding_index": 1})
        self.assertTrue(first.ok)
        self.assertTrue(second.flags["param_unknown"])
        self.assertFalse(CV.fold_compile_proposal([first, second]).ok)
        with self.assertRaises(S.ToolHarnessFault) as caught:
            CV.compile_proposal(ctx, {"finding_index": 2})
        self.assertEqual(caught.exception.code, "finding_index_out_of_range")
        with self.assertRaises(S.ToolHarnessFault) as caught:
            CV.compile_proposal(ctx, {"finding_index": -1})
        self.assertEqual(caught.exception.code, "finding_index_invalid")
        bare_ctx = RG.ToolContext(root=self.root, task_id=TASK.task_id, role=POP, task=TASK)
        with self.assertRaises(S.ToolHarnessFault) as caught:
            CV.compile_proposal(bare_ctx, {"finding_index": 0})
        self.assertEqual(caught.exception.code, "no_critic_session")

    def test_pre_flight_never_calls_materialize_mutation(self):
        """SoT T3 `mutant_applicable` "does not exist": no critic validator
        — per finding, per payload, or inside a full harness-validated
        session — calls `materialize_mutation` (gold-bearing) or
        `run_attack`; `compile_attacks` and the promoter's grammar are all
        they run."""
        pop_session, pop_ctx = self.session(POP)
        shc_session, shc_ctx = self.session(SHC)
        with mock.patch.object(attacks, "materialize_mutation",
                               side_effect=AssertionError("materialize_mutation inside a session")) as mm, \
                mock.patch.object(attacks, "run_attack", side_effect=AssertionError("run_attack inside a session")) as ra:
            CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL).run(pop_ctx, _payload(
                _finding(proposed=_proposal()),
                _finding(attack="constants", proposed=_proposal("constants", {"hardcode_population": "development"},
                                                                rationale="the development outputs verbatim")),
                _finding(attack="no_dedup", proposed=_proposal("no_dedup", {"remove_dedup": True})),
            ))
            for i in range(len(pop_session.findings)):
                CV.compile_proposal(pop_ctx, {"finding_index": i})
            CV.critic_validator(CV.COMPILE_PROBE_TOOL).run(shc_ctx, _payload(
                _finding(attack="no_dedup"), _finding(attack="skip_extraction"),
                _finding(attack="constants", summary="emit the development population's outputs verbatim"),
            ))
            for i in range(len(shc_session.findings)):
                CV.compile_probe(shc_ctx, {"finding_index": i})
            # A whole session: one red submit, one correction, one green submit.
            script = [
                [tool_use(SUBMIT, _payload(_finding(proposed=_proposal(params={"bogus": 1}))), "a")],
                [tool_use(SUBMIT, _payload(_finding(proposed=_proposal())), "b")],
            ]
            result, _provider, _session = self.run_critic_session(POP, script, block=POP_BLOCK)
            self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
            self.assertEqual(result.validator_run_count, 2)
            self.assertEqual((mm.call_count, ra.call_count), (0, 0))

    def test_critic_session_compile_correction_carries_the_diagnostic(self):
        """SoT T1.1 end to end on the demo task: the harness runs
        `compile_proposal` on every submitted payload; a red result is ONE
        compile correction whose tool_result IS the rendered Diagnostic
        (never CORRECTION_TEXT plus a problem sentence), the corrected
        payload is accepted and `final` is the one-shot normalized text;
        the second red is accepted as is; and two schema corrections
        exhaust the shared budget so a red on the last turn is accepted."""
        # A green POP major must not concede that any hidden graded population
        # catches its proposed wrong logic; that would self-refute the role's
        # blindness claim in the deterministic council screen.
        good = _payload(_finding(proposed=_proposal(
            passes=tuple(population.value for population in PopulationName)
        )))
        red = _payload(_finding(proposed=_proposal(params={"bogus": 1})))
        result, provider, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, red, "a")], [tool_use(SUBMIT, good, "b")]], block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, P.normalized_text_for(POP, json.loads(json.dumps(good))))
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        self.assertEqual((result.validator_run_count, result.oracle_bits_used, result.tool_call_count), (2, 6, 0))
        self.assertEqual(session.check_count, 2)
        correction = provider.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(correction["is_error"])
        self.assertTrue(correction["content"].startswith("[compile] uncompilable"))
        self.assertIn("param_unknown=true", correction["content"])
        self.assertNotIn("That call was refused", correction["content"])
        self.assertNotIn(P.CORRECTION_TEXT[:20], correction["content"])
        validator_turns = [t for t in result.turns if t.kind == "validator"]
        self.assertEqual([t.outcome_code for t in validator_turns], ["uncompilable", "compiles"])
        self.assertTrue(all(t.fresh and t.tool_name == CV.COMPILE_PROPOSAL_TOOL for t in validator_turns))
        self.assertEqual([c["wire_tools"] for c in provider.calls], [[SUBMIT], [SUBMIT]])
        self.assert_identities(result)
        # The second red is accepted as submitted and screened post-session.
        result, _, _ = self.run_critic_session(
            POP, [[tool_use(SUBMIT, red, "a")], [tool_use(SUBMIT, red, "b")]], block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        self.assertEqual(result.final, P.normalized_text_for(POP, json.loads(json.dumps(red))))
        # Schema and compile corrections SHARE the two: after two schema
        # corrections a red on the last permitted turn is accepted as is.
        result, _, _ = self.run_critic_session(
            POP, [[text_block("no call")], [text_block("still no call")], [tool_use(SUBMIT, red, "c")]],
            block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 2, "compile": 0})
        self.assertEqual(result.correction_count, S.SCHEMA_RETRIES)
        self.assert_identities(result)
        # The shortcut attacker's validator answers the same way.
        result, provider, _ = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, _payload(_finding(attack=None)), "a")],
                  [tool_use(SUBMIT, _payload(_finding(attack="no_dedup")), "b")]], block=SHC_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        self.assertTrue(provider.calls[1]["messages"][-1]["content"][0]["content"].startswith("[compile] uncompilable"))


# ---------------------------------------------------------------------------
# The post-session projection and the seat's manifest
# ---------------------------------------------------------------------------

def _roles_with_session_block() -> list[str]:
    roles = P._agents_doc().get("roles") or {}
    return sorted(r for r, spec in roles.items() if isinstance(spec, dict) and isinstance(spec.get("session"), dict))


class CorrectionExhaustionTest(_CriticCase):
    def test_compile_proposal_kind_without_default_variant_is_uncompilable_not_a_harness_fault(self):
        """A variant-only kind must name a registered variant explicitly.

        The validator reports a bounded compile correction.  It never guesses
        the sole registry member and never converts bad provider output into a
        harness crash.
        """
        session, ctx = self.session(POP)
        tool = CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL)
        for kind in ("wrong_agg_stage", "custom"):
            with self.subTest(kind=kind):
                self.assertNotIn("", attacks.KIND_VARIANTS[AttackKind(kind)])
                payload = _payload(_finding(attack=kind, proposed=_proposal(kind=kind)))
                folded = tool.run(ctx, payload)  # never raises
                self.assertFalse(folded.ok)
                self.assertEqual((folded.code, folded.subject), ("uncompilable", kind))
                self.assertTrue(folded.flags["variant_invalid"])
                self.assertFalse(folded.flags[CV.CLAIM_FIDELITY_FLAG])
                rendered, transport = _texts(folded)
                self.assertTrue(rendered.startswith("[compile] uncompilable"))
                self.assertNotIn("directive", transport)
                self.assertEqual([c.ok for c in session.finding_checks], [False])
        for kind, variant in (
            ("inner_join", "second_hop"),
            ("custom", "add_dedup"),
        ):
            with self.subTest(kind=kind, variant=variant):
                folded = tool.run(
                    ctx,
                    _payload(_finding(
                        summary=(
                            f"{variant} mutates customer_summary with {kind}"
                        ),
                        detail=(
                            f"apply {variant} to customer_summary exactly"
                        ),
                        attack=kind,
                        proposed=_proposal(kind=kind, params={"variant": variant}),
                    )),
                )
                self.assertTrue(folded.ok)
        pipedrive = tool.run(
            ctx,
            _payload(_finding(
                summary="last_update_time aggregate is not distinguished",
                detail=(
                    "caches_cache_usage_distribution has no repeated cache id "
                    "with different values, so MAX, MIN, and FIRST agree"
                ),
                attack="wrong_agg_stage",
                proposed=None,
            )),
        )
        self.assertFalse(pipedrive.ok)
        self.assertTrue(pipedrive.flags["proposal_missing"])
        self.assertFalse(pipedrive.flags[CV.PROPOSAL_PRESENT_FLAG])
        # Through the bounded session: one compile correction, then the red
        # is accepted as submitted — SUBMITTED, never HARNESS_FAULT.
        bad = _payload(_finding(attack="wrong_agg_stage", proposed=_proposal(kind="wrong_agg_stage")))
        result, provider, _ = self.run_critic_session(
            POP, [[tool_use(SUBMIT, bad, "a")], [tool_use(SUBMIT, bad, "b")]], block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertIsNone(result.fault)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        correction = provider.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(correction["content"].startswith("[compile] uncompilable"))
        self.assertIn("variant_invalid=true", correction["content"])
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROPOSAL_TOOL,))
        self.assert_identities(result)

    def test_uncompilable_major_proposal_after_correction_exhaustion_does_not_spend_a_round(self):
        """A red actionable MAJOR after the correction budget is VOIDED (D4).

        The bounded session records the provider submission faithfully; the
        post-session boundary then voids the still-malformed proposal under
        `uncompilable_after_corrections`, counts it on the seat
        (`compile_correction_exhausted`) and continues — never a stage FAIL
        charged to the task (no POPULATION round), never a run abort (the
        `ProviderProtocolError` here once ended a 152-exchange metrology run
        over one finding).
        """
        from elt_taskgen import cli
        from elt_taskgen.models import FindingScreenStatus

        red = _payload(_finding(proposed=_proposal(params={"bogus": 1})))
        result, provider, session = self.run_critic_session(
            POP,
            [[text_block("no call")], [tool_use(SUBMIT, red, "b")], [tool_use(SUBMIT, red, "c")]],
            block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertIsNone(result.fault)
        self.assertEqual(dict(result.correction_kinds), {"schema": 1, "compile": 1})
        self.assertEqual(result.correction_count, S.SCHEMA_RETRIES)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROPOSAL_TOOL,))
        self.assertEqual(result.submitted_with_red_validators, 1)
        terminals = [t for t in result.turns if t.kind == "model" and t.category == "terminal"]
        self.assertEqual([t.outcome_code for t in terminals], [S.VALIDATOR_RED_AT_SUBMIT_CODE])
        self.assertEqual(result.action_trace[-1].outcome_code, S.VALIDATOR_RED_AT_SUBMIT_CODE)
        dumped = result.as_dict()
        self.assertEqual(dumped["red_validators_at_submit"], [CV.COMPILE_PROPOSAL_TOOL])
        self.assertEqual(dumped["submitted_with_red_validators"], 1)
        self.assertTrue(result.verify_chain())
        self.assertEqual(result.final, P.normalized_text_for(POP, json.loads(json.dumps(red))))
        self.assert_identities(result)

        # POST-SESSION: what the harness compiled red is VOIDED before the
        # screen — words verbatim, executable content withheld, INFO.
        findings = session.findings
        self.assertEqual(len(findings), 1)
        (finding,) = findings
        self.assertIs(finding.severity, Severity.MAJOR)
        self.assertIsNotNone(finding.proposed_case)
        self.assertEqual([c.ok for c in session.finding_checks], [False])
        self.assertEqual(session.compile_correction_exhausted, 0)
        (voided,) = CV.void_uncompilable_proposals(findings, session, result)
        self.assertIs(voided.severity, Severity.INFO)
        self.assertIsNone(voided.proposed_case)
        self.assertIsNone(voided.suggested_attack)
        self.assertEqual(voided.summary, finding.summary)
        self.assertEqual(voided.detail, finding.detail)
        self.assertIs(voided.screen.status, FindingScreenStatus.VOID)
        self.assertEqual(voided.screen.signals, (CV.UNCOMPILABLE_AFTER_CORRECTIONS,))
        self.assertIs(voided.screen.claimed_severity, Severity.MAJOR)
        self.assertEqual(voided.screen.withheld_proposal, finding.proposed_case)
        self.assertIn("param_unknown", voided.screen.evidence)
        self.assertEqual(session.compile_correction_exhausted, 1)
        # The RAW population-adversary finding would have failed the attack
        # stage on the POPULATION route (a paid proposer session, then the
        # round); the voided one is no finding there: not blocking, not a
        # protocol failure, and its withheld proposal is not re-certified.
        blocking, problems = cli._blocking_proposal_failures(list(findings), ())
        self.assertEqual([f.finding_id for f in blocking], [finding.finding_id])
        self.assertTrue(any("major" in p for p in problems))
        self.assertIs(cli._proposal_failure_route(list(findings), ()), RepairRoute.POPULATION)
        self.assertEqual(cli._blocking_proposal_failures([voided], ()), ([], []))
        self.assertEqual(cli._validated_executable_findings(TASK, [voided]), [voided])
        self.assertEqual(
            cli._claimed_critic_handoff(voided), (Severity.INFO, None, None)
        )
        self.assertIsNone(cli._critic_adjudication_block([voided]))
        # With no result the recorded checks still decide. Lower severity is
        # not a bypass: a malformed MINOR proposal is voided the same way.
        (again,) = CV.void_uncompilable_proposals(findings, session, None)
        self.assertIs(again.screen.status, FindingScreenStatus.VOID)
        minor = finding.model_copy(update={"severity": Severity.MINOR})
        (voided_minor,) = CV.void_uncompilable_proposals((minor,), session, None)
        self.assertIs(voided_minor.severity, Severity.INFO)
        self.assertIs(voided_minor.screen.claimed_severity, Severity.MINOR)
        self.assertEqual(session.compile_correction_exhausted, 3)
        # A green submission is returned untouched and records no red.
        good = _payload(_finding(proposed=_proposal()))
        result2, _, session2 = self.run_critic_session(POP, [[tool_use(SUBMIT, good, "a")]], block=POP_BLOCK)
        self.assertEqual(result2.red_validators_at_submit, ())
        self.assertEqual(result2.submitted_with_red_validators, 0)
        self.assertEqual(
            [t.outcome_code for t in result2.turns if t.kind == "model" and t.category == "terminal"],
            ["submitted"],
        )
        self.assertEqual(CV.void_uncompilable_proposals(session2.findings, session2, result2), session2.findings)
        # A finding red at submit whose LATER payload compiled green is not
        # voided: the checks are the last payload's.
        self.assertEqual(
            CV.void_uncompilable_proposals(session2.findings, session2, result),
            session2.findings,
        )
        # The signal stays out of every in-session projection vocabulary.
        self.assertNotIn(CV.UNCOMPILABLE_AFTER_CORRECTIONS, CV.GRAMMAR_CODES)
        self.assertNotIn(CV.UNCOMPILABLE_AFTER_CORRECTIONS, CV.SCREEN_LEXICON)


class ProposalMatrixTest(_CriticCase):
    def test_proposal_matrix_is_post_session_and_never_a_tool_in_any_manifest(self):
        """A24: the in-session `proposal_matrix` / `predict_check` verbs are
        NOT built. `project_proposal_matrix` is a plain function over the
        promoter's outcomes, and no role's wire manifest, harness-validator
        list, dispatch registry or declared registry names any of them —
        with every declared session enabled as well as under the shipped
        config."""
        self.assertTrue(callable(CV.project_proposal_matrix))
        self.assertNotIsInstance(CV.project_proposal_matrix, RG.Tool)
        self.assertFalse(hasattr(CV.project_proposal_matrix, "run"))
        self.assertTrue(set(CV.CRITIC_VALIDATOR_NAMES).isdisjoint(CV.POST_SESSION_PROJECTION_NAMES))
        forbidden = set(CV.POST_SESSION_PROJECTION_NAMES)
        roles = sorted({r.value for r in CouncilRole} | {"repair_proposer", "independent_implementer",
                                                          "independent_loader", "audit_triage"})
        enabled = _doc(**{role: {"enabled": True} for role in _roles_with_session_block()})
        enabled["roles"][POP]["session"]["measured_match_bit"] = True
        enabled["roles"][POP]["session"]["max_oracle_bits"] = 7
        for label, ctx in (("shipped", contextlib.nullcontext()), ("every session enabled", _agents(enabled))):
            with ctx:
                for role in roles:
                    with self.subTest(config=label, role=role):
                        manifest = P.role_behavior_manifest(role)
                        names = {t["name"] for t in manifest["tools"]}
                        names |= {v["name"] for v in manifest["harness_validators"]}
                        names |= set(RG.ToolRegistry.for_role(role).names)
                        names |= set(RG.ToolRegistry.declared_for_role(role).names)
                        names |= set(P.session_policy_for(role).allowlist)
                        self.assertTrue(names.isdisjoint(forbidden), names & forbidden)
                self.assertTrue(set(RG._registered_names()).isdisjoint(forbidden))
        # Post-session: the record is a pure function of the promoter's
        # outcomes, booleans only.
        outcome = attacks.PromotionOutcome(
            finding_id="population_adversary-00-abcd1234", case_name="proposed__population_adversary-00-abcd1234",
            kind="inner_join", promoted=False, reason="measured reward matrix does not match the proposed expectation",
            predicted=_expected(), measured={p.value: 0.25 for p in PopulationName},
            measured_pass={p.value: False for p in PopulationName}, fidelity={"passed": True},
            mismatches=("development: predicted FULL, measured reward 0.25",),
        )
        matrix = CV.project_proposal_matrix((outcome, outcome.model_dump(mode="json")))
        self.assertEqual(set(matrix), {outcome.finding_id})
        entry = matrix[outcome.finding_id]
        self.assertEqual(set(entry), {"promoted", "per_population", "fidelity_ok"})
        self.assertEqual(set(entry["per_population"]), {p.value for p in PopulationName})
        self.assertEqual(entry["per_population"]["development"], {"predicted": True, "measured_pass": False})
        self.assertTrue(entry["fidelity_ok"])
        self.assertFalse(entry["promoted"])
        self.assertEqual(CV.project_proposal_matrix(()), {})

    def test_population_adversary_manifest_has_no_proposal_matrix_verb(self):
        """A24: the adversary's wire is the forced `report_findings` alone
        under the shipped bounded config (with or without the match-bit flag);
        its harness validators are
        `compile_proposal` (+ `measured_match_bit` under the flag) and never
        a matrix verb; the default tool choice stays the forced tool."""
        forbidden = set(CV.POST_SESSION_PROJECTION_NAMES)
        forced = P.tool_choice_for(POP)
        shipped = P.role_behavior_manifest(POP)
        self.assertEqual([t["name"] for t in shipped["tools"]], [SUBMIT])
        self.assertEqual(shipped["harness_validators"], [{"name": CV.COMPILE_PROPOSAL_TOOL}])
        self.assertEqual(shipped["tool_choice_policy"], forced)
        self.assertEqual(P.session_policy_for(POP).allowlist, (CV.COMPILE_PROPOSAL_TOOL,))
        with _agents(_doc(**{POP: {"enabled": True}})):
            manifest = P.role_behavior_manifest(POP)
            self.assertEqual([t["name"] for t in manifest["tools"]], [SUBMIT])
            self.assertEqual(manifest["harness_validators"], [{"name": CV.COMPILE_PROPOSAL_TOOL}])
            self.assertEqual(manifest["tool_choice_policy"], forced)
            policy = P.session_policy_for(POP)
            self.assertEqual(policy.allowlist, (CV.COMPILE_PROPOSAL_TOOL,))
            self.assertEqual(P.session_tool_choice(policy), forced)
            self.assertEqual([t["name"] for t in policy.wire_tools], [SUBMIT])
        with _agents(_doc(**{POP: {"enabled": True, "measured_match_bit": True, "max_oracle_bits": 7}})):
            manifest = P.role_behavior_manifest(POP)
            self.assertEqual([t["name"] for t in manifest["tools"]], [SUBMIT])
            self.assertEqual([v["name"] for v in manifest["harness_validators"]],
                             [CV.COMPILE_PROPOSAL_TOOL, CV.MEASURED_MATCH_BIT_TOOL])
            names = {t["name"] for t in manifest["tools"]} | {v["name"] for v in manifest["harness_validators"]}
            self.assertTrue(names.isdisjoint(forbidden))
            self.assertEqual(P.session_tool_choice(P.session_policy_for(POP)), forced)
        self.assertEqual(P.role_behavior_manifest(POP), shipped)


class RejectedProposalRecordTest(tap._DemoFixtureMixin, unittest.TestCase):
    def test_rejected_proposal_json_carries_booleans_not_rewards(self):
        """`attacks._record_rejected_proposal` writes `projection_matrix`,
        the post-session `project_proposal_matrix` record: per finding, the
        promoted and fidelity booleans and the per-population predicted /
        measured-pass booleans — never a reward float, never the reason or
        mismatch sentences. (The raw dump beside it is unchanged.)"""
        wrong = dict(tap.INNER_JOIN_TRUTH)
        wrong[PopulationName.COUNTERFACTUAL] = True
        finding = tap._finding("pa-03-matrix", tap._proposal(wrong))
        result = attacks.promote_proposed_cases(self.task, [finding], self.workspace, self.gold)
        self.assertEqual(len(result.rejected), 1)
        outcome = result.rejected[0]
        self.assertTrue(outcome.measured)  # the raw outcome DOES carry rewards
        path = (self.workspace / "tasks" / self.task.task_id / "attacks" / outcome.case_name
                / attacks.REJECTED_PROPOSAL_FILENAME)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("projection_matrix", record)
        matrix = record["projection_matrix"]
        self.assertEqual(matrix, CV.project_proposal_matrix((outcome,)))
        self.assertEqual(set(matrix), {"pa-03-matrix"})
        entry = matrix["pa-03-matrix"]
        self.assertEqual(set(entry), {"promoted", "per_population", "fidelity_ok"})
        self.assertIs(entry["promoted"], False)
        self.assertEqual(
            entry["per_population"],
            {p.value: {"predicted": wrong[p], "measured_pass": tap.INNER_JOIN_TRUTH[p]} for p in PopulationName},
        )
        self.assertIs(entry["per_population"]["counterfactual"]["predicted"], True)
        self.assertIs(entry["per_population"]["counterfactual"]["measured_pass"], False)

        def leaves(value):
            if isinstance(value, dict):
                for v in value.values():
                    yield from leaves(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    yield from leaves(v)
            else:
                yield value

        for leaf in leaves(matrix):
            self.assertIsInstance(leaf, bool, leaf)
        text = json.dumps(matrix)
        for word in ("measured\"", "reward", "mismatch", "reason", "0.", "1.0"):
            self.assertNotIn(word, text)
        self.assertNotIn("measured_reward", text)
        # Unchanged beside it: the raw dump and the Phase 0 projection.
        self.assertIn("measured", record)
        self.assertEqual(record["projection"], PJ.project_promotion(outcome))


# ---------------------------------------------------------------------------
# measured_match_bit (flag F) and the registry gate
# ---------------------------------------------------------------------------

class MeasuredMatchBitTest(_CriticCase):
    def _mismatch_result(self, finding_id="population_adversary-00-x"):
        outcome = attacks.PromotionOutcome(
            finding_id=finding_id, case_name=f"proposed__{finding_id}", kind="inner_join", promoted=False,
            reason="measured reward matrix does not match the proposed expectation on 3 population(s)",
            predicted=_expected(), measured={p.value: 0.25 for p in PopulationName},
            measured_pass={p.value: False for p in PopulationName},
            mismatches=("development: predicted FULL, measured reward 0.25",),
        )
        return attacks.PromotionResult(task=TASK, promoted=(), rejected=(outcome,), outcomes=(outcome,), rewards={})

    def test_measured_match_bit_is_off_by_default_and_counts_one_oracle_bit(self):
        """SoT T1.1 flag F / A24: `roles.population_adversary.session.
        measured_match_bit` ships false, so the validator is neither
        declared nor run; when on, it is ONE aggregate bit (1 oracle bit,
        `per_session` 1) the harness runs on the first submitted payload and
        never again, in a scratch workspace — the matrices stay behind."""
        block = P.role_loop_limits(POP)
        self.assertIs(block["measured_match_bit"], False)
        self.assertEqual(S.SessionLimits.from_block(block).active_harness_validators, (CV.COMPILE_PROPOSAL_TOOL,))
        self.assertEqual(CV.critic_registry(POP).names, (CV.COMPILE_PROPOSAL_TOOL,))
        self.assertEqual(RG.ToolRegistry.for_role(POP).names, (CV.COMPILE_PROPOSAL_TOOL,))
        flagged = {**block, "enabled": True, "measured_match_bit": True, "max_oracle_bits": 7}
        self.assertEqual(S.SessionLimits.from_block(flagged).active_harness_validators,
                         (CV.COMPILE_PROPOSAL_TOOL, CV.MEASURED_MATCH_BIT_TOOL))
        self.assertEqual(CV.critic_registry(POP, flagged).names, (CV.COMPILE_PROPOSAL_TOOL, CV.MEASURED_MATCH_BIT_TOOL))
        tool = CV.critic_validator(CV.MEASURED_MATCH_BIT_TOOL)
        self.assertEqual((tool.cost.oracle_bits, tool.cost.per_session), (1, 1))
        self.assertTrue(tool.harness_only and tool.validator)
        self.assertEqual(tool.permitted_roles, frozenset({POP}))
        self.assertIn(CV.MEASURED_MATCH_BIT_TOOL, M.HARNESS_VALIDATOR_MODULES)
        # A session under the flag: compile (3 bits) + match bit (1 bit) on
        # the first submit, compile alone (3 bits) on the second = 7.
        ws = Path(self._tmp.name) / "ws"
        (ws / "tasks" / TASK.task_id / "populations" / "development" / "rows").mkdir(parents=True)
        session = CV.CriticSession(task=TASK, role=POP, gold=object(), workspace=ws)
        good = _payload(_finding(proposed=_proposal()))
        with mock.patch.object(attacks, "promote_proposed_cases", return_value=self._mismatch_result()) as promoter:
            result, provider, session = self.run_critic_session(
                POP, [[tool_use(SUBMIT, good, "a")], [tool_use(SUBMIT, good, "b")]], block=flagged, session=session,
            )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(promoter.call_count, 1)
        scratch = Path(promoter.call_args.args[2])
        self.assertTrue(str(scratch).startswith(str(self.root.resolve())))
        self.assertNotEqual(scratch, ws)
        self.assertTrue((scratch / "tasks" / TASK.task_id / "populations" / "development" / "rows").is_dir())
        self.assertEqual((result.oracle_bits_used, result.validator_run_count), (7, 3))
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        self.assertEqual(session.match_bit_calls, 1)
        validator_turns = [(t.tool_name, t.outcome_code) for t in result.turns if t.kind == "validator"]
        self.assertEqual(validator_turns, [(CV.COMPILE_PROPOSAL_TOOL, "compiles"), (CV.MEASURED_MATCH_BIT_TOOL, "mismatch"),
                                           (CV.COMPILE_PROPOSAL_TOOL, "compiles")])
        correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertIn("[promotion] mismatch", correction)
        self.assertIn("exact_match=false", correction)
        for value in ("0.25", "1.0", "reward", "LOST", "FULL"):
            self.assertNotIn(value, correction)
        self.assert_identities(result)
        # The per-finding function answers the same one bit.
        session.install_payload(good)
        with mock.patch.object(attacks, "promote_proposed_cases", return_value=self._mismatch_result()):
            diag = CV.measured_match_bit(session.context(self.root), {"finding_index": 0})
        self.assertEqual((diag.ok, diag.code, diag.flags[CV.EXACT_MATCH_FLAG]), (False, "mismatch", False))
        _texts(diag)
        # Without a gold handle the bit is a harness fault, never a guess.
        bare, ctx = self.session(POP)
        bare.install_payload(good)
        with self.assertRaises(S.ToolHarnessFault) as caught:
            CV.measured_match_bit(ctx, {"finding_index": 0})
        self.assertEqual(caught.exception.code, "no_gold_handle")

    def test_measured_match_bit_enters_fingerprint_when_enabled(self):
        """A24 / SoT T7 R-H: the match bit is wired — and its code hashed —
        only when the adversary's session is enabled AND the flag is on;
        each step moves the tool surface and the routing fingerprint, and
        the shipped config restores the exact fingerprint."""
        routing = P.load_role_routing(None)
        base = M.council_routing_fingerprint(routing)
        base_surface = M.tool_surface_sha256()
        self.assertEqual(M.critic_wired_validators()[POP], (CV.COMPILE_PROPOSAL_TOOL,))
        with _agents(_doc(**{POP: {"enabled": False}})):
            rollback_fp = M.council_routing_fingerprint(routing)
            rollback_surface = M.tool_surface_sha256()
            self.assertEqual(M.critic_wired_validators()[POP], ())
            self.assertNotEqual(rollback_fp, base)
        with _agents(_doc(**{POP: {"enabled": True, "measured_match_bit": True, "max_oracle_bits": 7}})):
            flagged_fp = M.council_routing_fingerprint(routing)
            flagged_surface = M.tool_surface_sha256()
            self.assertEqual(M.critic_wired_validators()[POP], (CV.COMPILE_PROPOSAL_TOOL, CV.MEASURED_MATCH_BIT_TOOL))
            digests = M.validator_digests()
            self.assertTrue(set(M.HARNESS_VALIDATOR_MODULES[CV.MEASURED_MATCH_BIT_TOOL]) <= set(digests["code"]))
            self.assertIn("elt_taskgen.review.tools.critic_validators", digests["code"])
            self.assertEqual(digests["binaries"], M.toolchain_pins())
            self.assertEqual(RG.ToolRegistry.for_role(POP).names, (CV.COMPILE_PROPOSAL_TOOL, CV.MEASURED_MATCH_BIT_TOOL))
            self.assertEqual([t["name"] for t in P.wire_tools_for(POP)], [SUBMIT])
        self.assertNotEqual(flagged_fp, base)
        self.assertNotEqual(flagged_surface, base_surface)
        self.assertNotEqual(rollback_surface, base_surface)
        self.assertEqual(M.council_routing_fingerprint(routing), base)
        self.assertEqual(M.tool_surface_sha256(), base_surface)


class RegistryGateTest(_CriticCase):
    def test_critic_validators_stay_off_the_registry_and_the_wire_while_disabled(self):
        """Roadmap R-A / R-H: declared for POP and SHC, the validators enter
        `ToolRegistry.for_role` only while the seat's block is enabled and
        never the wire (`harness_only`); `declared_for_role` (the runner
        roles' ungated TOOL view) stays empty for a critic seat — the
        ungated view of the validators is `critic_registry`."""
        for role, names in ((POP, (CV.COMPILE_PROPOSAL_TOOL,)), (SHC, (CV.COMPILE_PROBE_TOOL,))):
            with self.subTest(role=role):
                self.assertEqual(RG.ToolRegistry.for_role(role).names, names)
                self.assertEqual(RG.ToolRegistry.declared_for_role(role).names, ())
                self.assertEqual(CV.critic_registry(role).names, names)
                self.assertEqual(CV.critic_registry(role).wire_tools(), [])
                self.assertTrue(P.role_is_agentic(role))
                wire = P.wire_tools_for(role)
                sha = P.role_behavior_sha256(role)
                with _agents(_doc(**{role: {"enabled": False}})):
                    self.assertEqual(RG.ToolRegistry.for_role(role).names, ())
                    self.assertEqual(P.wire_tools_for(role), wire)
                    self.assertFalse(P.role_is_agentic(role))
                    self.assertNotEqual(P.role_behavior_sha256(role), sha)
                self.assertEqual(RG.ToolRegistry.for_role(role).names, names)
                self.assertEqual(P.role_behavior_sha256(role), sha)
        # A declared validator's name is REGISTERED (naming it from another
        # role is `tool_not_permitted`); the flagged match bit is declared
        # only under its flag, so under the shipped config it is unknown.
        self.assertTrue({CV.COMPILE_PROPOSAL_TOOL, CV.COMPILE_PROBE_TOOL} <= set(RG._registered_names()))
        self.assertNotIn(CV.MEASURED_MATCH_BIT_TOOL, RG._registered_names())
        for tool in CV.CRITIC_VALIDATORS:
            self.assertIsInstance(tool, RG.Tool)
            self.assertTrue(tool.harness_only)
        # A critic policy: the forced report_findings as the only terminal,
        # no abort (abstention is an empty findings list), one-shot wire.
        policy = CV.critic_policy(POP)
        self.assertEqual((policy.submit_tool, policy.abort_tool, policy.mode), (SUBMIT, "", "harness_validated"))
        self.assertEqual(policy.terminal_tool_names, (SUBMIT,))
        self.assertEqual([t["name"] for t in policy.wire_tools], [SUBMIT])
        self.assertEqual(policy.allowlist, (CV.COMPILE_PROPOSAL_TOOL,))
        self.assertEqual(policy.limits.as_manifest(), P.role_loop_limits(POP))
        with self.assertRaises(ValueError):
            CV.critic_policy("ambiguity_critic")
        with self.assertRaises(ValueError):
            CV.CriticSession(task=TASK, role="ambiguity_critic")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 4: the council-side entry for a critic session
# ---------------------------------------------------------------------------

class FindingsFromSessionTest(_CriticCase):
    def test_minor_and_info_cannot_bypass_session_handoff_validation(self):
        """Severity never turns malformed executable content into evidence.

        A MINOR proposal still compiles through the closed grammar, while an
        INFO proposal is rejected because informational findings are explicitly
        non-executable. Both exhaust only the bounded correction and are then
        VOIDED by the harness before screening or attack execution: no
        executable content survives, the words do, and the run continues.
        """
        from elt_taskgen.models import FindingScreenStatus

        cases = {
            "minor": _payload(_finding(
                severity="minor",
                proposed=_proposal(params={"bogus": 1}),
            )),
            "info": _payload(_finding(
                severity="info",
                proposed=_proposal(),
            )),
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                result, _, session = self.run_critic_session(
                    POP,
                    [
                        [tool_use(SUBMIT, payload, "a")],
                        [tool_use(SUBMIT, payload, "b")],
                    ],
                    block=POP_BLOCK,
                )
                self.assertEqual(
                    result.red_validators_at_submit,
                    (CV.COMPILE_PROPOSAL_TOOL,),
                )
                (screened,) = council.findings_from_session(
                    TASK,
                    CouncilRole.POPULATION_ADVERSARY,
                    result,
                    session,
                    TASK.content_hash()[:8],
                )
                self.assertIs(screened.severity, Severity.INFO)
                self.assertIsNone(screened.proposed_case)
                self.assertIs(screened.screen.status, FindingScreenStatus.VOID)
                self.assertIn(CV.UNCOMPILABLE_AFTER_CORRECTIONS, screened.screen.signals)
                self.assertEqual(screened.summary, payload["findings"][0]["summary"])
                self.assertEqual(session.compile_correction_exhausted, 1)
                # On the wire it is an INFO observation with no executable
                # content, which the byte-identical `run_council` parses as
                # no finding for the attack compiler.
                wire = json.loads(P.session_findings_text(POP, [screened]))
                self.assertEqual(wire["findings"][0]["severity"], "info")
                self.assertIsNone(wire["findings"][0]["proposed_case"])
                self.assertIsNone(wire["findings"][0]["suggested_attack"])

    def test_session_schema_requires_every_field_and_rejects_unknown_keys(self):
        """The session consumer mirrors the strict critic response schema."""
        valid = _payload(_finding(proposed=_proposal(
            passes=tuple(population.value for population in PopulationName)
        )))
        missing = json.loads(json.dumps(valid))
        del missing["findings"][0]["detail"]
        invented = json.loads(json.dumps(valid))
        invented["findings"][0]["accept"] = True
        result, _, _ = self.run_critic_session(
            POP,
            [
                [tool_use(SUBMIT, missing, "a")],
                [tool_use(SUBMIT, invented, "b")],
                [tool_use(SUBMIT, valid, "c")],
            ],
            block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 2, "compile": 0})
        self.assertEqual(result.final, P.normalized_text_for(POP, valid))

    def test_findings_from_session_is_the_only_path_from_a_session_to_the_screen(self):
        """`council.findings_from_session(task, role, result, session,
        id_suffix)` = `_parse_findings` -> `void_uncompilable_proposals` ->
        `screen_findings`: an actionable proposal the harness compiled red past
        the correction budget is VOIDED before the screen (never a provider
        protocol failure: the seat was told and did not fix it), a green
        session's findings arrive exactly as the one-shot screen would leave
        them, a `final` of None (a limit stop with no green draft, a caught
        policy violation) is no finding, an auto-submitted draft is parsed
        like a payload; and in the source tree NO other caller feeds a
        session's payload to `void_uncompilable_proposals` or
        `screen_findings`."""
        import ast
        import pathlib

        from elt_taskgen.models import FindingScreenStatus

        red = _payload(_finding(proposed=_proposal(params={"bogus": 1})))
        result, _, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, red, "a")], [tool_use(SUBMIT, red, "b")]], block=POP_BLOCK,
        )
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROPOSAL_TOOL,))
        suffix = TASK.content_hash()[:8]
        (voided,) = council.findings_from_session(
            TASK,
            CouncilRole.POPULATION_ADVERSARY,
            result,
            session,
            suffix,
        )
        self.assertIs(voided.screen.status, FindingScreenStatus.VOID)
        self.assertIn(CV.UNCOMPILABLE_AFTER_CORRECTIONS, voided.screen.signals)
        self.assertIs(voided.severity, Severity.INFO)
        self.assertEqual(session.compile_correction_exhausted, 1)
        # The role may be passed by name too and reaches the same boundary.
        (again,) = council.findings_from_session(TASK, POP, result, session, suffix)
        self.assertIs(again.screen.status, FindingScreenStatus.VOID)
        # The protocol-fault halt for output the parser cannot read is
        # untouched: malformed JSON in `final` is still a ProviderProtocolError.
        garbage = type("R", (), {"final": "not json {{{", "red_validators_at_submit": ()})()
        with self.assertRaises(council.ProviderProtocolError):
            council.findings_from_session(TASK, POP, garbage, session, suffix)
        # A GREEN session: byte-for-byte what run_council's own parse + screen
        # gives. Its POP major does not concede a hidden graded discriminator.
        good = _payload(_finding(proposed=_proposal(
            passes=tuple(population.value for population in PopulationName)
        )))
        result2, _, session2 = self.run_critic_session(POP, [[tool_use(SUBMIT, good, "a")]], block=POP_BLOCK)
        screened = council.findings_from_session(TASK, CouncilRole.POPULATION_ADVERSARY, result2, session2, suffix)
        expected = council.screen_findings(
            TASK, council._parse_findings(CouncilRole.POPULATION_ADVERSARY, result2.final, suffix)
        )
        self.assertEqual(screened, expected)
        self.assertIsNotNone(screened[0].proposed_case)
        # `final` None is incomplete protocol in production. Metrology may opt
        # into an empty scoring outcome without admitting a task. An
        # auto-submitted validator-green draft is parsed as a payload.
        limit_result = type("R", (), {"final": None, "red_validators_at_submit": ()})()
        with self.assertRaises(council.ProviderProtocolError):
            council.findings_from_session(
                TASK, POP, limit_result, session2, suffix
            )
        self.assertEqual(
            council.findings_from_session(
                TASK,
                POP,
                limit_result,
                session2,
                suffix,
                allow_no_submission=True,
            ),
            [],
        )
        draft_result = type("R", (), {"final": dict(good), "red_validators_at_submit": ()})()
        self.assertEqual(
            [f.summary for f in council.findings_from_session(TASK, POP, draft_result, session2, suffix)],
            [good["findings"][0]["summary"]],
        )
        bad_draft = type("R", (), {"final": {"findings": [{"severity": "loud", "summary": "x"}]}, "red_validators_at_submit": ()})()
        with self.assertRaises(council.ProviderProtocolError):
            council.findings_from_session(TASK, POP, bad_draft, session2, suffix)
        # STATIC: the only caller of `void_uncompilable_proposals` in the
        # package is `findings_from_session`; the only callers of
        # `screen_findings` are `run_council` and `findings_from_session`;
        # `findings_from_session` is called by the provider dispatch alone.
        src = pathlib.Path(council.__file__).resolve().parents[1]
        callers: dict[str, set[str]] = {"void_uncompilable_proposals": set(), "screen_findings": set(), "findings_from_session": set()}
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in callers:
                        callers[name].add(f"{path.relative_to(src)}::{node.name}")
        self.assertEqual(callers["void_uncompilable_proposals"], {"review/council.py::findings_from_session"})
        self.assertEqual(callers["screen_findings"], {"review/council.py::run_council", "review/council.py::findings_from_session"})
        self.assertEqual(callers["findings_from_session"], {"review/providers.py::_session_final_text"})


# ---------------------------------------------------------------------------
# Batch repair (2026-09-09): D1 pre-flight demand, D2 variant grammar, D3
# claim normalization, D4 void-not-abort — reproduced from the batch evidence
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
#: The failed re-earn's CORRECTED adversary response (recovery-live-attempt-
#: 20260909.json `corrected_response_path`): `params.variant = "second_hop"`,
#: prose "The second-hop join ...".  Read-only batch evidence.
_D3_TRANSCRIPT = (
    _REPO / "council" / "transcripts" / "population_adversary"
    / "c49624215026220ae935e5da6be5f675a41f178bcc892994a67bdf9af23b0f15.json"
)


def _public_with_variants():
    """`PublicIdentifierSet` with the attack-kind variants published. Since
    the 2026-09-09 batch repair `projection._vocabulary_identifiers` publishes
    them itself, so this is the real vocabulary; kept as an explicit context
    so the test reads the same either way."""
    original = PJ._vocabulary_identifiers

    def with_variants():
        return frozenset(original() | set(attacks.all_kind_variant_names()))

    return mock.patch.object(PJ, "_vocabulary_identifiers", with_variants)


def _public_without_variants():
    """`PublicIdentifierSet` as it read BEFORE the vocabulary published the
    attack-kind variants: a compile correction must then WITHHOLD the names
    (the transport gatekeeper would trip on them), never invent them."""
    original = PJ._vocabulary_identifiers

    def without_variants():
        return frozenset(original() - set(attacks.all_kind_variant_names()))

    return mock.patch.object(PJ, "_vocabulary_identifiers", without_variants)


class BatchRepairTest(_CriticCase):
    def test_d3_second_hop_prose_satisfies_the_second_hop_variant(self):
        """The failed re-earn, offline: the corrected response parsed by the
        council's own parser compiles green through the REAL
        `compile_proposal` — "second-hop" names `second_hop` — and the
        boundary that once raised over it is no longer reachable."""
        record = json.loads(_D3_TRANSCRIPT.read_text(encoding="utf-8"))
        session, ctx = self.session(POP)
        # The recorded response carries the wire's `role` echo beside the
        # findings; the validator takes the `report_findings` arguments.
        payload = {"findings": json.loads(record["response"])["findings"]}
        (finding,) = council._parse_findings(
            CouncilRole.POPULATION_ADVERSARY, record["response"], TASK.content_hash()[:8]
        )
        self.assertEqual(dict(finding.proposed_case.params), {"variant": "second_hop"})
        text = f"{finding.summary} {finding.detail} {finding.proposed_case.rationale}"
        self.assertNotIn("second_hop", text.lower())
        self.assertIn("second-hop", text.lower())
        _case, fidelity = attacks.validate_proposed_case(TASK, finding, finding.proposed_case)
        self.assertTrue(fidelity["passed"], fidelity["errors"])
        self.assertEqual(fidelity["requested"]["variant"], "second_hop")
        folded = CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL).run(ctx, payload)
        self.assertTrue(folded.ok, folded.render())
        self.assertTrue(folded.flags[CV.CLAIM_FIDELITY_FLAG])
        _texts(folded)
        # Normalize case and separators, then match whole words only;
        # customer_summary_copy must not match customer_summary.
        for prose, identifier, names in (
            ("The second-hop join", "second_hop", True),
            ("The Second Hop join", "second_hop", True),
            ("SECOND_HOP", "second_hop", True),
            ("a hop", "second_hop", False),
            ("customers rows", "customer", False),
            ("mart customer_summary, rule 5", "customer_summary", True),
            ("Customer Summary mart", "customer_summary", True),
            ("customer_summary_copy", "customer_summary", False),
            ("inner join changes customer_summary only", "customer_summary_copy", False),
        ):
            with self.subTest(prose=prose, identifier=identifier):
                self.assertIs(attacks.claim_names_identifier(prose, identifier), names)
        self.assertEqual(
            attacks.missing_claim_identifiers("second-hop on customer_summary", ("second_hop", "orders", "customer_summary")),
            ("orders",),
        )

    def test_d2_missing_or_unknown_variant_is_a_grammar_code_with_the_allowed_variants(self):
        """Batch report 156's shape (`wrong_agg_stage`, no variant): both
        pre-flights answer `variant_invalid` with the kind as subject and the
        registry's variants as public names — never a ValueError, never a
        guessed default.  The names travel only through the public identifier
        set the transport gatekeeper enforces (today: withheld; with the
        vocabulary published: named)."""
        proposal_tool = CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL)
        probe_tool = CV.critic_validator(CV.COMPILE_PROBE_TOOL)
        pipedrive_text = dict(
            summary="last_update_time aggregation is not distinguished",
            detail=(
                "deals_activity has no repeated deal id with different "
                "last_update_time values, so MAX, MIN, and FIRST agree"
            ),
        )
        missing = _payload(_finding(attack="wrong_agg_stage", proposed=_proposal(kind="wrong_agg_stage"), **pipedrive_text))
        unknown = _payload(_finding(
            attack="wrong_agg_stage",
            proposed=_proposal(kind="wrong_agg_stage", params={"variant": "max_as_min"}),
            **pipedrive_text,
        ))
        probe_only = _payload(_finding(attack="wrong_agg_stage", proposed=None, **pipedrive_text))
        for public, expected_names in ((False, ()), (True, ("filter_before_aggregate",))):
            with self.subTest(vocabulary_published=public):
                with (contextlib.nullcontext() if public else _public_without_variants()):
                    session, ctx = self.session(POP)
                    for label, payload in (("missing", missing), ("unknown", unknown)):
                        diag = proposal_tool.run(ctx, payload)
                        self.assertFalse(diag.ok, label)
                        self.assertEqual((diag.code, diag.subject), ("uncompilable", "wrong_agg_stage"), label)
                        self.assertTrue(diag.flags["variant_invalid"], label)
                        self.assertEqual(diag.names, expected_names, label)
                        rendered, transport = _texts(diag)
                        self.assertNotIn("directive", transport)
                        self.assertNotIn("max_as_min", transport)
                    # The adversary's bare probe (no proposal at all, as in the
                    # batch) is `proposal_missing` AND told the variants.
                    diag = proposal_tool.run(ctx, probe_only)
                    self.assertFalse(diag.ok)
                    self.assertTrue(diag.flags["proposal_missing"])
                    self.assertEqual(diag.subject, "wrong_agg_stage")
                    self.assertEqual(diag.names, expected_names)
                    _texts(diag)
                    # The shortcut attacker's probe side: the compiler refuses
                    # to guess, in the closed grammar.
                    session, ctx = self.session(SHC)
                    diag = probe_tool.run(ctx, probe_only)
                    self.assertFalse(diag.ok)
                    self.assertEqual(diag.subject, "wrong_agg_stage")
                    self.assertTrue(diag.flags["variant_invalid"])
                    self.assertEqual(diag.names, expected_names)
                    _texts(diag)
        self.assertEqual(attacks.allowed_kind_variants(AttackKind.WRONG_AGG_STAGE), ("filter_before_aggregate",))
        self.assertEqual(attacks.allowed_kind_variants(AttackKind.NO_OP), ())
        self.assertIn("second_hop", attacks.all_kind_variant_names())
        self.assertNotIn("", attacks.all_kind_variant_names())

    def test_d1_major_adversary_finding_without_a_proposal_is_corrected_in_session(self):
        """Batch reports 111/123: the adversary's MAJOR observation carried no
        `proposed_case` and slipped through the pre-flight green.  Now the
        session answers it with a `proposal_missing` compile correction
        ("major adversary finding requires an executable proposed_case"); a
        seat that attaches one on its next turn submits green, and a seat
        that does not is voided post-session with the counter recorded — the
        attack stage never sees an unresolved major from a validated seat."""
        from elt_taskgen.models import FindingScreenStatus

        bare = _payload(_finding(
            attack=None,
            summary="No graded population contains a customer without orders",
            detail="an INNER join would keep full reward on customer_summary",
        ))
        # A blindness claim: the exact wrong logic keeps full reward on every
        # population (an all-pass prediction; a proposal predicting a hidden
        # kill would be voided by the screen as self-nullifying).
        fixed = _payload(_finding(
            attack="inner_join",
            proposed=_proposal(passes=tuple(p.value for p in PopulationName)),
            summary="No graded population contains a customer without orders",
            detail="an INNER join would keep full reward on customer_summary",
        ))
        # 1. Told, then fixed in-session: one compile correction, SUBMITTED green.
        result, provider, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, bare, "a")], [tool_use(SUBMIT, fixed, "b")]], block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertTrue(correction.startswith("[compile] uncompilable"))
        self.assertIn("proposal_missing=true", correction)
        self.assertIn("proposal_present=false", correction)
        self.assertEqual(result.red_validators_at_submit, ())
        screened = council.findings_from_session(TASK, POP, result, session, TASK.content_hash()[:8])
        self.assertEqual(len(screened), 1)
        self.assertIsNotNone(screened[0].proposed_case)
        self.assertEqual(session.compile_correction_exhausted, 0)
        self.assert_identities(result)
        # 2. Told, and not fixed: accepted red, voided post-session, counted.
        result, _, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, bare, "a")], [tool_use(SUBMIT, bare, "b")]], block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROPOSAL_TOOL,))
        (voided,) = council.findings_from_session(TASK, POP, result, session, TASK.content_hash()[:8])
        self.assertIs(voided.severity, Severity.INFO)
        self.assertIs(voided.screen.status, FindingScreenStatus.VOID)
        self.assertEqual(voided.screen.signals, (CV.UNCOMPILABLE_AFTER_CORRECTIONS,))
        self.assertIn("proposal_missing", voided.screen.evidence)
        self.assertEqual(session.compile_correction_exhausted, 1)
        # A MINOR adversary observation is reviewable prose: green, untouched.
        minor = _payload(_finding(attack=None, severity="minor"))
        result, _, session = self.run_critic_session(POP, [[tool_use(SUBMIT, minor, "a")]], block=POP_BLOCK)
        self.assertEqual(result.correction_count, 0)
        (kept,) = council.findings_from_session(TASK, POP, result, session, TASK.content_hash()[:8])
        self.assertIs(kept.severity, Severity.MINOR)
        self.assertEqual(session.compile_correction_exhausted, 0)

    def test_shortcut_attacker_proposals_are_compiled_in_session_and_voided_when_exhausted(self):
        """The attack stage certifies the attacker's `proposed_case`s through
        the same closed grammar as the adversary's; a red one used to reach it
        as a protocol block nobody could correct in-session (batch reports
        123/156: `keys_only` proposals refused for one unlisted mart).  Now
        `compile_probe` compiles the payload's proposals too: a red proposal
        is a compile correction with the public names it failed to name, and
        one still red after the correction is voided, while the probe-side
        rule (red only when no probe compiles) is untouched."""
        from elt_taskgen.models import FindingScreenStatus

        tool = CV.critic_validator(CV.COMPILE_PROBE_TOOL)
        session, ctx = self.session(SHC)
        unfaithful = _payload(_finding(
            summary="skipping one source table survives on customer_summary",
            detail="omit the orders source and the mart still grades",
            attack="skip_extraction",
            proposed=_proposal("skip_extraction", {"skip_tables": ["orders", "customers"]}),
        ))
        diag = tool.run(ctx, unfaithful)
        self.assertFalse(diag.ok)
        self.assertEqual(diag.subject, "skip_extraction")
        self.assertTrue(diag.flags["claim_missing_identifier"])
        self.assertFalse(diag.flags[CV.CLAIM_FIDELITY_FLAG])
        self.assertEqual(diag.names, ("customers",))
        self.assertTrue(diag.flags[CV.EVERY_PROBE_COMPILES_FLAG])
        self.assertNotIn(CV.SAME_MUTANT_FLAG, diag.flags)
        _texts(diag)
        self.assertEqual([c.ok for c in session.finding_checks], [False])
        # A faithful proposal beside compiling probes: the probe fold, verbatim.
        faithful = _payload(
            _finding(attack="no_dedup", proposed=_proposal("no_dedup", {})),
            _finding(
                summary="skipping orders and customers survives on customer_summary",
                detail="omit the orders and customers sources",
                attack="skip_extraction",
                proposed=_proposal("skip_extraction", {"skip_tables": ["orders", "customers"]}),
            ),
        )
        diag = tool.run(ctx, faithful)
        self.assertTrue(diag.ok)
        self.assertEqual(set(diag.flags), {CV.IS_DIRECTIVE_FLAG, CV.PROBE_PRESENT_FLAG, CV.EVERY_PROBE_COMPILES_FLAG,
                                           CV.EXECUTABLE_PROBE_FLAG})
        self.assertTrue(diag.flags[CV.EXECUTABLE_PROBE_FLAG])
        self.assertEqual([c.ok for c in session.finding_checks], [True, True])
        # Through the bounded session: corrected once, still red, voided.
        result, provider, session = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, unfaithful, "a")], [tool_use(SUBMIT, unfaithful, "b")]], block=SHC_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertIn("claim_missing_identifier=true", correction)
        self.assertIn("names=customers", correction)
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROBE_TOOL,))
        # The post-session void reads the attacker's recorded checks exactly
        # as the adversary's, through the ONLY path from a session to the
        # screen (`compile_probe` red at submit, not `compile_proposal`).
        (voided,) = council.findings_from_session(TASK, SHC, result, session, TASK.content_hash()[:8])
        self.assertIs(voided.severity, Severity.INFO)
        self.assertIs(voided.screen.status, FindingScreenStatus.VOID)
        self.assertEqual(voided.screen.signals, (CV.UNCOMPILABLE_AFTER_CORRECTIONS,))
        self.assertEqual(session.compile_correction_exhausted, 1)
        # And it travels as an INFO observation the attack stage certifies
        # as nothing executable (no protocol block on the withheld proposal).
        wire = json.loads(P.session_findings_text(SHC, [voided]))
        self.assertIsNone(wire["findings"][0]["proposed_case"])
        self.assert_identities(result)

    def test_no_op_and_task_wide_proposals_compile_without_naming_every_mart(self):
        """Every batch task's shortcut attacker proposed `no_op` (params {}):
        under the current compiler that raised "attack kind 'no_op' has no
        AST mutation rule" out of claim fidelity and blocked the review as a
        protocol failure.  Directive-only and task-wide kinds now target the
        whole task statically and carry no per-mart naming requirement; a
        site-specific kind on a multi-mart task still must name each mart."""
        first = TASK.marts[0]
        second_name = "customer_summary_copy"
        second = first.model_copy(update={"name": second_name, "plan": first.plan.model_copy(update={"mart": second_name})})
        multi = TASK.model_copy(update={
            "marts": (first, second),
            "reference": TASK.reference.model_copy(update={"sql_by_mart": {
                first.name: TASK.reference.sql_by_mart[first.name],
                second_name: TASK.reference.sql_by_mart[first.name],
            }}),
        })
        text = dict(summary="every measure defaults to 0", detail="a keys-only or empty submission may grade")
        for kind in ("no_op", "skip_extraction", "keys_only", "constants"):
            with self.subTest(kind=kind):
                payload = _payload(_finding(attack=kind, proposed=_proposal(kind=kind), **text))
                session = CV.CriticSession(task=multi, role=SHC)
                diag = CV.critic_validator(CV.COMPILE_PROBE_TOOL).run(session.context(self.root), payload)
                self.assertTrue(diag.ok, diag.render())
                (finding,) = session.findings
                case, fidelity = attacks.validate_proposed_case(multi, finding, finding.proposed_case)
                self.assertTrue(fidelity["passed"], fidelity["errors"])
                self.assertEqual(fidelity["requested"]["target_marts"], sorted([first.name, second_name]))
                if kind in ("no_op", "skip_extraction"):
                    sql_by_mart = attacks.materialize_mutation(multi, case, None)
                    requested, realized, checks = attacks._validate_realized_fidelity(multi, case, sql_by_mart, None)
                    self.assertEqual(requested, realized)
                    self.assertIn("closed materializer", checks[0])
        site_specific = _payload(_finding(attack="inner_join", proposed=_proposal(), summary="inner join changes customer_summary only", detail="x"))
        session = CV.CriticSession(task=multi, role=POP)
        diag = CV.critic_validator(CV.COMPILE_PROPOSAL_TOOL).run(session.context(self.root), site_specific)
        self.assertFalse(diag.ok)
        self.assertEqual(diag.names, (second_name,))


# End-to-end batch-repair regressions through the real bounded session,
# wire parsing, screening, and executable-finding validation.

def _through_review(role: str, result, session) -> tuple[list[Finding], list[Finding]]:
    """(post-session findings, the review stage's screened findings) — the
    production path from a session to `cli._validated_executable_findings`."""
    findings = council.findings_from_session(TASK, CouncilRole(role), result, session, TASK.content_hash()[:8])
    wire = P.session_findings_text(role, findings)
    reparsed = council._parse_findings(CouncilRole(role), wire, TASK.content_hash()[:8])
    return findings, council.screen_findings(TASK, reparsed)


def _review_probes(screened: list[Finding]) -> list[Finding]:
    """The review stage's diligence check (`cli.make_review_runner`): the
    shortcut attacker's findings that satisfy THE predicate,
    `council.is_executable_probe`."""
    return [
        f for f in screened
        if f.role is CouncilRole.SHORTCUT_ATTACKER and council.is_executable_probe(f)
    ]


class BatchRepairRoundTwoTest(_CriticCase):
    def test_attacker_bare_attack_is_a_proposal_missing_correction_then_voided(self):
        """Review finding 1-2: `compile_probe` recorded a green stub for every
        attacker finding without a `proposed_case`, so a MINOR finding naming
        an attack with `proposed_case: null` passed the pre-flight and the
        review stage then raised the protocol block "actionable minor finding
        names an attack but has no proposed_case" (`retry_guard=explicit`,
        blocked forever).  Now the bare attack is a `proposal_missing` compile
        correction (the promise of the tool description), a seat that attaches
        the case on its next turn submits green, and one that does not is
        voided post-session while the stage proceeds with its other probe."""
        from elt_taskgen import cli
        from elt_taskgen.models import FindingScreenStatus

        bare = _finding(attack="no_dedup", severity="minor", summary="no dedup survives",
                        detail="duplicate order_items rows keep customer_summary grading")
        green = _finding(attack="constants", severity="minor", proposed=_proposal("constants", {}),
                         summary="constants survive", detail="zeros keep customer_summary grading")
        # 1. Told and fixed: one compile correction naming the flag, green.
        fixed = _finding(attack="no_dedup", severity="minor", proposed=_proposal("no_dedup", {}),
                         summary="no dedup survives", detail="duplicate order_items rows keep customer_summary grading")
        result, provider, session = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, _payload(bare), "a")], [tool_use(SUBMIT, _payload(fixed), "b")]], block=SHC_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertTrue(correction.startswith("[compile] uncompilable"), correction)
        self.assertIn("proposal_missing=true", correction)
        self.assertIn("subject=no_dedup", correction)
        self.assertEqual(result.red_validators_at_submit, ())
        _post, screened = _through_review(SHC, result, session)
        self.assertEqual(len(_review_probes(screened)), 1)
        cli._validated_executable_findings(TASK, screened)  # no protocol failure
        self.assert_identities(result)
        # 2. Told and not fixed, beside a valid proposal: the bare finding is
        #    voided, the valid probe survives, the stage proceeds.
        payload = _payload(bare, green)
        result, _, session = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, payload, "a")], [tool_use(SUBMIT, payload, "b")]], block=SHC_BLOCK,
        )
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROBE_TOOL,))
        post, screened = _through_review(SHC, result, session)
        self.assertIs(post[0].screen.status, FindingScreenStatus.VOID)
        self.assertEqual(post[0].screen.signals, (CV.UNCOMPILABLE_AFTER_CORRECTIONS,))
        self.assertEqual(session.compile_correction_exhausted, 1)
        self.assertIs(screened[0].severity, Severity.INFO)
        self.assertIsNone(screened[0].suggested_attack)
        validated = cli._validated_executable_findings(TASK, screened)
        self.assertEqual(len(_review_probes(validated)), 1)
        blocking, problems = cli._blocking_proposal_failures(screened, ())
        self.assertEqual(problems, [])
        # 3. The exact D2 batch shape (`wrong_agg_stage`, no variant, no
        #    proposal): the correction names the registry's variants AND the
        #    missing proposal; still bare after it, the finding is voided
        #    instead of reaching the review stage as a protocol block.
        d2 = _payload(_finding(attack="wrong_agg_stage", severity="minor",
                               summary="aggregation stage", detail="filter placement on customer_summary"))
        result, provider, session = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, d2, "a")], [tool_use(SUBMIT, d2, "b")]], block=SHC_BLOCK,
        )
        correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertIn("proposal_missing=true", correction)
        self.assertIn("variant_invalid=true", correction)
        post, screened = _through_review(SHC, result, session)
        self.assertIs(post[0].screen.status, FindingScreenStatus.VOID)
        cli._validated_executable_findings(TASK, screened)  # INFO bare: no problem
        # An INFO suggestion is not a handoff: beside a compiling probe it
        # draws no correction (alone it is the probe side's "zero probes").
        info = _payload(_finding(attack="no_dedup", severity="info", summary="idea", detail="maybe"), green)
        result, _, _ = self.run_critic_session(SHC, [[tool_use(SUBMIT, info, "a")]], block=SHC_BLOCK)
        self.assertEqual(result.correction_count, 0)

    def test_diligence_is_one_predicate_from_preflight_to_screen_to_review(self):
        """Review findings 1-2 and 1-3 (the decision): ONE diligence rule.
        The review stage's rule in `cli.make_review_runner` is the authority
        — `council.is_executable_probe`: above INFO, a `suggested_attack`
        matched by a `proposed_case` of the same kind — and the attacker's
        pre-flight (`CompileProbeTool`) and the screen's restoration
        (`_keep_shortcut_diligence`) apply the same predicate, so a payload
        that passes the pre-flight cannot fail review on diligence.  Three
        readings once disagreed: a compiling probe on an INFO finding beside
        a bare MINOR observation passed the pre-flight green (any compiling
        `suggested_attack` counted), drew no correction, and then blocked the
        review stage forever on `critic_shortcut_diligence_incomplete`."""
        import inspect

        from elt_taskgen import cli
        from elt_taskgen.models import FindingScreenStatus

        # 1. One definition, three call sites.
        self.assertIn("council.is_executable_probe(", inspect.getsource(cli.make_review_runner))
        self.assertIn("is_executable_probe", inspect.getsource(council._keep_shortcut_diligence))
        self.assertIn("is_executable_probe", inspect.getsource(CV.fold_compile_probe_payload))
        probe = council.is_executable_probe
        f = lambda **kw: Finding(finding_id="shortcut_attacker-00-abcdef12", role=CouncilRole.SHORTCUT_ATTACKER,
                                 summary="s", detail="d", **kw)
        matrix = {p: True for p in PopulationName}
        case = lambda kind: __import__("elt_taskgen.models", fromlist=["ProposedAttackCase"]).ProposedAttackCase(
            kind=kind, params={}, expected_pass=matrix, rationale="r")
        self.assertTrue(probe(f(severity=Severity.MINOR, suggested_attack=AttackKind.NO_DEDUP, proposed_case=case(AttackKind.NO_DEDUP))))
        self.assertFalse(probe(f(severity=Severity.INFO, suggested_attack=AttackKind.NO_DEDUP, proposed_case=case(AttackKind.NO_DEDUP))))
        self.assertFalse(probe(f(severity=Severity.MAJOR, suggested_attack=AttackKind.NO_DEDUP)))
        self.assertFalse(probe(f(severity=Severity.MAJOR, proposed_case=case(AttackKind.NO_DEDUP))))
        self.assertFalse(probe(f(severity=Severity.MAJOR, suggested_attack=AttackKind.CONSTANTS, proposed_case=case(AttackKind.NO_DEDUP))))

        # 2. The shapes the review rule refuses, red in-session with
        #    `executable_probe=false` and every probe/proposal bit honest:
        #    an INFO suggestion (never compiled), a bare observation, a
        #    proposal that names no attack — alone or together.  Nothing is
        #    malformed, so no grammar flag and nothing to void post-session.
        tool = CV.critic_validator(CV.COMPILE_PROBE_TOOL)
        info_probe = _finding(attack="no_dedup", severity="info", summary="idea", detail="maybe")
        bare_minor = _finding(attack=None, severity="minor", summary="an observation", detail="about customer_summary")
        proposal_only = _finding(attack=None, severity="minor", proposed=_proposal("no_dedup", {}),
                                 summary="no dedup survives", detail="duplicate order_items rows keep customer_summary grading")
        green = _finding(attack="no_dedup", severity="minor", proposed=_proposal("no_dedup", {}),
                         summary="no dedup survives", detail="duplicate order_items rows keep customer_summary grading")
        for label, payload in (
            ("info probe beside a bare minor observation", _payload(info_probe, bare_minor)),
            ("proposal without an attack beside an info probe", _payload(proposal_only, info_probe)),
            ("proposal without an attack alone", _payload(proposal_only)),
            ("info probe alone", _payload(info_probe)),
        ):
            with self.subTest(label=label):
                session, ctx = self.session(SHC)
                diag = tool.run(ctx, payload)
                _texts(diag)
                self.assertEqual((diag.ok, diag.code), (False, "uncompilable"))
                self.assertFalse(diag.flags[CV.EXECUTABLE_PROBE_FLAG])
                self.assertFalse(any(diag.flags.get(code, False) for code in CV.GRAMMAR_CODES), diag.flags)
                self.assertNotIn(CV.SAME_MUTANT_FLAG, diag.flags)
                self.assertTrue(all(c.ok for c in session.finding_checks))
        # And the shape the OLD pre-flight refused while the review rule
        # accepts it — a kind with no default bare probe (`wrong_agg_stage`)
        # carrying a valid proposal of that kind — is GREEN: the proposal is
        # the executable handoff, and the probe side's honest
        # `every_probe_compiles=false` is information, not a verdict.
        variant = _finding(attack="custom", severity="minor",
                           summary="the add_dedup variant survives on customer_summary",
                           detail="an added distinct over customer_summary rows keeps grading",
                           proposed=_proposal("custom", {"variant": "add_dedup"}))
        for label, payload in (("valid proposal on a defaultless kind", _payload(variant)),
                               ("probe beside an info probe", _payload(info_probe, green))):
            with self.subTest(label=label):
                session, ctx = self.session(SHC)
                diag = tool.run(ctx, payload)
                _texts(diag)
                self.assertEqual((diag.ok, diag.code), (True, "compiles"), diag.render())
                self.assertTrue(diag.flags[CV.EXECUTABLE_PROBE_FLAG])
                self.assertIn(diag.subject, diag.names)
                self.assertFalse(any(diag.flags.get(code, False) for code in CV.GRAMMAR_CODES), diag.flags)
        session, ctx = self.session(SHC)
        diag = tool.run(ctx, _payload(variant))
        self.assertEqual((diag.subject, diag.names), ("custom", ("custom",)))
        self.assertFalse(diag.flags[CV.EVERY_PROBE_COMPILES_FLAG])  # the bare probe side, honestly
        result, _, session = self.run_critic_session(SHC, [[tool_use(SUBMIT, _payload(variant), "a")]], block=SHC_BLOCK)
        self.assertEqual((result.correction_count, result.red_validators_at_submit), (0, ()))
        self.assertEqual(len(_review_probes(_through_review(SHC, result, session)[1])), 1)

        # 3. Through the bounded session: told once (`executable_probe=false`),
        #    fixed on turn 2, green — and the review stage counts the probe.
        result, provider, session = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, _payload(info_probe, bare_minor), "a")],
                  [tool_use(SUBMIT, _payload(info_probe, green), "b")]], block=SHC_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertTrue(correction.startswith("[compile] uncompilable"), correction)
        self.assertIn("executable_probe=false", correction)
        self.assertEqual(result.red_validators_at_submit, ())
        _post, screened = _through_review(SHC, result, session)
        self.assertEqual(len(_review_probes(screened)), 1)
        cli._validated_executable_findings(TASK, screened)
        self.assert_identities(result)

        # 4. Told and not fixed: red at submit, nothing voided (no proposal
        #    was malformed), zero probes at review — and the review stage
        #    then FAILs on the attacker's route because the seat was told
        #    in-session, never the forever block.
        result, provider, session = self.run_critic_session(
            SHC, [[tool_use(SUBMIT, _payload(info_probe, bare_minor), "a")],
                  [tool_use(SUBMIT, _payload(info_probe, bare_minor), "b")]], block=SHC_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROBE_TOOL,))
        self.assertEqual(result.submitted_with_red_validators, 1)
        post, screened = _through_review(SHC, result, session)
        self.assertFalse(any(f.screen is not None and f.screen.status is FindingScreenStatus.VOID for f in post))
        self.assertEqual(session.compile_correction_exhausted, 0)
        self.assertEqual(_review_probes(screened), [])
        cli._validated_executable_findings(TASK, screened)  # no protocol problem either
        row = {"role": "shortcut_attacker", "entry_schema": 3,
               "correction_kinds": dict(result.correction_kinds),
               "submitted_with_red_validators": result.submitted_with_red_validators}
        self.assertTrue(cli._attacker_corrected_in_session((row,)))

        # 5. Every payload the pre-flight passes GREEN yields at least one
        #    probe at the review stage's rule after the real screen — the
        #    invariant the decision names.
        constants = _finding(attack="constants", severity="minor", proposed=_proposal("constants", {}),
                             summary="constants survive", detail="zeros keep customer_summary grading")
        twin = dict(green)
        for label, payload in (
            ("one probe", _payload(green)),
            ("probe beside an info probe", _payload(info_probe, green)),
            ("probe beside a bare observation", _payload(bare_minor, green)),
            ("two probes of one mutant", _payload(green, twin)),
            ("two probes of two mutants", _payload(green, constants)),
            ("probe beside a proposal without an attack", _payload(proposal_only, constants)),
        ):
            with self.subTest(label=label):
                result, _, session = self.run_critic_session(
                    SHC, [[tool_use(SUBMIT, payload, "a")]], block=SHC_BLOCK,
                )
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual(result.correction_count, 0)
                self.assertEqual(result.red_validators_at_submit, ())
                _post, screened = _through_review(SHC, result, session)
                self.assertGreaterEqual(len(_review_probes(screened)), 1, label)
                cli._validated_executable_findings(TASK, screened)

    def test_fatal_adversary_observation_without_an_attack_is_reviewable_prose(self):
        """Review finding 1-4: the adversary's mandatory-proposal rule reached
        FATAL, so a FATAL bare observation ("a graded population's conditions
        contradict the prose" — a rejection reason with no executable form)
        was voided after one correction and the task proceeded.  It is now
        reviewable prose at FATAL: no correction, no void, and the review
        stage's `fatal` path sees it.  A MAJOR bare adversary claim is still
        corrected and voided (D1), and a FATAL that names an attack is still
        an active handoff that must compile."""
        from elt_taskgen import cli

        fatal = _payload(_finding(attack=None, severity="fatal",
                                  summary="the stress population contradicts rule 2",
                                  detail="its stated customers conditions cannot hold with the customer_summary prose"))
        result, _, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, fatal, "a")], [tool_use(SUBMIT, fatal, "b")]], block=POP_BLOCK,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.correction_count, 0)
        self.assertEqual(result.red_validators_at_submit, ())
        post, screened = _through_review(POP, result, session)
        self.assertIs(post[0].severity, Severity.FATAL)
        self.assertIsNone(post[0].screen)
        self.assertEqual([f.severity for f in screened], [Severity.FATAL])
        cli._validated_executable_findings(TASK, screened)  # a bare observation, at every severity
        self.assertEqual(session.compile_correction_exhausted, 0)
        # MAJOR bare: still the D1 correction + void.
        major = _payload(_finding(attack=None, severity="major"))
        result, _, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, major, "a")], [tool_use(SUBMIT, major, "b")]], block=POP_BLOCK,
        )
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        post, _ = _through_review(POP, result, session)
        self.assertIs(post[0].severity, Severity.INFO)
        # FATAL naming an attack with no proposal: STILL a defect claim, not
        # an attack proposal (the decision on 1-4) — no correction, no void,
        # it travels at FATAL with its attack, and the review stage's
        # `fatal` path sees it.
        fatal_attack = _payload(_finding(attack="inner_join", severity="fatal",
                                         summary="the stress population contradicts rule 2",
                                         detail="its stated customers conditions cannot hold with the customer_summary prose"))
        result, provider, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, fatal_attack, "a")], [tool_use(SUBMIT, fatal_attack, "b")]], block=POP_BLOCK,
        )
        self.assertEqual(result.correction_count, 0)
        self.assertEqual(result.red_validators_at_submit, ())
        post, screened = _through_review(POP, result, session)
        self.assertIs(post[0].severity, Severity.FATAL)
        self.assertIs(post[0].suggested_attack, AttackKind.INNER_JOIN)
        self.assertIsNone(post[0].screen)
        self.assertEqual([f.severity for f in screened], [Severity.FATAL])
        cli._validated_executable_findings(TASK, screened)  # an unresolved fatal claim, never protocol
        self.assertEqual(session.compile_correction_exhausted, 0)
        for attack in (None, AttackKind.INNER_JOIN):
            for role in (POP, SHC):
                self.assertFalse(CV._requires_executable_proposal(
                    Finding(finding_id="population_adversary-00-abcdef12", role=CouncilRole.POPULATION_ADVERSARY,
                            severity=Severity.FATAL, summary="s", detail="d", suggested_attack=attack), role))
        # A FATAL that DOES carry a proposal: the proposal is still compiled
        # and a red one is still a correction (the seat hears about it), but
        # the finding is NEVER voided by the pre-flight — it keeps its
        # severity and its executable fields for the review stage.
        unfaithful = _finding(attack="skip_extraction", severity="fatal",
                              summary="the stress population contradicts rule 2",
                              detail="skipping orders keeps customer_summary grading",
                              proposed=_proposal("skip_extraction", {"skip_tables": ["orders", "customers"]}))
        result, provider, session = self.run_critic_session(
            POP, [[tool_use(SUBMIT, _payload(unfaithful), "a")], [tool_use(SUBMIT, _payload(unfaithful), "b")]],
            block=POP_BLOCK,
        )
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        self.assertIn("claim_missing_identifier=true", provider.calls[1]["messages"][-1]["content"][0]["content"])
        self.assertEqual(result.red_validators_at_submit, (CV.COMPILE_PROPOSAL_TOOL,))
        post, screened = _through_review(POP, result, session)
        self.assertIs(post[0].severity, Severity.FATAL)
        self.assertIsNotNone(post[0].proposed_case)
        self.assertIsNone(post[0].screen)
        self.assertEqual([f.severity for f in screened], [Severity.FATAL])
        self.assertEqual(session.compile_correction_exhausted, 0)

    def test_non_finite_params_are_a_schema_correction_never_a_harness_fault(self):
        """Review finding 1-1: `json.loads` accepts `NaN` / `Infinity` /
        `-Infinity` (and overflows `1e999` to inf), the schema check passed
        the payload, and `canonical_json(allow_nan=False)` then raised a bare
        ValueError out of the runner's submit — a ToolHarnessFault halt (exit
        2, no verdict) for a schema-valid model payload.  The decoder now
        refuses the literal as the teaching problem `validate_payload_for`
        reports, so the session issues a SCHEMA correction and a corrected
        second turn submits."""
        for text in ('{"variant": NaN}', '{"variant": Infinity}',
                     '{"hardcode_population": -Infinity}', '{"variant": 1e999}'):
            with self.subTest(params=text):
                proposal = dict(_proposal("inner_join"))
                proposal["params"] = text
                bad = _payload(_finding(proposed=proposal))
                problem = P.validate_payload_for(POP, json.loads(json.dumps(bad)))
                self.assertIsNotNone(problem)
                self.assertIn("non-finite number", problem)
                good = _payload(_finding(proposed=_proposal("inner_join")))
                result, provider, _ = self.run_critic_session(
                    POP, [[tool_use(SUBMIT, bad, "a")], [tool_use(SUBMIT, good, "b")]], block=POP_BLOCK,
                )
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual(dict(result.correction_kinds), {"schema": 1, "compile": 0})
                self.assertIsNone(result.fault)
                # A session answers a schema-invalid submit with the fixed
                # refusal text under `invalid_arguments` (never free text).
                correction = provider.calls[1]["messages"][-1]["content"][0]["content"]
                self.assertIn("invalid_arguments", correction)
                self.assert_identities(result)
        # A dict from an older transcript is held to the same rule.
        with self.assertRaises(ValueError):
            P._normalized_proposal({"proposed_case": {**_proposal("inner_join"), "params": {"x": float("inf")}}})
        self.assertEqual(P._normalized_proposal({"proposed_case": {**_proposal("inner_join"), "params": {"x": 1.5}}})["params"], {"x": 1.5})

"""The deterministic finding screen: junk findings and duplicate mutants.

WHY THIS EXISTS
Two defects measured in the real demo run (docs/runs/demo.md §6.2, §8.4):

  * `population_adversary-00` carried detail "This is a placeholder - see
    specific findings below." at severity `major`, and
    `population_adversary-01` carried detail "N/A" with a summary ending "No
    defect here, retracting." — also `major`. Nothing rejected either, and a
    downstream reader could not tell them from the finding that mattered.
  * the three ambiguity findings and `population_adversary-02` all compile to
    the SAME no_dedup mutant — four findings, one executable consequence, four
    separate executions.

These tests pin BOTH the behaviour and its limits: what the screen catches,
what it deliberately does not read (R02: withdrawal is the provider's
`disposition` field, and no sentence in a finding withdraws it), that it never
deletes or rewords a finding, and that everything it withholds is recorded on
the finding itself.
"""

from __future__ import annotations

import unittest

from elt_taskgen import demo_fixture
from elt_taskgen.models import (
    AttackKind,
    CouncilRole,
    Finding,
    FindingDisposition,
    FindingProvenance,
    FindingScreenStatus,
    PopulationName,
    ProposedAttackCase,
    Severity,
    TaskVariant,
)
from elt_taskgen.review import council
from elt_taskgen.verification import attacks

WITHDRAWN = FindingDisposition.WITHDRAWN


def _task():
    return demo_fixture.demo_task()


def _finding(fid, *, severity=Severity.MAJOR, summary="s", detail="d", attack=None,
             proposed=None, role=CouncilRole.AMBIGUITY_CRITIC,
             provenance=FindingProvenance.PROVIDER, disposition=None):
    return Finding(
        finding_id=fid,
        role=role,
        provenance=provenance,
        severity=severity,
        summary=summary,
        detail=detail,
        suggested_attack=attack,
        proposed_case=proposed,
        disposition=disposition,
    )


def _signals(out):
    return tuple(out.screen.signals) if out.screen else ()


GROUNDED = (
    "Rule 2 never says whether duplicate order_items rows for a customer_id "
    "should be summed twice in total_spend."
)


def _wire_constants_proposal() -> dict:
    """Strict provider-wire proposal used by raw-response test doubles."""
    populations = (
        "development",
        "primary",
        "resampled",
        "counterfactual",
        "stress",
    )
    return {
        "kind": "constants",
        "params": "{}",
        "expected_pass_by_stage": {
            "extract_load": {name: True for name in populations},
            "transform": {name: False for name in populations},
        },
        "rationale": "a constants shortcut must lose transform reward",
    }


class SelfNullifyingFindingTest(unittest.TestCase):
    """C1 — a finding that nullifies itself must not survive as a major."""

    def test_placeholder_detail_is_weak_evidence_and_demoted(self):
        task = _task()
        junk = _finding(
            "population_adversary-00",
            summary="No population guarantees a customer with orders but zero "
                    "completed orders, but more critically ...",
            detail="This is a placeholder - see specific findings below.",
        )
        out = council.screen_findings(task, [junk])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertNotIn("placeholder_text", out.screen.signals)
        self.assertIn("ungrounded_detail", out.screen.signals)
        self.assertIs(out.severity, Severity.INFO)
        self.assertIs(out.screen.claimed_severity, Severity.MAJOR)

    def test_a_closing_retraction_sentence_is_not_a_withdrawal(self):
        """The recorded demo finding still voids, on its empty detail alone.
        Its closing "No defect here, retracting." withdraws nothing; filed
        with disposition 'withdrawn', it carries both signals."""
        task = _task()
        recorded = dict(
            summary=(
                "Both development and stress populations state every customer "
                "has a completed order, so an INNER JOIN is only caught by "
                "primary/resampled/counterfactual, which do contain such "
                "customers. No defect here, retracting."
            ),
            detail="N/A",
        )
        out = council.screen_findings(
            task, [_finding("population_adversary-01", **recorded)]
        )[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertEqual(_signals(out), ("empty_detail",))

        out = council.screen_findings(task, [_finding(
            "population_adversary-01", disposition=WITHDRAWN, **recorded
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertEqual(_signals(out), ("empty_detail", "self_retracted"))

    def test_a_context_only_label_is_not_a_withdrawal(self):
        task = _task()
        labeled = dict(
            role=CouncilRole.POPULATION_ADVERSARY,
            summary="Only counterfactual catches the inner_join variant.",
            detail=(
                "The counterfactual population guarantees a childless customer, "
                "so inner_join loses reward there. This is reported as context "
                "for coverage only."
            ),
        )
        out = council.screen_findings(
            task, [_finding("population_adversary-02", **labeled)]
        )[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.MAJOR)

        out = council.screen_findings(task, [_finding(
            "population_adversary-02", disposition=WITHDRAWN, **labeled
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("self_retracted", out.screen.signals)
        self.assertIs(out.severity, Severity.INFO)

    def test_context_and_no_defect_clauses_are_not_withdrawals(self):
        """Neither clause withdraws. The ungrounded one is still weak evidence."""
        task = _task()
        cases = (
            (
                "Counterfactual guarantees the discriminator, so this is "
                "reported as context for coverage only.",
                (),
            ),
            ("No defect filed.", ("ungrounded_detail",)),
        )
        for index, (detail, prose_signals) in enumerate(cases):
            with self.subTest(detail=detail):
                out = council.screen_findings(task, [_finding(
                    f"context-{index}",
                    summary="rule 2 may be ambiguous",
                    detail=detail,
                )])[0]
                self.assertEqual(_signals(out), prose_signals)
                out = council.screen_findings(task, [_finding(
                    f"context-{index}",
                    summary="rule 2 may be ambiguous",
                    detail=detail,
                    disposition=WITHDRAWN,
                )])[0]
                self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
                self.assertIn("self_retracted", out.screen.signals)

    def test_a_void_never_deletes_or_rewords_the_finding(self):
        task = _task()
        junk = _finding("x-00", detail="N/A", summary="something. Retracting.")
        out = council.screen_findings(task, [junk])[0]
        self.assertEqual(out.finding_id, junk.finding_id)
        self.assertEqual(out.summary, junk.summary)
        self.assertEqual(out.detail, junk.detail)
        self.assertEqual(out.role, junk.role)

    def test_withheld_executable_content_is_recorded_not_erased(self):
        task = _task()
        proposal = ProposedAttackCase(
            kind=AttackKind.NO_DEDUP,
            expected_pass={p: (p is not PopulationName.STRESS) for p in PopulationName},
            rationale="only stress declares duplicate headers",
        )
        junk = _finding(
            "x-00", detail=GROUNDED, summary="rule 2 dedupe is ambiguous",
            attack=AttackKind.NO_DEDUP, proposed=proposal, disposition=WITHDRAWN,
        )
        out = council.screen_findings(task, [junk])[0]
        self.assertEqual(_signals(out), ("self_retracted",))
        self.assertIsNone(out.suggested_attack)
        self.assertIsNone(out.proposed_case)
        self.assertIs(out.screen.withheld_attack, AttackKind.NO_DEDUP)
        self.assertEqual(out.screen.withheld_proposal, proposal)

    def test_a_voided_finding_compiles_to_no_probe(self):
        task = _task()
        junk = _finding("x-00", detail="N/A", summary="placeholder",
                        attack=AttackKind.NO_DEDUP)
        screened = council.screen_findings(task, [junk])
        compiled = attacks.compile_attacks(
            task.model_copy(update={"attack_cases": ()}), screened
        )
        self.assertEqual(compiled, ())

    def test_thin_evidence_with_an_executable_probe_is_noted_not_voided(self):
        """The asymmetry: a terse finding that compiles to a mutant keeps its
        probe (the mutant is its evidence) and is flagged, not silenced."""
        task = _task()
        terse = _finding(
            "s-00", severity=Severity.MINOR,
            summary="constants shortcut must lose reward",
            detail="compile a constants mutant",
            attack=AttackKind.CONSTANTS, role=CouncilRole.SHORTCUT_ATTACKER,
        )
        out = council.screen_findings(task, [terse])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.NOTED)
        self.assertIn("ungrounded_detail", out.screen.signals)
        self.assertIs(out.suggested_attack, AttackKind.CONSTANTS)
        self.assertIs(out.severity, Severity.MINOR)
        # The contrast half: the same thin evidence with no probe is voided.
        probeless = _finding("x-00", summary="something is off", detail="N/A")
        out = council.screen_findings(task, [probeless])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)

    def test_a_withdrawal_voids_even_when_a_probe_is_attached(self):
        task = _task()
        retracting_prose = dict(
            detail=GROUNDED, attack=AttackKind.NO_DEDUP,
            summary="on inspection rule 2 is fine. No defect here, retracting.",
        )
        out = council.screen_findings(task, [_finding("x-00", **retracting_prose)])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.suggested_attack, AttackKind.NO_DEDUP)

        out = council.screen_findings(task, [_finding(
            "x-00", disposition=WITHDRAWN, **retracting_prose
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIsNone(out.suggested_attack)

    def test_recorded_filing_nothing_further_needs_the_disposition(self):
        """Recorded DLT shape: the detail closes by withdrawing the filing.

        As recorded (no disposition) the finding stays an active MAJOR claim
        and blocks. Filed with disposition 'withdrawn', it is voided on that
        field, and the evidence quotes the field, not the prose.
        """
        from elt_taskgen import cli

        task = _task()
        recorded = dict(
            summary=(
                "distinct_amount_count has two incompatible readings for "
                "customer_summary."
            ),
            detail=(
                "Withdrawn-scope check: rule 2 and the total_spend column "
                "description agree. I can state no second concrete reading; "
                "filing nothing further on this column."
            ),
        )
        out = council.screen_findings(task, [_finding("ambiguity_critic-00", **recorded)])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.MAJOR)
        self.assertEqual(cli._blocking_proposal_failures([out], ())[0], [out])

        out = council.screen_findings(task, [_finding(
            "ambiguity_critic-00", disposition=WITHDRAWN, **recorded
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertEqual(_signals(out), ("self_retracted",))
        self.assertIn("disposition 'withdrawn'", out.screen.evidence)
        self.assertNotIn("filing nothing further", out.screen.evidence)
        self.assertIs(out.screen.claimed_severity, Severity.MAJOR)
        self.assertEqual(cli._blocking_proposal_failures([out], ()), ([], []))

    def test_report_249_graded_fork_withdrawal_needs_the_disposition(self):
        """The Twitter critic disproved its own fork in one long sentence.

        Against the demo task the recorded detail names nothing of the task,
        so it is weak evidence; its closing withdrawal adds no signal. The
        same closing clause after grounded evidence leaves an active claim.
        """
        from elt_taskgen import cli

        task = _task()
        closing = (
            "both readings produce identical graded rows, so I withdraw this "
            "as a graded fork."
        )
        recorded = dict(
            summary="twitter_ads__account_report leaves a key/group-by fork.",
            detail=(
                "Section 1 says one row exists per account_id, placement and "
                "date_day, while Row content also lists the account_history "
                "attributes. Since the prose explicitly declares "
                "account_history, line_item_history and "
                "promoted_tweet_history to have at most one row per id, "
                + closing
            ),
        )
        fid = "ambiguity_critic-00-c24c53df"
        out = council.screen_findings(task, [_finding(fid, **recorded)])[0]
        self.assertEqual(_signals(out), ("ungrounded_detail",))

        grounded = _finding(fid, summary=recorded["summary"], detail=f"{GROUNDED} Yet {closing}")
        out = council.screen_findings(task, [grounded])[0]
        self.assertIsNone(out.screen)
        self.assertEqual(cli._blocking_proposal_failures([out], ())[0], [out])

        out = council.screen_findings(task, [_finding(fid, disposition=WITHDRAWN, **recorded)])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("self_retracted", out.screen.signals)
        self.assertIs(out.severity, Severity.INFO)
        self.assertIs(out.screen.claimed_severity, Severity.MAJOR)
        self.assertEqual(cli._blocking_proposal_failures([out], ()), ([], []))

    def test_recorded_twitter_no_defect_fork_with_metadata_aside(self):
        """The recorded prose adds no signal; only the disposition withdraws."""
        task = _task()
        recorded = _finding(
            "ambiguity_critic-00-current-twitter",
            summary="twitter_ads__account_report appears to leave a null fork.",
            detail=(
                "The two rules jointly and consistently cover clicks and "
                "conversion_custom_sale_amount, and I can state no second "
                "reading that changes a graded value. Withdrawing this as a "
                "fork — no defect is claimed. (Filed at info per instruction "
                "not to fabricate; see other findings.)"
            ),
        )
        out = council.screen_findings(task, [recorded])[0]
        self.assertEqual(_signals(out), ("ungrounded_detail",))

        withdrawn = _finding(
            recorded.finding_id, summary=recorded.summary,
            detail=recorded.detail, disposition=WITHDRAWN,
        )
        out = council.screen_findings(task, [withdrawn])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("self_retracted", out.screen.signals)
        self.assertIs(out.severity, Severity.INFO)

        live = recorded.model_copy(update={
            "detail": recorded.detail + " But total_spend is still ambiguous."
        })
        self.assertIsNone(council.screen_findings(task, [live])[0].screen)

    def test_graded_fork_words_do_not_overmatch_live_claims(self):
        """Negation, attribution and a trailing counterclaim remain live."""
        task = _task()
        details = (
            GROUNDED + " I do not withdraw this as a graded fork.",
            GROUNDED + (
                ' The note says "I withdraw this as a graded fork", but the '
                "two outputs still differ."
            ),
            GROUNDED + (
                " A reviewer might withdraw this as a graded fork, but the "
                "customer_summary outputs still differ."
            ),
            GROUNDED + (
                " I withdraw this as a style concern, not as a graded fork."
            ),
        )
        for index, detail in enumerate(details):
            with self.subTest(detail=detail):
                out = council.screen_findings(task, [_finding(
                    f"ambiguity_critic-live-{index}", detail=detail
                )])[0]
                self.assertIsNone(out.screen)
                self.assertIs(out.severity, Severity.MAJOR)

    def test_proposal_that_disclaims_the_scope_it_compiles_is_voided(self):
        """A bare kind is global; this critic explicitly claims one mart only."""
        task = _task()
        other = task.marts[0].model_copy(update={"name": "other_mart"})
        task = task.model_copy(update={"marts": task.marts + (other,)})
        proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN,
            expected_pass={p: True for p in PopulationName},
            rationale=(
                "If the mutant is applied globally to other_mart as well, "
                "counterfactual would catch it; this prediction is for the "
                "customer_summary join specifically."
            ),
        )
        finding = _finding(
            "population_adversary-00",
            role=CouncilRole.POPULATION_ADVERSARY,
            summary="An inner join in customer_summary is not distinguished.",
            detail=GROUNDED,
            proposed=proposal,
        )
        out = council.screen_findings(task, [finding])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("self_retracted", out.screen.signals)
        self.assertIn("retracts its own executable scope", out.screen.evidence)
        self.assertEqual(out.screen.withheld_proposal, proposal)
        self.assertIsNone(out.proposed_case)

    def test_scope_screen_requires_both_halves_and_multiple_marts(self):
        """Ordinary caveats and scoped prose are not enough to erase a claim."""
        task = _task()
        other = task.marts[0].model_copy(update={"name": "other_mart"})
        multi = task.model_copy(update={"marts": task.marts + (other,)})
        rationales = (
            # Global applicability is discussed, but no narrower claim is made.
            "If the mutant is applied globally, every population still passes.",
            # One mart is discussed, but the critic does not admit the submitted
            # executable has a different scope.
            "This prediction is for the customer_summary join specifically.",
            # Both phrases occur, but in the non-withdrawing order and with no
            # claim that the global result differs.
            "This prediction is for customer_summary specifically. If the "
            "mutant is applied globally, every population still passes.",
        )
        for index, rationale in enumerate(rationales):
            with self.subTest(rationale=rationale):
                proposal = ProposedAttackCase(
                    kind=AttackKind.INNER_JOIN,
                    expected_pass={p: True for p in PopulationName},
                    rationale=rationale,
                )
                finding = _finding(
                    f"population_adversary-{index:02d}",
                    role=CouncilRole.POPULATION_ADVERSARY,
                    detail=GROUNDED,
                    proposed=proposal,
                )
                out = council.screen_findings(multi, [finding])[0]
                self.assertIsNone(out.screen)
                self.assertEqual(out.proposed_case, proposal)

        single_proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN,
            expected_pass={p: True for p in PopulationName},
            rationale=(
                "If the mutant is applied globally, counterfactual catches it; "
                "this prediction is for customer_summary specifically."
            ),
        )
        single = _finding(
            "population_adversary-single",
            role=CouncilRole.POPULATION_ADVERSARY,
            detail=GROUNDED,
            proposed=single_proposal,
        )
        out = council.screen_findings(task, [single])[0]
        self.assertIsNone(out.screen)
        self.assertEqual(out.proposed_case, single_proposal)

    def test_population_major_predicting_a_hidden_catch_is_voided(self):
        """Report 252: one admitted discriminator refutes POP blindness."""
        task = _task()
        expected = {population: True for population in PopulationName}
        expected[PopulationName.COUNTERFACTUAL] = False
        proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN,
            expected_pass=expected,
            expected_pass_by_stage={
                TaskVariant.EXTRACT_LOAD: {
                    population: True for population in PopulationName
                },
                TaskVariant.TRANSFORM: expected,
            },
            rationale=(
                "counterfactual guarantees row B for customer_summary, so it "
                "catches the proposed INNER join"
            ),
        )
        finding = _finding(
            "population_adversary-00-report-252",
            role=CouncilRole.POPULATION_ADVERSARY,
            summary=(
                "Only counterfactual exercises the customer_summary LEFT join."
            ),
            detail=(
                "Rule 2 requires customer_summary to retain every customer_id; "
                "counterfactual contains the childless discriminator."
            ),
            attack=AttackKind.INNER_JOIN,
            proposed=proposal,
        )

        out = council.screen_findings(task, [finding])[0]

        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("predicted_graded_discriminator", out.screen.signals)
        self.assertIn("counterfactual", out.screen.evidence)
        self.assertIs(out.severity, Severity.INFO)
        self.assertIs(out.screen.claimed_severity, Severity.MAJOR)
        self.assertIs(out.screen.withheld_attack, AttackKind.INNER_JOIN)
        self.assertEqual(out.screen.withheld_proposal, proposal)
        self.assertIsNone(out.suggested_attack)
        self.assertIsNone(out.proposed_case)

        from elt_taskgen import cli

        self.assertEqual(cli._blocking_proposal_failures([out], ()), ([], []))

    def test_population_prediction_screen_has_narrow_negative_guards(self):
        """Only provider-authored POP majors conceding a hidden catch void."""
        task = _task()

        def proposal_with_false(*populations: PopulationName) -> ProposedAttackCase:
            expected = {population: True for population in PopulationName}
            for population in populations:
                expected[population] = False
            return ProposedAttackCase(
                kind=AttackKind.INNER_JOIN,
                expected_pass=expected,
                expected_pass_by_stage={
                    TaskVariant.EXTRACT_LOAD: {
                        population: True for population in PopulationName
                    },
                    TaskVariant.TRANSFORM: expected,
                },
                rationale="customer_summary INNER join prediction",
            )

        hidden_catch = proposal_with_false(PopulationName.COUNTERFACTUAL)
        cases = (
            # All hidden populations pass: a live-blindness claim remains live.
            _finding(
                "population-adversary-all-true",
                role=CouncilRole.POPULATION_ADVERSARY,
                detail=GROUNDED,
                proposed=proposal_with_false(),
            ),
            # Development is visible debug data, never a graded discriminator.
            _finding(
                "population-adversary-development-only",
                role=CouncilRole.POPULATION_ADVERSARY,
                detail=GROUNDED,
                proposed=proposal_with_false(PopulationName.DEVELOPMENT),
            ),
            # A real mart-local concern may be unable to express target scope.
            _finding(
                "population-adversary-no-proposal",
                role=CouncilRole.POPULATION_ADVERSARY,
                detail=GROUNDED,
                proposed=None,
            ),
            # Other roles use proposals to establish different claims.
            _finding(
                "shortcut-attacker-hidden-catch",
                role=CouncilRole.SHORTCUT_ATTACKER,
                detail=GROUNDED,
                proposed=hidden_catch,
            ),
            # Minor probes are non-blocking evidence, not MAJOR blindness claims.
            _finding(
                "population-adversary-minor",
                role=CouncilRole.POPULATION_ADVERSARY,
                severity=Severity.MINOR,
                detail=GROUNDED,
                proposed=hidden_catch,
            ),
            # FATAL means population conditions contradict the prose, a distinct
            # mandate that is not erased by an attack prediction.
            _finding(
                "population-adversary-fatal",
                role=CouncilRole.POPULATION_ADVERSARY,
                severity=Severity.FATAL,
                detail=GROUNDED,
                proposed=hidden_catch,
            ),
        )

        for finding in cases:
            with self.subTest(finding=finding.finding_id):
                out = council.screen_findings(task, [finding])[0]
                self.assertIsNone(out.screen)
                self.assertEqual(out.proposed_case, finding.proposed_case)

    def test_a_grounded_substantive_finding_is_untouched(self):
        task = _task()
        good = _finding("a-00", detail=GROUNDED, attack=AttackKind.NO_DEDUP)
        out = council.screen_findings(task, [good])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.MAJOR)
        self.assertIs(out.suggested_attack, AttackKind.NO_DEDUP)

    def test_code_authored_leak_findings_are_never_screened(self):
        """The leak detectors are the firewall, not a proposal.

        The exemption is CODE PROVENANCE, not FATAL severity — see
        ProviderFatalScreeningTest for the incident that forced the
        distinction. This finding is exempt on the same terms as before.
        """
        task = _task()
        leak = _finding(
            "leak-00", severity=Severity.FATAL, detail="N/A",
            provenance=FindingProvenance.CODE,
        )
        out = council.screen_findings(task, [leak])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)

    def test_quoting_a_retraction_mid_argument_does_not_fire(self):
        """Quoted retraction words never withdraw a finding."""
        task = _task()
        good = _finding(
            "a-00",
            summary="One reading concludes there is no defect here; that "
                    "reading is wrong because rule 2 keys only on order_id.",
            detail=GROUNDED,
        )
        out = council.screen_findings(task, [good])[0]
        self.assertIsNone(out.screen)

        good = _finding(
            "a-01",
            summary=(
                "One reviewer recommended filing nothing further; that advice "
                "is wrong because rule 2 leaves total_spend ambiguous."
            ),
            detail=GROUNDED,
        )
        out = council.screen_findings(task, [good])[0]
        self.assertIsNone(out.screen)


#: Exact dbt8 findings whose fatal summaries were withdrawn in their details.
#: They bypassed the old severity-based screen and pin that regression.
APPLE_FATAL_00 = {
    "finding_id": "feasibility_reviewer-00-723a90c9",
    "summary": (
        "The ad_group_report source schema does not publish "
        "tap_total_downloads or total_downloads columns, which are required "
        "measures for mart output."
    ),
    "detail": (
        "The mart apple_search_ads__ad_group_report lists tap_total_downloads "
        "and total_downloads as output columns that must be derived from the "
        "source table ad_group_report (files backend). Per the conventions "
        "section, tap_total_downloads is computed as the sum of "
        "tap_new_downloads and tap_redownloads, and total_downloads is "
        "computed as the sum of new_downloads and redownloads. However, the "
        "published schema for ad_group_report contains only: ad_group_id, "
        "conversions, date, impressions, local_spend_amount, "
        "local_spend_currency, new_downloads, redownloads, tap_installs, "
        "tap_new_downloads, tap_redownloads, and taps. The columns "
        "tap_new_downloads and tap_redownloads exist in the source, so "
        "tap_total_downloads can be computed, but total_downloads requires "
        "both new_downloads and redownloads (both present). This is actually "
        "computable. Upon re-examination: the input columns ARE present. The "
        "defect is withdrawn."
    ),
}

APPLE_FATAL_01 = {
    "finding_id": "feasibility_reviewer-01-723a90c9",
    "summary": (
        "The ad_level_report source schema does not publish ad_group_id and "
        "campaign_id as key columns stated in the mart output grain, creating "
        "an ambiguity in row mapping."
    ),
    "detail": (
        "The mart apple_search_ads__ad_report specifies the grain as one row "
        "per ad_id, date_day, ad_group_id, campaign_id, currency, and states "
        "that rows of ad_level_report project this grain. The source schema "
        "ad_level_report includes ad_group_id, campaign_id, ad_id, date, and "
        "local_spend_currency (which maps to currency). The schema is "
        "complete and these columns are all present in the published "
        "ad_level_report. This finding is withdrawn upon verification."
    ),
}


class ProviderFatalScreeningTest(unittest.TestCase):
    """The exemption keys on PROVENANCE, not on SEVERITY.

    WHY THIS EXISTS
    `screen_findings` used to short-circuit every FATAL finding past the
    screen, on the premise that fatals are the code-side leak detectors and
    silencing one would hole the firewall. In production that premise is
    false in the only direction that matters: `run_council` returns early on
    any code-side fatal, so the ONLY findings this function ever sees are
    provider-authored — and a provider is free to type `"severity": "fatal"`.

    The dbt apple_search_ads review was rejected by two such findings, both of
    which withdraw their own claim in their own closing sentence.

    R02: the screen no longer reads withdrawal from prose. Those recorded
    fatals, filed without a disposition, stay fatal; filed with disposition
    'withdrawn', they are voided. These tests pin that AND its blast radius: a
    provider fatal that merely argues around hedging language is untouched, a
    provider fatal with thin evidence is NOTED and STILL FATAL (a rejection
    reason has no executable consequence by nature, so evidence-absence must
    not silence it), and a code-authored leak finding is exempt no matter what
    its text says.
    """

    def _provider_fatal(self, payload, disposition=None):
        return _finding(
            payload["finding_id"],
            severity=Severity.FATAL,
            summary=payload["summary"],
            detail=payload["detail"],
            role=CouncilRole.FEASIBILITY_REVIEWER,
            provenance=FindingProvenance.PROVIDER,
            disposition=disposition,
        )

    def test_the_recorded_apple_search_ads_fatals_stay_fatal_without_a_disposition(self):
        """Their closing sentences withdraw nothing; the review would reject."""
        task = _task()
        findings = [
            self._provider_fatal(APPLE_FATAL_00),
            self._provider_fatal(APPLE_FATAL_01),
        ]
        screened = council.screen_findings(task, findings)
        self.assertEqual(len(screened), 2)
        for out in screened:
            self.assertNotIn("self_retracted", _signals(out), out.finding_id)
            self.assertIs(out.severity, Severity.FATAL)

    def test_the_recorded_apple_search_ads_fatals_are_voided_by_the_disposition(self):
        task = _task()
        findings = [
            self._provider_fatal(APPLE_FATAL_00, WITHDRAWN),
            self._provider_fatal(APPLE_FATAL_01, WITHDRAWN),
        ]
        screened = council.screen_findings(task, findings)
        self.assertEqual(len(screened), 2)
        for out, source in zip(screened, findings):
            self.assertIsNotNone(out.screen, out.finding_id)
            self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
            self.assertIn("self_retracted", out.screen.signals)
            self.assertIs(out.severity, Severity.INFO)
            self.assertIs(out.screen.claimed_severity, Severity.FATAL)
            # never deleted, never reworded
            self.assertEqual(out.summary, source.summary)
            self.assertEqual(out.detail, source.detail)
        # ...and the review stage's rejection test (cli.run_review) sees none.
        self.assertEqual(
            [f for f in screened if f.severity is Severity.FATAL], []
        )

    def test_the_withdrawal_evidence_is_the_field_not_a_sentence(self):
        """The screen quotes no prose as the reason for a withdrawal."""
        task = _task()
        for payload, sentence in (
            (APPLE_FATAL_00, "the defect is withdrawn."),
            (APPLE_FATAL_01, "this finding is withdrawn upon verification."),
        ):
            with self.subTest(finding=payload["finding_id"]):
                out = council.screen_findings(
                    task, [self._provider_fatal(payload, WITHDRAWN)]
                )[0]
                self.assertIn("disposition 'withdrawn'", out.screen.evidence)
                self.assertNotIn(sentence, out.screen.evidence.lower())

    def test_a_genuine_fatal_that_merely_hedges_is_untouched(self):
        """THE OVER-CORRECTION GUARD.

        A real leak report that quotes and then REJECTS the 'this is not a
        defect' reading contains three separate retraction markers ('not a
        defect', 'no issue here', 'no defect'). No prose withdraws a finding,
        so the screen does not fire and the task is still rejected.
        """
        task = _task()
        genuine = _finding(
            "feasibility_reviewer-09",
            severity=Severity.FATAL,
            role=CouncilRole.FEASIBILITY_REVIEWER,
            summary=(
                "Solver prose leaks the reference aggregation for "
                "customer_summary: the task is solvable by transcription."
            ),
            detail=(
                "One reading says this is not a defect because the SQL sits "
                "in a comment; another insists there is no issue here at all "
                "since a solver would ignore it. Both are wrong. The prose "
                "reproduces the reference join of customers to order_items "
                "and the exact total_spend expression, so a solver can copy "
                "the answer without reasoning. Anyone claiming no defect has "
                "not read rule 2 against the leaked text; the leak stands and "
                "the task must be rejected."
            ),
        )
        out = council.screen_findings(task, [genuine])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)

    def test_a_fatal_report_about_placeholder_content_is_untouched(self):
        """The noun ``placeholder`` is evidence, not a self-nullification."""
        task = _task()
        genuine = _finding(
            "feasibility_reviewer-placeholder-defect",
            severity=Severity.FATAL,
            role=CouncilRole.FEASIBILITY_REVIEWER,
            summary=(
                "customer_summary contains a placeholder instead of the "
                "total_spend rule"
            ),
            detail=(
                "The customer_summary mart publishes a literal placeholder "
                "instead of deriving total_spend from order_items, so no "
                "solver can satisfy the required output."
            ),
        )
        out = council.screen_findings(task, [genuine])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)

    def test_a_placeholder_field_cannot_erase_a_substantive_fatal(self):
        """One stub field does not make the whole finding placeholder junk."""
        task = _task()
        cases = (
            (
                "Placeholder",
                "The customer_summary mart cannot derive total_spend because "
                "order_items publishes no monetary input column.",
            ),
            (
                "customer_summary cannot derive total_spend from order_items",
                "This is a placeholder",
            ),
            (
                "customer_summary cannot derive total_spend from order_items...",
                "This is a placeholder",
            ),
            (
                "customer_summary cannot derive total_spend from order_items, "
                "but more critically ...",
                "This is a placeholder",
            ),
        )
        for index, (summary, detail) in enumerate(cases):
            with self.subTest(summary=summary, detail=detail):
                out = council.screen_findings(task, [_finding(
                    f"feasibility-reviewer-mixed-placeholder-{index}",
                    severity=Severity.FATAL,
                    role=CouncilRole.FEASIBILITY_REVIEWER,
                    summary=summary,
                    detail=detail,
                )])[0]
                self.assertIs(out.severity, Severity.FATAL)
                if out.screen is not None:
                    self.assertNotEqual(out.screen.status, FindingScreenStatus.VOID)
                    self.assertNotIn("placeholder_text", out.screen.signals)

    def test_an_all_placeholder_fatal_is_still_voided(self):
        task = _task()
        out = council.screen_findings(task, [_finding(
            "feasibility-reviewer-all-placeholder",
            severity=Severity.FATAL,
            role=CouncilRole.FEASIBILITY_REVIEWER,
            summary="Placeholder",
            detail="This is a placeholder",
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("placeholder_text", out.screen.signals)
        self.assertIs(out.severity, Severity.INFO)

    def test_a_retraction_sentence_appended_to_a_genuine_fatal_is_not_a_withdrawal(self):
        """This was the known limit of reading withdrawal from prose: a real
        withdrawal sentence appended to a real defect report voided it. Under
        R02 the sentence withdraws nothing and the fatal stands; only the
        provider's disposition field withdraws, with the claimed severity and
        the finding's words kept on the record."""
        task = _task()
        leak = dict(
            severity=Severity.FATAL,
            role=CouncilRole.FEASIBILITY_REVIEWER,
            summary="Solver prose leaks the reference SQL for customer_summary.",
            detail=GROUNDED + " On reflection, no defect.",
        )
        out = council.screen_findings(task, [_finding("feasibility_reviewer-10", **leak)])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)

        out = council.screen_findings(task, [_finding(
            "feasibility_reviewer-10", disposition=WITHDRAWN, **leak
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIs(out.screen.claimed_severity, Severity.FATAL)
        self.assertIn("On reflection, no defect.", out.detail)

    def test_an_unpunctuated_fatal_quoting_a_marker_remains_fatal(self):
        """Newline/list boundaries prevent a quoted marker erasing evidence."""
        task = _task()
        out = council.screen_findings(task, [_finding(
            "feasibility_reviewer-12",
            severity=Severity.FATAL,
            role=CouncilRole.FEASIBILITY_REVIEWER,
            summary="No solver can compute total_spend for customer_summary",
            detail=(
                "The customer_summary mart requires total_spend over "
                "order_items\n- one reviewer argued no defect\n- order_items "
                "publishes no monetary column so the mart is unsatisfiable"
            ),
        )])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)
        self.assertIn("unsatisfiable", out.detail)

    def test_incidental_withdrawal_words_around_fatal_evidence_do_not_fire(self):
        """A marker anywhere in the final span is not a withdrawal clause."""
        task = _task()
        details = (
            # Incidental words precede the decisive evidence on one line.
            "The author wrote 'no defect', but order_items publishes no "
            "monetary column and customer_summary remains unsatisfiable",
            # Incidental words follow evidence, but explicitly reject them.
            "order_items publishes no monetary column; the note says no defect, "
            "but that note is false and customer_summary is unsatisfiable",
            # The final list item itself quotes the words without withdrawing.
            "customer_summary cannot compute total_spend from order_items\n"
            "- the missing monetary column is fatal\n"
            "- the phrase no defect appears only in the author's rejected note",
            # A negated withdrawal verb is evidence that the finding stands.
            "customer_summary is unsatisfiable because order_items has no "
            "price column; this finding is not withdrawn",
            # A bare source-domain word is not an authored withdrawal clause.
            "customer_summary cannot compute total_spend from order_items\n"
            "- final source status value\n"
            "- withdrawn",
            # Markdown blockquotes remain quotes, not list-item conclusions.
            "customer_summary cannot compute total_spend from order_items\n"
            "> no defect",
        )
        for index, detail in enumerate(details):
            with self.subTest(detail=detail):
                out = council.screen_findings(task, [_finding(
                    f"feasibility_reviewer-incidental-{index}",
                    severity=Severity.FATAL,
                    role=CouncilRole.FEASIBILITY_REVIEWER,
                    summary="customer_summary is not computable",
                    detail=detail,
                )])[0]
                self.assertIsNone(out.screen)
                self.assertIs(out.severity, Severity.FATAL)

    def test_a_terminal_list_item_withdrawal_needs_the_disposition(self):
        """A closing list item that withdraws the finding is still prose."""
        task = _task()
        listed = dict(
            severity=Severity.FATAL,
            role=CouncilRole.FEASIBILITY_REVIEWER,
            summary="customer_summary may be unsatisfiable",
            detail=(
                "I checked total_spend against order_items\n"
                "- both required source columns exist\n"
                "- this finding is withdrawn upon verification"
            ),
        )
        fid = "feasibility_reviewer-terminal-list"
        out = council.screen_findings(task, [_finding(fid, **listed)])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)

        out = council.screen_findings(task, [_finding(fid, disposition=WITHDRAWN, **listed)])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIn("self_retracted", out.screen.signals)

    def test_thin_evidence_never_voids_a_fatal(self):
        """A rejection reason has no mutant by nature — absence of an
        executable consequence must not be read as absence of a defect."""
        task = _task()
        for detail, signal in (
            ("N/A", "empty_detail"),
            ("The requirements cannot all be satisfied at once.",
             "ungrounded_detail"),
        ):
            with self.subTest(signal=signal):
                out = council.screen_findings(task, [_finding(
                    "feasibility_reviewer-11",
                    severity=Severity.FATAL,
                    role=CouncilRole.FEASIBILITY_REVIEWER,
                    summary="The specification cannot be satisfied by any solver.",
                    detail=detail,
                )])[0]
                self.assertEqual(out.screen.status, FindingScreenStatus.NOTED)
                self.assertIn(signal, out.screen.signals)
                self.assertIs(out.severity, Severity.FATAL)

    def test_the_same_thin_finding_at_major_is_still_voided(self):
        """The fatal carve-out is a carve-out, not a rewrite of the rule."""
        task = _task()
        out = council.screen_findings(task, [_finding(
            "a-00",
            severity=Severity.MAJOR,
            summary="The specification cannot be satisfied by any solver.",
            detail="N/A",
        )])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIs(out.severity, Severity.INFO)

    def test_a_code_finding_is_exempt_whatever_its_text_says(self):
        """Provenance decides, so no wording can screen the firewall away."""
        task = _task()
        out = council.screen_findings(task, [_finding(
            "leak-01",
            severity=Severity.FATAL,
            role=CouncilRole.SHORTCUT_ATTACKER,
            provenance=FindingProvenance.CODE,
            summary="Solver prose leaks private SQL from reference:customer_summary.",
            detail="N/A. No defect here, retracting.",
        )])[0]
        self.assertIsNone(out.screen)
        self.assertIs(out.severity, Severity.FATAL)

    def test_provenance_is_not_recoverable_from_role_or_severity(self):
        """Why the field had to be added: neither existing field separates the
        two authors. SHORTCUT_ATTACKER is both a leak-detector label and a real
        council seat, and FATAL is a severity a provider is free to claim."""
        task = demo_fixture.demo_task()
        leaked = task.model_copy(update={
            "solver_prompt": "Here is a hint:\n" + demo_fixture.REFERENCE_SQL
        })
        code_side = council.leak_findings(leaked)
        self.assertTrue(code_side)
        for f in code_side:
            self.assertIs(f.provenance, FindingProvenance.CODE)
            self.assertIs(f.role, CouncilRole.SHORTCUT_ATTACKER)
            self.assertIs(f.severity, Severity.FATAL)
        smuggled = (
            '{"findings": [{"severity": "fatal", "summary": "s", '
            '"detail": "d", "route_hint": null, '
            '"suggested_attack": null, "disposition": "active", "proposed_case": null, '
            '"provenance": "code"}]}'
        )
        with self.assertRaisesRegex(
            council.ProviderProtocolError, "unknown field.*provenance"
        ):
            council._parse_findings(
                CouncilRole.SHORTCUT_ATTACKER, smuggled, "deadbeef"
            )
        parsed = council._parse_findings(
            CouncilRole.SHORTCUT_ATTACKER,
            '{"findings": [{"severity": "fatal", "summary": "s", '
            '"detail": "d", "route_hint": null, '
            '"suggested_attack": null, "disposition": "active", "proposed_case": null}]}',
            "deadbeef",
        )
        # Same role and severity never confer code provenance; an explicit
        # provenance field is rejected above rather than silently trusted.
        self.assertIs(parsed[0].role, CouncilRole.SHORTCUT_ATTACKER)
        self.assertIs(parsed[0].severity, Severity.FATAL)
        self.assertIs(parsed[0].provenance, FindingProvenance.PROVIDER)

    def test_provenance_defaults_to_provider_so_the_exemption_is_earned(self):
        f = Finding(
            finding_id="x-00",
            role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.FATAL,
            summary="s",
        )
        self.assertIs(f.provenance, FindingProvenance.PROVIDER)


class RunCouncilFatalRetractionTest(unittest.TestCase):
    """End to end through `run_council`: the stage no longer rejects."""

    class _RetractingFatalProvider:
        """One seat returns the two recorded fatals whose prose withdraws
        them, filed under `disposition`; the shortcut seat returns a real
        probe so the run is otherwise well-formed."""

        def __init__(self, disposition="withdrawn"):
            self.disposition = disposition

        def complete(self, role, prompt):
            import json as _json

            if role is CouncilRole.FEASIBILITY_REVIEWER:
                return _json.dumps({"findings": [
                    {"severity": "fatal",
                     "summary": APPLE_FATAL_00["summary"],
                     "detail": APPLE_FATAL_00["detail"],
                     "route_hint": None, "suggested_attack": None,
                     "disposition": self.disposition,
                     "proposed_case": None},
                    {"severity": "fatal",
                     "summary": APPLE_FATAL_01["summary"],
                     "detail": APPLE_FATAL_01["detail"],
                     "route_hint": None, "suggested_attack": None,
                     "disposition": self.disposition,
                     "proposed_case": None},
                ]})
            if role is CouncilRole.SHORTCUT_ATTACKER:
                return _json.dumps({"findings": [
                    {"severity": "minor", "summary": "constants shortcut",
                     "detail": GROUNDED, "route_hint": None,
                     "suggested_attack": "constants", "disposition": "active",
                     "proposed_case": _wire_constants_proposal()},
                ]})
            return '{"findings": []}'

    class _GenuineFatalProvider:
        def complete(self, role, prompt):
            import json as _json

            if role is CouncilRole.FEASIBILITY_REVIEWER:
                return _json.dumps({"findings": [
                    {"severity": "fatal",
                     "summary": "No solver can compute total_spend: the "
                                "order_items table publishes no price column.",
                     "detail": "The customer_summary mart requires "
                               "total_spend over order_items, and order_items "
                               "declares no monetary column at all. There is "
                               "no reading of rule 2 under which this is "
                               "computable.",
                     "route_hint": None, "suggested_attack": None, "disposition": "active",
                     "proposed_case": None},
                ]})
            return '{"findings": []}'

    def test_two_withdrawn_fatals_no_longer_reject_the_task(self):
        findings = council.run_council(_task(), self._RetractingFatalProvider())
        fatal = [f for f in findings if f.severity is Severity.FATAL]
        self.assertEqual(fatal, [], "a withdrawn finding must not reject")
        voided = [f for f in findings
                  if f.screen and f.screen.status is FindingScreenStatus.VOID]
        self.assertEqual(len(voided), 2)
        for f in voided:
            self.assertIs(f.screen.claimed_severity, Severity.FATAL)
            self.assertIn("self_retracted", f.screen.signals)

    def test_the_same_fatals_filed_active_still_reject(self):
        """Their withdrawing prose is unchanged; only the field differs."""
        findings = council.run_council(
            _task(), self._RetractingFatalProvider(disposition="active")
        )
        fatal = [f for f in findings if f.severity is Severity.FATAL]
        self.assertEqual(len(fatal), 2)
        for f in fatal:
            self.assertNotIn("self_retracted", _signals(f))

    def test_a_genuine_unretracted_fatal_still_rejects(self):
        findings = council.run_council(_task(), self._GenuineFatalProvider())
        fatal = [f for f in findings if f.severity is Severity.FATAL]
        self.assertEqual(len(fatal), 1)
        self.assertIsNone(fatal[0].screen)
        self.assertIs(fatal[0].provenance, FindingProvenance.PROVIDER)


class ScreenLimitsTest(unittest.TestCase):
    """The screen's honesty: what it CANNOT catch is pinned too."""

    def test_a_rephrased_retraction_is_missed_by_design(self):
        """A retraction in any words survives: the screen reads withdrawal
        only from the disposition field (R02). Recorded, not hidden."""
        task = _task()
        sneaky = _finding(
            "a-00",
            summary="On further thought the order_items grain is unambiguous "
                    "and I am content to leave rule 2 as written.",
            detail=GROUNDED,
        )
        out = council.screen_findings(task, [sneaky])[0]
        self.assertIsNone(out.screen)  # prose never withdraws a finding

    def test_grounding_is_defeated_by_naming_one_real_column(self):
        """The structural floor is a floor, not a content judgement."""
        task = _task()
        thin = _finding("a-00", detail="Something is wrong with total_spend.")
        out = council.screen_findings(task, [thin])[0]
        self.assertIsNone(out.screen)

    def test_signal_catalogue_is_the_documented_one(self):
        self.assertEqual(
            set(council.SCREEN_SIGNALS),
            {"empty_detail", "ungrounded_detail",
             "predicted_graded_discriminator", "self_retracted",
             "placeholder_text"},
        )

    def test_vocabulary_holds_schema_and_population_names(self):
        vocab = council.task_vocabulary(_task())
        for token in ("customers", "orders", "order_items", "customer_summary",
                      "total_spend", "counterfactual", "postgres"):
            self.assertIn(token, vocab)
        self.assertNotIn("id", vocab)  # too short to ground anything


class MutantDedupTest(unittest.TestCase):
    """C2 — identical compiled mutants execute once."""

    def test_three_identical_kinds_collapse_to_one_execution(self):
        task = _task()
        findings = [
            _finding(f"a-{i:02d}", detail=GROUNDED, attack=AttackKind.NO_DEDUP)
            for i in range(3)
        ]
        screened = council.screen_findings(task, findings)
        compiled = attacks.compile_attacks(
            task.model_copy(update={"attack_cases": ()}), screened
        )
        self.assertEqual(len(compiled), 1)
        self.assertEqual(compiled[0].name, "finding__a-00")

    def test_suggestion_without_registered_default_is_never_guessed(self):
        task = _task().model_copy(update={"attack_cases": ()})
        finding = _finding(
            "population_adversary-pipedrive",
            detail=(
                "customer_summary total_spend has no repeated customer_id with "
                "different values, so MAX and MIN agree"
            ),
            attack=AttackKind.WRONG_AGG_STAGE,
            role=CouncilRole.POPULATION_ADVERSARY,
        )
        self.assertEqual(attacks.compile_attacks(task, [finding]), ())

    def test_the_representative_names_every_contributor(self):
        task = _task()
        findings = [
            _finding(f"a-{i:02d}", detail=GROUNDED, attack=AttackKind.NO_DEDUP)
            for i in range(3)
        ]
        screened = council.screen_findings(task, findings)
        rep = screened[0]
        self.assertEqual(rep.screen.status, FindingScreenStatus.REPRESENTATIVE)
        self.assertEqual(rep.screen.coalesced_from, ("a-01", "a-02"))
        for dup in screened[1:]:
            self.assertEqual(dup.screen.status, FindingScreenStatus.DUPLICATE)
            self.assertEqual(dup.screen.duplicate_of, "a-00")
            self.assertIs(dup.screen.withheld_attack, AttackKind.NO_DEDUP)
            self.assertIs(dup.severity, Severity.MAJOR)  # NOT demoted

    def test_different_mutations_of_one_kind_are_not_merged(self):
        """constants + 'emit development's outputs verbatim' compiles to a
        different mutant than a plain constants probe, and both must run."""
        task = _task()
        emission = _finding(
            "s-00", severity=Severity.MINOR, attack=AttackKind.CONSTANTS,
            summary="emit the development population's outputs verbatim",
            detail=GROUNDED,
        )
        plain = _finding(
            "s-01", severity=Severity.MINOR, attack=AttackKind.CONSTANTS,
            summary="fix completed_order_count at 1 for every customer_id",
            detail=GROUNDED,
        )
        screened = council.screen_findings(task, [emission, plain])
        self.assertTrue(all(f.screen is None for f in screened))
        compiled = attacks.compile_attacks(
            task.model_copy(update={"attack_cases": ()}), screened
        )
        self.assertEqual(len(compiled), 2)
        self.assertNotEqual(compiled[0].mutation, compiled[1].mutation)

    def test_a_probe_identical_to_a_promoted_proposal_yields_to_it(self):
        task = _task()
        proposal = ProposedAttackCase(
            kind=AttackKind.NO_DEDUP,
            expected_pass={p: (p is not PopulationName.STRESS) for p in PopulationName},
            rationale="only stress declares duplicate headers",
        )
        proposer = _finding(
            "p-02", detail=GROUNDED, attack=AttackKind.NO_DEDUP,
            proposed=proposal, role=CouncilRole.POPULATION_ADVERSARY,
            severity=Severity.MINOR,
        )
        echo = _finding("a-00", detail=GROUNDED, attack=AttackKind.NO_DEDUP)
        screened = council.screen_findings(task, [echo, proposer])
        compiled = attacks.compile_attacks(
            task.model_copy(update={"attack_cases": ()}), screened
        )
        self.assertEqual(compiled, ())  # the promoter executes this mutant
        for f in screened:
            self.assertEqual(f.screen.status, FindingScreenStatus.DUPLICATE)
            self.assertEqual(f.screen.duplicate_of, "proposed__p-02")
        # ... and the proposal itself is untouched, so promotion still runs it.
        self.assertIsNotNone(
            next(f for f in screened if f.finding_id == "p-02").proposed_case
        )

    def test_dedup_never_removes_the_shortcut_attackers_last_probe(self):
        """The review stage reads a shortcut PROBE — a non-informational
        finding whose `suggested_attack` is matched by its own `proposed_case`
        — as diligence evidence; the screen must not manufacture that failure
        by deduping it away (a finding carrying both fields is always
        DUPLICATE of its own proposal)."""
        task = _task()
        proposal = ProposedAttackCase(
            kind=AttackKind.KEYS_ONLY,
            expected_pass={p: (p is not PopulationName.STRESS) for p in PopulationName},
            rationale="keys only",
        )
        proposer = _finding(
            "p-00", detail=GROUNDED, attack=AttackKind.KEYS_ONLY,
            proposed=proposal, role=CouncilRole.POPULATION_ADVERSARY,
            severity=Severity.MINOR,
        )
        only_probe = _finding(
            "s-00", severity=Severity.MINOR, detail=GROUNDED,
            attack=AttackKind.KEYS_ONLY, proposed=proposal,
            role=CouncilRole.SHORTCUT_ATTACKER,
        )
        screened = council.screen_findings(task, [proposer, only_probe])
        kept = next(f for f in screened if f.finding_id == "s-00")
        self.assertIs(kept.suggested_attack, AttackKind.KEYS_ONLY)
        self.assertIsNotNone(kept.proposed_case)
        self.assertIn("shortcut_diligence_exemption", kept.screen.signals)

    def test_exemption_uses_the_review_stages_probe_predicate(self):
        """Review finding 1-3 (batch-repair round 2): the restoration once
        counted a bare `suggested_attack` as a probe, so one MAJOR bare-attack
        attacker finding beside a valid proposal left the valid one DUPLICATE
        with its attack withheld — zero probes at the review stage and the
        `critic_shortcut_diligence_incomplete` block.  The predicate is now
        the review stage's own (attack + matching proposal, above INFO): the
        valid probe is restored, the bare attack is never "restored" (it can
        never count), and a voided probe is still never resurrected."""
        task = _task()
        proposal = ProposedAttackCase(
            kind=AttackKind.KEYS_ONLY,
            expected_pass={p: (p is not PopulationName.STRESS) for p in PopulationName},
            rationale="keys only",
        )
        bare = _finding(
            "s-00", severity=Severity.MAJOR, detail=GROUNDED,
            attack=AttackKind.NO_DEDUP, role=CouncilRole.SHORTCUT_ATTACKER,
        )
        valid = _finding(
            "s-01", severity=Severity.MINOR, detail=GROUNDED,
            attack=AttackKind.KEYS_ONLY, proposed=proposal,
            role=CouncilRole.SHORTCUT_ATTACKER,
        )
        screened = council.screen_findings(task, [bare, valid])
        by_id = {f.finding_id: f for f in screened}
        self.assertIs(by_id["s-01"].suggested_attack, AttackKind.KEYS_ONLY)
        self.assertIs(by_id["s-01"].screen.status, FindingScreenStatus.REPRESENTATIVE)
        self.assertIn("shortcut_diligence_exemption", by_id["s-01"].screen.signals)
        self.assertIs(by_id["s-00"].suggested_attack, AttackKind.NO_DEDUP)
        self.assertIsNone(by_id["s-00"].screen)
        # THE predicate (the review stage's, `council.is_executable_probe`)
        # is the one the restoration applied: one definition, three sites.
        probes = [f for f in screened if council.is_executable_probe(f)]
        self.assertEqual([f.finding_id for f in probes], ["s-01"])
        self.assertTrue(council.is_executable_probe(valid))
        self.assertFalse(council.is_executable_probe(bare))
        self.assertFalse(council.is_executable_probe(valid.model_copy(update={"severity": Severity.INFO})))
        self.assertFalse(council.is_executable_probe(valid.model_copy(update={"suggested_attack": AttackKind.NO_DEDUP})))
        # Two valid probes of one mutant: one representative, one duplicate.
        twin = valid.model_copy(update={"finding_id": "s-02"})
        screened = council.screen_findings(task, [valid, twin])
        statuses = {f.finding_id: f.screen.status for f in screened}
        self.assertEqual(statuses["s-01"], FindingScreenStatus.REPRESENTATIVE)
        self.assertEqual(statuses["s-02"], FindingScreenStatus.DUPLICATE)
        # A bare attack alone: nothing to restore, nothing manufactured.
        (alone,) = council.screen_findings(task, [bare])
        self.assertIsNone(alone.screen)

    def test_the_exemption_never_resurrects_a_voided_probe(self):
        task = _task()
        junk = _finding(
            "s-00", severity=Severity.MINOR, detail="N/A",
            summary="no defect here, retracting.",
            attack=AttackKind.KEYS_ONLY, role=CouncilRole.SHORTCUT_ATTACKER,
            disposition=WITHDRAWN,
        )
        out = council.screen_findings(task, [junk])[0]
        self.assertEqual(out.screen.status, FindingScreenStatus.VOID)
        self.assertIsNone(out.suggested_attack)

    def test_dedup_target_prefix_matches_the_promoter(self):
        self.assertEqual(
            council.PROPOSAL_CASE_NAME_PREFIX, attacks.PROPOSAL_CASE_PREFIX
        )

    def test_screening_is_deterministic_and_order_independent(self):
        task = _task()
        findings = [
            _finding(f"a-{i:02d}", detail=GROUNDED, attack=AttackKind.NO_DEDUP)
            for i in range(3)
        ]
        forward = council.screen_findings(task, findings)
        backward = council.screen_findings(task, list(reversed(findings)))
        self.assertEqual(
            sorted(f.model_dump_json() for f in forward),
            sorted(f.model_dump_json() for f in backward),
        )

    def test_nothing_is_ever_removed_from_the_list(self):
        task = _task()
        findings = [
            _finding("a-00", detail="N/A"),
            _finding("a-01", detail=GROUNDED, attack=AttackKind.NO_DEDUP),
            _finding("a-02", detail=GROUNDED, attack=AttackKind.NO_DEDUP),
        ]
        screened = council.screen_findings(task, findings)
        self.assertEqual(len(screened), 3)
        self.assertEqual(
            [f.finding_id for f in screened], ["a-00", "a-01", "a-02"]
        )


class ScreenReportTest(unittest.TestCase):
    def test_report_names_every_screened_finding(self):
        task = _task()
        findings = [
            _finding("a-00", detail="N/A"),
            _finding("a-01", detail=GROUNDED, attack=AttackKind.NO_DEDUP),
            _finding("a-02", detail=GROUNDED, attack=AttackKind.NO_DEDUP),
        ]
        line = council.screen_report(council.screen_findings(task, findings))
        self.assertIn("a-00", line)
        self.assertIn("a-02->a-01", line)

    def test_report_calls_out_a_voided_fatal_by_name(self):
        """Voiding a rejection reason is loud in the ledger, never silent."""
        task = _task()
        line = council.screen_report(council.screen_findings(task, [
            _finding("feasibility_reviewer-00", severity=Severity.FATAL,
                     role=CouncilRole.FEASIBILITY_REVIEWER,
                     summary=APPLE_FATAL_00["summary"],
                     detail=APPLE_FATAL_00["detail"],
                     disposition=WITHDRAWN),
            _finding("a-00", detail="N/A"),
        ]))
        self.assertIn("SELF-WITHDRAWN FATAL", line)
        self.assertIn("feasibility_reviewer-00", line)
        # an ordinary void is NOT promoted to that headline
        self.assertNotIn("SELF-WITHDRAWN FATAL feasibility_reviewer-00, a-00",
                         line)

    def test_report_is_explicit_when_nothing_was_screened(self):
        task = _task()
        line = council.screen_report(
            council.screen_findings(task, [_finding("a-00", detail=GROUNDED)])
        )
        self.assertIn("nothing voided", line)


# Current recordings use demo_fixture.demo_task(); the old rollback table is retired.

class RecordedDemoCouncilTest(unittest.TestCase):
    """Against the six COMMITTED transcripts — the measured run, replayed."""

    @classmethod
    def setUpClass(cls):
        import json

        from elt_taskgen.review import providers

        fixtures = providers.default_fixtures_dir()
        if not providers.transcripts_present(fixtures):
            raise unittest.SkipTest("recorded council transcripts not present")
        task = demo_fixture.demo_task()
        # The key is recomputed from the current author behavior + view. An
        # intentional author-prompt change must make the old recording
        # unavailable; never re-key an answer to a prompt it did not receive.
        key = providers.transcript_key(
            "semantic_author", council._author_view(task)
        )
        author_record = fixtures / "semantic_author" / f"{key}.json"
        if not author_record.is_file():
            raise unittest.SkipTest(
                "recorded demo council replay skipped: the committed semantic "
                "author transcript predates the current author prompt/view "
                f"({author_record.name} is absent). Re-seed with 'elt-taskgen "
                "record-transcripts' (needs an API key); do NOT re-key the old "
                "answer, which would fabricate evidence."
            )
        prose = json.loads(author_record.read_text())["response"]
        cls.task = task.model_copy(update={"solver_prompt": prose})
        import tempfile
        from pathlib import Path

        cls.tmp = tempfile.mkdtemp()
        provider = providers.RoutedProvider(
            providers.load_role_routing(None),
            providers.TranscriptStore(
                Path(cls.tmp) / "transcripts", fixtures_dir=fixtures
            ),
            providers.CostMeter(budget_per_task_usd=3.0),
            replay_only=True,
        )
        try:
            cls.findings = council.run_council(cls.task, provider)
        except providers.TranscriptMissingError as exc:
            # Missing critic transcripts mean the recorded view is stale.
            # Never re-key old answers to a changed view; only a live re-record
            # can retire this visible skip.
            raise unittest.SkipTest(
                "recorded demo council replay skipped: the committed CRITIC "
                "transcripts were recorded against the pre-parity critic view "
                "and are re-keyed by it — "
                f"{str(exc).rstrip('.')}. Re-seed with 'elt-taskgen "
                "record-transcripts' (needs an API key); do NOT re-key the "
                "existing files, which would replay answers to a view these "
                "critics were never shown. This skip is VISIBLE by design."
            ) from exc

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    # NOTE ON PINNING STYLE. Finding ids embed the prompt-family fingerprint
    # (their -<hash> suffix rotates on ANY critic prompt/view edit), so ids
    # are derived from the recording rather than hardcoded — the drift alarm
    # is the measured STRUCTURE (counts, screen statuses, compile membership),
    # which only moves when the recording is deliberately re-seeded.

    @classmethod
    def _fid(cls, stem: str) -> str:
        suffix = cls.findings[0].finding_id.rsplit("-", 1)[1]
        return f"{stem}-{suffix}"

    def test_all_seven_findings_are_still_present(self):
        """The screen withholds and annotates; it never deletes a finding.

        7 measured on the attempt-12 recording (the first recorded against
        the ENRICHED critic view, which now reproduces documentation.md's
        typed source-table block alongside schemas/*.csv): 0 ambiguity + 1
        population + 6 shortcut. Composition on attempt 11, on the poorer
        view, was 0 + 2 + 5 — the total is unchanged at 7, one population
        finding moved to the shortcut seat. The ambiguity critic still files
        NOTHING on this clean specimen even now that it can see declared
        column types and key roles, which is the enriched view's main
        false-alarm risk and it did not fire.

        History of this number: 10 under the pre-discipline prompts (2
        ambiguity + 3 population + 5 shortcut), 6 on the attempt-9 recording
        (0 + 1 + 5), 7 on attempt 10 (0 + 2 + 5), 7 on attempt 11 (0 + 2 +
        5) and 7 again now (0 + 1 + 6). The single population finding is the
        orphan-customer_id coverage hole (major, no attack kind — on attempt
        11 this same concern proposed a CUSTOM case, and its companion
        header-duplicate observation is not filed at all on this view).

        This docstring makes NO claim about metrology discipline: the
        admission evidence is being re-earned on the enriched view at the
        same time as this recording, and the per-role precision / nitpick
        numbers belong to that report, not to this fixture."""
        self.assertEqual(len(self.findings), 7)

    def test_voided_findings_in_this_recorded_run(self):
        """This recording carries NO voided findings — zero measured.

        The previous recording had exactly one (the shortcut attacker's fifth
        slot came back as placeholder junk and the screen voided it). Under
        the disciplined prompts that slot now returns a real, well-formed
        observation (severity 'info', no suggested attack), so nothing in
        this recording trips the junk-voiding path. That path stays pinned
        synthetically above; this specimen pins that the screen does not void
        findings which are merely low-severity or attack-less."""
        voided = [
            f.finding_id
            for f in self.findings
            if f.screen is not None
            and f.screen.status is FindingScreenStatus.VOID
        ]
        self.assertEqual(voided, [])

    def test_duplicates_collapse_onto_their_representative(self):
        """Zero duplicates measured in this recording: the v3 disciplined
        critics each report distinct concerns, so nothing collapses. The
        duplicate-collapse behaviour itself stays pinned synthetically in
        MutantDedupTest above; this specimen pins that the screen does not
        invent duplicates where none exist."""
        dupes = {
            f.finding_id: f.screen.duplicate_of
            for f in self.findings
            if f.screen is not None
            and f.screen.status is FindingScreenStatus.DUPLICATE
        }
        self.assertEqual(dupes, {})

    def test_compiled_case_count_matches_the_screen(self):
        """17 executions: 12 standing (4 transform + 8 extract-load) + 5
        finding-compiled. The 12 standing cases are unchanged across every
        recording. The finding-compiled half was 6 under the pre-discipline
        prompts, fell to 4 on the attempt-9 recording (the disciplined
        ambiguity critic files NOTHING on this clean specimen, so its two
        attacks are gone), was 5 on attempt 10, 5 on attempt 11 and is 5
        again on attempt 12 — but the MEMBERSHIP changed again, which is
        what this test re-pins.

        On attempt 12 (the first recording against the ENRICHED critic view)
        the shortcut attacker files SIX findings, not five. The first five
        (-00..-04) carry an attack kind — CONSTANTS, KEYS_ONLY, NO_OP,
        SKIP_EXTRACTION, CONSTANTS — so exactly those five compile, the same
        count as attempt 11 from a larger pool. The sixth (-05) is severity
        INFO with no attack kind and does not compile.

        The lone population finding does not compile either: -00 is the
        orphan-customer_id coverage hole and names no attack kind at all. On
        attempt 11 the same concern arrived as -00 suggesting CUSTOM (dropped
        for a different, separately-pinned reason — a CUSTOM proposal with no
        executable payload) alongside a second, header-duplicate finding -01
        that carried no kind; on the enriched view only the one finding is
        filed and it is the no-kind form. The finding text is still carried
        in the review payload — nothing is lost, only nothing is faked."""
        compiled = attacks.compile_attacks(self.task, self.findings)
        self.assertEqual(len(compiled), 17)
        names = {c.name for c in compiled}
        # SIX shortcut findings on attempt 12 (five on attempt 11); the first
        # five carry a kind — the shortcut-probes gate needs them — and the
        # sixth is an attack-less INFO observation that does not compile.
        self.assertEqual(
            len([f for f in self.findings if "shortcut_attacker" in f.finding_id]),
            6,
        )
        for i in range(5):
            self.assertIn(f"finding__{self._fid(f'shortcut_attacker-{i:02d}')}", names)
        self.assertNotIn(f"finding__{self._fid('shortcut_attacker-05')}", names)
        # the ambiguity critic filed nothing on this clean specimen, so no
        # ambiguity finding exists to compile; the one adversary finding
        # names no attack kind and so yields no executable case.
        self.assertEqual(
            [f.finding_id for f in self.findings if "ambiguity_critic" in f.finding_id],
            [],
        )
        self.assertEqual(
            len([f for f in self.findings if "population_adversary" in f.finding_id]),
            1,
        )
        self.assertNotIn(f"finding__{self._fid('ambiguity_critic-00')}", names)
        self.assertNotIn(f"finding__{self._fid('population_adversary-00')}", names)

    def test_finding_with_no_suggested_attack_is_not_compiled(self):
        """population_adversary-00 reports a concern but suggests no
        executable attack kind; it survives as a finding and is deliberately
        absent from the compiled battery.

        Re-pinned from -01 back to -00 on the attempt-12 recording (the first
        against the enriched critic view). On attempt 11 the adversary filed
        two findings and -00 was the one carrying CUSTOM, so -01 was the
        plain no-kind specimen; on the enriched view it files one finding,
        -00, and that finding carries no kind at all — so -00 is now the
        specimen that exercises the plain no-kind path. -01 no longer
        exists, which is why the old pin raised StopIteration rather than
        passing vacuously."""
        finding = next(
            f for f in self.findings
            if f.finding_id == self._fid("population_adversary-00")
        )
        self.assertIsNone(finding.suggested_attack)
        self.assertIsNone(finding.proposed_case)
        compiled = {c.name for c in attacks.compile_attacks(self.task, self.findings)}
        self.assertNotIn(f"finding__{self._fid('population_adversary-00')}", compiled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""R02: a critic finding's withdrawal state is structured data, never prose.

Two regex systems used to read withdrawal out of free-form explanation text:
`cli._detail_withdraws_the_finding` (the critic-to-attack handoff) and the
terminal-clause grammar behind `self_retracted` in `council.screen_findings`.
Both misread active claims — "I withdraw nothing.", and "Both readings produce
the same keys. Their totals differ." — voiding a MAJOR finding that would
otherwise block as an unresolved claim. The invariant these tests hold is that
prose alone cannot change withdrawal state.
"""

from __future__ import annotations

import json
import unittest

from elt_taskgen import cli, demo_fixture
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
)
from elt_taskgen.review import council

#: Explanations that must never withdraw an active claim.
ACTIVE_EXPLANATIONS = (
    "I withdraw nothing.",
    "Both readings produce the same keys. Their totals differ.",
    "Both readings produce the same keys, but their totals differ.",
    "The totals differ between the readings. I withdraw this finding.",
    'The spec\'s note says "I withdraw this finding" but the totals still differ.',
    "This is not a false alarm: the totals differ between the readings.",
)


#: Names this task's mart and column, so the screen's weak-evidence rule
#: (`ungrounded_detail`) has nothing to object to and withdrawal is the only
#: thing that could void the finding. The explanation under test stays the
#: CLOSING text, which is what both prose readers inspected.
GROUNDING = "customer_summary.total_spend can be computed two ways from the prose."


def _major(detail: str, **fields) -> Finding:
    return Finding(
        finding_id="ambiguity_critic-00-test",
        role=CouncilRole.AMBIGUITY_CRITIC,
        severity=Severity.MAJOR,
        summary="the two readings disagree on customer_summary.total_spend",
        detail=f"{GROUNDING} {detail}",
        **fields,
    )


class ProseNeverWithdrawsTests(unittest.TestCase):
    """Valid under the old and the new protocol alike: a finding that declares
    no withdrawal stays active, whatever its explanation says."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.task = demo_fixture.demo_task()

    def test_the_attack_handoff_keeps_the_claim_active_and_blocking(self) -> None:
        for detail in ACTIVE_EXPLANATIONS:
            with self.subTest(detail=detail):
                (screened,) = cli._validated_executable_findings(self.task, [_major(detail)])
                self.assertIsNone(screened.screen, "the finding was voided by its prose")
                blocking, problems = cli._blocking_proposal_failures([screened], [])
                self.assertEqual(blocking, [screened])
                self.assertTrue(problems)

    def test_the_council_screen_reads_no_retraction_from_prose(self) -> None:
        for detail in ACTIVE_EXPLANATIONS:
            with self.subTest(detail=detail):
                (screened,) = council.screen_findings(self.task, [_major(detail)])
                signals = screened.screen.signals if screened.screen else ()
                self.assertNotIn("self_retracted", signals)
                self.assertIsNot(
                    screened.screen.status if screened.screen else None,
                    FindingScreenStatus.VOID,
                )


def _wire(detail: str, disposition, *, role=CouncilRole.AMBIGUITY_CRITIC) -> str:
    """One provider response in the live critic protocol."""
    item = {
        "severity": "major",
        "summary": "the two readings disagree on customer_summary.total_spend",
        "detail": f"{GROUNDING} {detail}",
        "route_hint": "specification",
        "suggested_attack": None,
        "proposed_case": None,
    }
    if disposition is not _MISSING:
        item["disposition"] = disposition
    return json.dumps({"findings": [item]})


_MISSING = object()


class StructuredDispositionTests(unittest.TestCase):
    """The new live protocol, from the real parser through both consumers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.task = demo_fixture.demo_task()

    def _parse(self, raw: str, role=CouncilRole.AMBIGUITY_CRITIC) -> list[Finding]:
        return council._parse_findings(role, raw, "test")

    def test_an_active_disposition_is_never_overridden_by_prose(self) -> None:
        for detail in ACTIVE_EXPLANATIONS:
            with self.subTest(detail=detail):
                (finding,) = self._parse(_wire(detail, "active"))
                self.assertIs(finding.disposition, FindingDisposition.ACTIVE)
                self.assertFalse(finding.withdrawn)
                (handed,) = cli._validated_executable_findings(self.task, [finding])
                self.assertIsNone(handed.screen)
                blocking, _ = cli._blocking_proposal_failures([handed], [])
                self.assertEqual(blocking, [handed])
                (screened,) = council.screen_findings(self.task, [finding])
                self.assertNotIn(
                    "self_retracted", screened.screen.signals if screened.screen else ()
                )

    def test_an_explicit_withdrawal_applies_to_its_own_finding_and_keeps_the_evidence(self) -> None:
        (finding,) = self._parse(_wire("The totals differ between the readings.", "withdrawn"))
        self.assertTrue(finding.withdrawn)
        self.assertIs(finding.provenance, FindingProvenance.PROVIDER)

        (handed,) = cli._validated_executable_findings(self.task, [finding])
        self.assertIs(handed.screen.status, FindingScreenStatus.VOID)
        self.assertIn(cli.WITHDRAWN_BY_DISPOSITION_SIGNAL, handed.screen.signals)
        # The original claim, its evidence and its provenance survive the void.
        self.assertEqual(handed.summary, finding.summary)
        self.assertEqual(handed.detail, finding.detail)
        self.assertIs(handed.screen.claimed_severity, Severity.MAJOR)
        self.assertIs(handed.disposition, FindingDisposition.WITHDRAWN)
        blocking, _ = cli._blocking_proposal_failures([handed], [])
        self.assertEqual(blocking, [])

        (screened,) = council.screen_findings(self.task, [finding])
        self.assertIs(screened.screen.status, FindingScreenStatus.VOID)
        self.assertIn("self_retracted", screened.screen.signals)
        self.assertIn("disposition", screened.screen.evidence)

    def test_a_withdrawal_cancels_no_other_finding(self) -> None:
        withdrawn = _major("The totals differ.", disposition=FindingDisposition.WITHDRAWN)
        active = _major("The totals differ.", disposition=FindingDisposition.ACTIVE).model_copy(
            update={"finding_id": "ambiguity_critic-01-test"}
        )
        handed = cli._validated_executable_findings(self.task, [withdrawn, active])
        blocking, _ = cli._blocking_proposal_failures(handed, [])
        self.assertEqual([f.finding_id for f in blocking], [active.finding_id])

    def test_a_withdrawal_never_withholds_an_executable_proposal_at_the_handoff(self) -> None:
        proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN,
            params={},
            expected_pass={p: False for p in PopulationName},
            expected_pass_by_stage={
                "extract_load": {p: True for p in PopulationName},
                "transform": {p: False for p in PopulationName},
            },
            rationale="an INNER join drops customers without completed orders",
        )
        finding = _major(
            "The totals differ.",
            disposition=FindingDisposition.WITHDRAWN,
            proposed_case=proposal,
        )
        (handed,) = cli._validated_executable_findings(self.task, [finding])
        self.assertNotIn(
            cli.WITHDRAWN_BY_DISPOSITION_SIGNAL,
            handed.screen.signals if handed.screen else (),
        )
        self.assertIsNotNone(handed.proposed_case)

    def test_missing_or_invalid_disposition_is_a_protocol_failure(self) -> None:
        for label, value in (("missing", _MISSING), ("unknown", "maybe"), ("null", None)):
            with self.subTest(label):
                with self.assertRaises(council.ProviderProtocolError):
                    self._parse(_wire("The totals differ.", value))

    def test_a_historical_record_is_readable_active_and_byte_stable(self) -> None:
        legacy = {
            "finding_id": "ambiguity_critic-00-old",
            "role": "ambiguity_critic",
            "severity": "major",
            "provenance": "provider",
            "summary": "the two readings disagree on customer_summary.total_spend",
            "detail": f"{GROUNDING} I withdraw nothing.",
            "route_hint": None,
            "suggested_attack": None,
            "proposed_case": None,
            "screen": None,
        }
        finding = Finding.model_validate(legacy)
        self.assertIsNone(finding.disposition)
        self.assertFalse(finding.withdrawn)
        # Never re-inferred from prose, and its serialized form is unchanged.
        self.assertEqual(json.loads(finding.model_dump_json()), legacy)
        (handed,) = cli._validated_executable_findings(self.task, [finding])
        self.assertIsNone(handed.screen)

    def test_a_code_finding_cannot_be_withdrawn(self) -> None:
        with self.assertRaises(ValueError):
            Finding(
                finding_id="code-00-leak",
                role=CouncilRole.AMBIGUITY_CRITIC,
                severity=Severity.FATAL,
                summary="private reference SQL leaked into the prose",
                provenance=FindingProvenance.CODE,
                disposition=FindingDisposition.WITHDRAWN,
            )

    def test_independent_resolution_checks_still_resolve_an_active_claim(self) -> None:
        """Removing the prose authority must not remove the other screens."""
        # A NO_DEDUP proposal against a task that declares no dedupe rule.
        no_dedup = ProposedAttackCase(
            kind=AttackKind.NO_DEDUP,
            params={},
            expected_pass={p: False for p in PopulationName},
            expected_pass_by_stage={
                "extract_load": {p: True for p in PopulationName},
                "transform": {p: False for p in PopulationName},
            },
            rationale="duplicate rows would double customer_summary.total_spend",
        )
        declares_dedupe = any(
            getattr(op, "kind", None) is not None and op.kind.value == "dedupe"
            for mart in self.task.marts
            for op in mart.plan.ops
        )
        if not declares_dedupe:
            finding = _major(
                "The totals differ.",
                disposition=FindingDisposition.ACTIVE,
                proposed_case=no_dedup,
                suggested_attack=AttackKind.NO_DEDUP,
            )
            (handed,) = cli._validated_executable_findings(self.task, [finding])
            self.assertIn(
                cli.MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL, handed.screen.signals
            )
        # An active claim that names nothing in the task is still weak evidence.
        ungrounded = Finding(
            finding_id="ambiguity_critic-02-test",
            role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.MAJOR,
            summary="something seems off",
            detail="Something seems off about it.",
            disposition=FindingDisposition.ACTIVE,
        )
        (screened,) = council.screen_findings(self.task, [ungrounded])
        self.assertIn("ungrounded_detail", screened.screen.signals)


if __name__ == "__main__":
    unittest.main()

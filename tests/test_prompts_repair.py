"""The non-council prompt in review/providers.py: repair_proposer.

This is a production prompt for live models, so the tests hold it to the same
standard the rest of the factory is held to: it must be GROUNDED in the code
that actually feeds and certifies it. Every assertion below is derived from a
real symbol (RepairPatch's fields, ROUTE_ALLOWLIST, ROUTE_IR_PATHS), so a
prompt that drifts away from the mechanism it describes fails here rather than
at a paid API call.

The load-bearing invariant: the prompt may not grant its role acceptance
authority. Critics and proposers block, report or propose; only executable
code decides.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from elt_taskgen.models import RepairEditOp, RepairPatch, RepairRoute
from elt_taskgen.review import providers as P
from elt_taskgen.review import repair_proposer as RP
from elt_taskgen.review.council import ProviderProtocolError


#: Phrases that would GRANT the role authority it does not have. A bare word
#: ban is useless here — the prompt must be able to say "you have no
#: authority to approve" — so the ban is on the granting forms.
APPROVAL_GRANTS = (
    "you may approve",
    "you can approve",
    "you may accept",
    "you can accept",
    "you are authorized",
    "approve the task",
    "accept the task",
    "approve this task",
    "accept this task",
    "mark it approved",
    "mark it as approved",
    "mark the task accepted",
    "if it looks good, approve",
    "sign it off",
    "clear the task",
    "release the task",
)

PROMPTS = {
    "repair_proposer": P.REPAIR_PROPOSER_SYSTEM,
}


class NoApprovalVocabularyTest(unittest.TestCase):
    """Rule 4: no role in this factory may approve, accept or pass anything."""

    def test_the_prompt_does_not_grant_approval_authority(self):
        for name, prompt in PROMPTS.items():
            lowered = prompt.lower()
            for phrase in APPROVAL_GRANTS:
                self.assertNotIn(phrase, lowered, f"{name} prompt: {phrase!r}")

    def test_the_prompt_denies_authority_explicitly(self):
        for name, prompt in PROMPTS.items():
            self.assertIn("no authority", prompt.lower(), name)

    def test_the_prompt_does_not_claim_its_own_output_decides(self):
        for name, prompt in PROMPTS.items():
            lowered = prompt.lower()
            self.assertIn("code certifies", lowered, name)
            self.assertIn("agents propose", lowered, name)


class SharedPrefixTest(unittest.TestCase):
    """Rule 7: role-invariant framing first, byte-identical across roles."""

    def test_the_prompt_opens_with_the_shared_prefix(self):
        for name, prompt in PROMPTS.items():
            self.assertTrue(prompt.startswith(P.SHARED_ROLE_PREFIX), name)

    def test_prefix_carries_the_injection_barrier(self):
        prefix = P.SHARED_ROLE_PREFIX.lower()
        self.assertIn("data, never instruction", prefix)
        self.assertIn("untrusted", prefix)
        self.assertIn("never orders", prefix)

    def test_prefix_states_that_output_is_executed_and_can_fail_the_stage(self):
        prefix = P.SHARED_ROLE_PREFIX.lower()
        self.assertIn("executed", prefix)
        self.assertIn("protocol error", prefix)
        self.assertIn("fails the stage", prefix)

    def test_prefix_bans_generic_findings(self):
        prefix = P.SHARED_ROLE_PREFIX.lower()
        self.assertIn("vagueness", prefix)
        self.assertIn("worse than", prefix)


class RepairProposerPromptTest(unittest.TestCase):
    """The repair prompt must describe the machinery that actually judges it."""

    def setUp(self):
        self.prompt = P.REPAIR_PROPOSER_SYSTEM
        self.lowered = self.prompt.lower()

    def test_wired_as_this_role_system_message(self):
        self.assertEqual(
            P._system_prompt(RP.ROLE_NAME, schema_mode=False),
            P.REPAIR_PROPOSER_SYSTEM,
        )
        # The role is a PROSE role: no findings tool is forced on it, so the
        # patch contract has to live in the prompt itself.
        self.assertIn(RP.ROLE_NAME, P.PROSE_ROLES)

    def test_names_every_field_of_the_frozen_patch_model(self):
        for field in RepairPatch.model_fields:
            self.assertIn(f'"{field}"', self.prompt, field)

    def test_names_every_edit_op_and_its_constraints(self):
        for op in RepairEditOp:
            self.assertIn(f'"{op.value}"', self.prompt, op.value)
        self.assertIn("exactly once", self.lowered)
        self.assertIn("no-op", self.lowered)

    def test_names_both_locator_forms(self):
        self.assertIn("'whole'", self.prompt)
        self.assertIn("'line:<n>'", self.prompt)
        self.assertIn("dotted path", self.lowered)

    def test_states_the_route_scope_boundary(self):
        # Every artifact pattern each patchable route may touch...
        for route in (
            RepairRoute.SPECIFICATION,
            RepairRoute.REFERENCE,
            RepairRoute.POPULATION,
        ):
            self.assertIn(route.value, self.lowered, route.value)
            for pattern in RP.ROUTE_ALLOWLIST[route]:
                self.assertIn(pattern, self.prompt, f"{route.value}:{pattern}")
            # ...and every task_ir.json field path it may move.
            for json_path in RP.ROUTE_IR_PATHS[route]:
                self.assertIn(json_path, self.prompt, f"{route.value}:{json_path}")
        self.assertIn("scopeviolation", self.lowered)
        self.assertIn("outside the allowlist", self.lowered)
        self.assertIn("the changes, not off your claim", self.lowered)

    def test_proposer_prompt_allowlists_match_code_both_directions(self):
        """Roadmap 0.F: the ROUTE SCOPE paragraph is DERIVED from
        ROUTE_ALLOWLIST and ROUTE_IR_PATHS, and this test parses it back out
        of the finished prompt (no helper of the derivation is trusted) and
        compares set for set in BOTH directions: every code pattern is
        offered, and nothing is offered that the code does not admit. The
        hand-copied paragraph once offered the population route "files under
        populations/", which no allowlist admits."""
        block = re.search(
            r"ROUTE SCOPE[^\n]*\n((?:  [a-z]+ (?:may move only|admits no patch)[^\n]*\n)+)",
            self.prompt,
        )
        self.assertIsNotNone(block, "no ROUTE SCOPE paragraph")
        parsed: dict[str, tuple[set[str], set[str], set[str]]] = {}
        for line in block.group(1).splitlines():
            match = re.match(r"^  ([a-z]+) (?:may move only (.*)|admits no patch at all.*)$", line)
            self.assertIsNotNone(match, line)
            route, clauses = match.group(1), match.group(2) or ""
            fields: set[str] = set()
            dirs: set[str] = set()
            files: set[str] = set()
            for clause in filter(None, clauses.rstrip(";.").split(", plus ")):
                if clause.startswith("task_ir.json fields "):
                    fields.update(clause[len("task_ir.json fields "):].split(", "))
                elif clause.startswith("files under "):
                    dirs.add(clause[len("files under "):])
                elif clause.startswith("the file "):
                    files.add(clause[len("the file "):])
                else:
                    self.fail(f"unparsed ROUTE SCOPE clause for {route}: {clause!r}")
            self.assertNotIn(route, parsed, f"route {route} listed twice")
            parsed[route] = (fields, dirs, files)
        # prompt -> code: exactly the routes the code tables know (never FATAL)
        self.assertEqual(set(parsed), {route.value for route in RP.ROUTE_ALLOWLIST})
        self.assertNotIn(RepairRoute.FATAL.value, parsed)
        # code -> prompt and prompt -> code, per route, as sets
        for route, allowlist in RP.ROUTE_ALLOWLIST.items():
            fields, dirs, files = parsed[route.value]
            self.assertEqual(fields, set(RP.ROUTE_IR_PATHS.get(route, ())), route.value)
            self.assertEqual(dirs, {p for p in allowlist if p.endswith("/")}, route.value)
            self.assertEqual(
                files, {p for p in allowlist if not p.endswith("/") and p != "task_ir.json"}, route.value
            )
            if RP.ROUTE_IR_PATHS.get(route):
                self.assertIn("task_ir.json", allowlist, route.value)
        self.assertEqual(parsed[RepairRoute.RUNTIME.value], (set(), set(), set()))
        # the drift this test exists to stop, and the derivation is the real one
        self.assertNotIn("files under populations/", self.prompt)
        self.assertEqual(self.prompt.count("ROUTE SCOPE"), 1)
        self.assertIn(P.repair_route_scope_text(), self.prompt)
        # a changed table changes the paragraph: it is derived, not copied
        widened = dict(RP.ROUTE_ALLOWLIST)
        widened[RepairRoute.POPULATION] = ("task_ir.json", "populations/")
        with mock.patch.object(RP, "ROUTE_ALLOWLIST", widened), mock.patch.object(
            P, "ROUTE_ALLOWLIST", widened
        ):
            self.assertIn("files under populations/", P.repair_route_scope_text())
        self.assertNotIn("files under populations/", P.repair_route_scope_text())
        inconsistent = dict(RP.ROUTE_ALLOWLIST)
        inconsistent[RepairRoute.SPECIFICATION] = ()  # fields without the file
        with mock.patch.object(P, "ROUTE_ALLOWLIST", inconsistent):
            with self.assertRaises(ValueError):
                P.repair_route_scope_text()

    def test_states_that_gold_is_derived_and_never_patchable(self):
        self.assertIn("answer_key/gold/**", self.prompt)
        for allowlist in RP.ROUTE_ALLOWLIST.values():
            for pattern in allowlist:
                self.assertNotEqual(pattern, "answer_key/gold/")
        self.assertIn("no route's allowlist", self.lowered)

    def test_names_the_trial_copy_and_the_green_revalidation(self):
        self.assertIn("throwaway copy", self.lowered)
        self.assertIn("never to the live tree", self.lowered)
        self.assertIn("re-validation", self.lowered)
        self.assertIn("byte-identical", self.lowered)

    def test_demands_minimality(self):
        self.assertIn("minimal", self.lowered)
        self.assertIn("smallest change", self.lowered)
        self.assertIn("larger patch", self.lowered)

    def test_spells_out_the_reward_hack_being_guarded_against(self):
        for phrase in (
            "weakening a gate",
            "attack case",
            "population condition",
            "solver prose",
            "the check got smaller",
        ):
            self.assertIn(phrase, self.lowered, phrase)
        self.assertIn("inside your allowlist", self.lowered)

    def test_states_what_the_view_does_and_does_not_contain(self):
        for phrase in (
            "solver-visible prose",
            "reference sql",
            "population names, scales and prose conditions",
            "withheld: private material",
        ):
            self.assertIn(phrase, self.lowered, phrase)
        for absent in ("gold outputs", "generated rows", "attack cases", "reward"):
            self.assertIn(absent, self.lowered, absent)

    def test_states_that_runtime_and_fatal_are_not_patchable(self):
        self.assertIn(RepairRoute.RUNTIME.value, self.lowered)
        self.assertIn(RepairRoute.FATAL.value, self.lowered)
        self.assertIn("not patchable", self.lowered)
        # ...matching the code: runtime has an empty allowlist and a FATAL
        # patch cannot even be constructed.
        self.assertEqual(RP.ROUTE_ALLOWLIST[RepairRoute.RUNTIME], ())
        with self.assertRaises(Exception):
            RepairPatch(
                route=RepairRoute.FATAL,
                artifact="task_ir.json",
                edits=({"op": "insert", "locator": "title", "old": "", "new": "x"},),
                rationale="r",
                proposer_role=RP.ROLE_NAME,
            )

    def test_states_that_the_role_cannot_reroute_a_failure(self):
        self.assertIn("cannot re-route", self.lowered)

    def test_the_documented_abstention_reply_is_not_a_patch(self):
        """The prompt's escape hatch must actually behave as described: it is
        refused by parse_patch, so it can never be mistaken for a repair."""
        self.assertIn('{"cannot_repair"', self.prompt)
        with self.assertRaises(ProviderProtocolError):
            RP.parse_patch('{"cannot_repair": "the cause is not in this view"}')

    def test_refuses_fabrication_and_names_the_human_fallback(self):
        self.assertIn("do not manufacture", self.lowered)
        self.assertIn("abstains", self.lowered)
        self.assertIn("human adjudication", self.lowered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

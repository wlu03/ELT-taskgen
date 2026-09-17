"""The $0 column-attribution precheck (verification/attribution.py).

Measured on dbt_apple_search_ads: the authored prose said `organization_id`
came from the matching `campaign_history` record while the frozen reference
reads `ad_history."org_id"`. The independent builder FOLLOWED THE PROSE and
disagreed with gold on 243 of 247 rows; dual-build-agreement refused at
`gates`, one four-seat council run after the sentence was written.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.verification import attribution


SOURCES = ("ad_group_history", "campaign_history", "ad_history", "users", "orders")


class ProseClaimParsing(unittest.TestCase):
    """Attribution is found by naming a DECLARED SOURCE TABLE, never by
    matching a sentence template — an author writes English, not a format."""

    def test_claims_are_read_per_mart(self) -> None:
        prose = """
## Mart: alpha
- `organization_id` (integer): taken from the matching `ad_group_history` record.
## Mart: beta
- `organization_id` (integer): taken from the matching `campaign_history` record.
"""
        claims = attribution.prose_claims(prose, SOURCES)
        self.assertEqual(claims["alpha"]["organization_id"], "ad_group_history")
        self.assertEqual(claims["beta"]["organization_id"], "campaign_history")

    def test_any_phrasing_that_names_the_table_is_understood(self) -> None:
        """The first version keyed on one template and so saw claims in 1 of
        15 authored tasks — blind even to the phrasing review/prompts.py holds
        up as standard. These are all real phrasings from released tasks."""
        for line in (
            "- `role` (text): the `role` of that `users` row, copied unchanged.",
            "- `role` (text): taken from the matching `users` record.",
            "- `role` (text): carried unchanged from `users`.",
            "- `role` (text): whatever the joined `users` row holds.",
        ):
            claims = attribution.prose_claims(f"## Mart: m\n{line}\n", SOURCES)
            self.assertEqual(
                claims.get("m", {}).get("role"), "users", f"missed: {line}"
            )

    def test_naming_two_tables_is_not_a_claim(self) -> None:
        """A sentence mentioning several tables describes a relationship;
        guessing which one it means is how a checker invents defects."""
        prose = ("## Mart: m\n- `x` (integer): the `users` row behind each "
                 "`orders` row.\n")
        self.assertEqual(attribution.prose_claims(prose, SOURCES), {})

    def test_a_word_that_is_not_a_source_table_is_not_a_claim(self) -> None:
        prose = "## Mart: m\n- `x` (integer): the `id` of the winner.\n"
        self.assertEqual(attribution.prose_claims(prose, SOURCES), {})

    def test_lines_before_any_mart_heading_are_dropped(self) -> None:
        prose = "- `x` (integer): taken from the matching `users` record.\n"
        self.assertEqual(attribution.prose_claims(prose, SOURCES), {})

    def test_a_line_that_makes_no_attribution_is_out_of_scope(self) -> None:
        prose = "## Mart: alpha\n- `x` (integer): the number of things.\n"
        self.assertEqual(attribution.prose_claims(prose, SOURCES), {})


class AttributionCheck(unittest.TestCase):
    """The check never certifies: silence on either side is not a defect."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.task = demo_fixture.demo_task()

    def _write_reference(self, mart: str, sql: str) -> None:
        d = self.ws / "tasks" / self.task.task_id / "answer_key" / "reference"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{mart}.sql").write_text(sql, encoding="utf-8")

    def test_no_reference_on_disk_is_not_a_refusal(self) -> None:
        self.assertIsNone(attribution.check_attribution(self.task, self.ws))

    def test_agreeing_prose_passes(self) -> None:
        mart = self.task.marts[0].name
        self._write_reference(mart, 'SELECT customers."customer_name" AS "who"')
        task = self.task.model_copy(
            update={"solver_prompt": f"## Mart: {mart}\n"
                    "- `who` (text): taken from the matching `customers` record.\n"}
        )
        self.assertIsNone(attribution.check_attribution(task, self.ws))

    def test_contradicting_prose_is_refused_and_names_both_tables(self) -> None:
        mart = self.task.marts[0].name
        self._write_reference(mart, 'SELECT customers."customer_name" AS "who"')
        task = self.task.model_copy(
            update={"solver_prompt": f"## Mart: {mart}\n"
                    "- `who` (text): taken from the matching `orders` record.\n"}
        )
        detail = attribution.check_attribution(task, self.ws)
        self.assertIsNotNone(detail)
        self.assertIn("column-attribution", detail)
        self.assertIn("orders", detail)
        self.assertIn("customers", detail)
        self.assertIn(f"{mart}.who", detail)

    def test_a_step_alias_is_not_an_origin(self) -> None:
        """The compiled reference re-projects a column through CTEs, so a
        qualifier like `mart_top` is a step alias, not a source. Treating the
        first qualifier of any kind as the origin refused SIX released tasks
        whose prose was correct."""
        mart = self.task.marts[0].name
        self._write_reference(
            mart,
            'WITH step_6 AS (SELECT "f_id" AS "top_row_id" FROM step_4) '
            'SELECT mart_top."top_row_id" AS "top_row_id" FROM step_6 AS mart_top',
        )
        task = self.task.model_copy(
            update={"solver_prompt": f"## Mart: {mart}\n"
                    "- `top_row_id` (bigint): the id of that `orders` row.\n"}
        )
        self.assertIsNone(attribution.check_attribution(task, self.ws))

    def test_a_column_the_reference_never_binds_says_nothing(self) -> None:
        mart = self.task.marts[0].name
        self._write_reference(mart, 'SELECT customers."customer_name" AS "who"')
        task = self.task.model_copy(
            update={"solver_prompt": f"## Mart: {mart}\n"
                    "- `unbound` (text): taken from the matching `orders` record.\n"}
        )
        self.assertIsNone(attribution.check_attribution(task, self.ws))


if __name__ == "__main__":
    unittest.main()

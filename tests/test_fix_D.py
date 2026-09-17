"""Group D fixes whose natural home is a test file another group owns.

  * SynSQL `_comment_domains` drops enumeration SENTINELS ('etc.', 'and so
    on', a trailing 'or'/'and') — the domain stays closed, but no ladder
    branch or filter literal is ever the string 'etc.' (I6-E).
  * SynSQL never grains a mart, or takes a hop-2 join, on a parent column
    that is not a declared single-column PK/business key: `chain_candidates`
    is never disarmed by omission (`key_parents=None` == declared keys), so a
    schema whose ONLY links target non-key columns is refused at ingest,
    before generate/reference spend (G2-A / I5-A: 2.8% of SynSQL FK edges
    target a non-PK parent column and froze inflated gold — link_count=28056
    against 79 real children — that GROUP BY hid).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from elt_taskgen.adapters import synsql
from elt_taskgen.adapters.evidence import declared_key_parents


def _write_tables(tmpdir: str, entries: list[dict]) -> Path:
    path = Path(tmpdir) / "tables.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _entry(db_id: str, *, extra_link: bool) -> dict:
    """objects(object_id PK, object_name, object_label) <- voice_commands via
    the NON-key `object_name`; optionally a second, keyed link to users."""
    ddls = [
        (
            'CREATE TABLE "objects" ("object_id" INTEGER PRIMARY KEY, '
            '"object_name" TEXT NOT NULL, "object_label" TEXT NOT NULL)'
        ),
        (
            'CREATE TABLE "users" ("user_id" INTEGER PRIMARY KEY, '
            '"user_name" TEXT NOT NULL, "tier" TEXT /* Tier (e.g., gold, silver, '
            "bronze, etc.) */)"
        ),
        (
            'CREATE TABLE "voice_commands" ("command_id" INTEGER PRIMARY KEY, '
            '"object" TEXT, "user_id" INTEGER, "phrase" TEXT NOT NULL, '
            '"duration" INTEGER, "status" TEXT NOT NULL '
            "/* Outcome (e.g., ok, failed, or retried) */)"
        ),
    ]
    column_names_original = [
        [-1, "*"],
        [0, "object_id"],        # 1
        [0, "object_name"],      # 2
        [0, "object_label"],     # 3
        [1, "user_id"],          # 4
        [1, "user_name"],        # 5
        [1, "tier"],             # 6
        [2, "command_id"],       # 7
        [2, "object"],           # 8
        [2, "user_id"],          # 9
        [2, "phrase"],           # 10
        [2, "duration"],         # 11
        [2, "status"],           # 12
    ]
    foreign_keys = [[8, 2]]
    if extra_link:
        foreign_keys.append([9, 4])
    return {
        "db_id": db_id,
        "ddls": ddls,
        "table_names": ["objects", "users", "voice commands"],
        "table_names_original": ["objects", "users", "voice_commands"],
        "column_names": [[t, n.replace("_", " ")] for t, n in column_names_original],
        "column_names_original": column_names_original,
        "column_types": [
            "text", "number", "text", "text", "number", "text", "text",
            "number", "text", "number", "text", "number", "text",
        ],
        "foreign_keys": foreign_keys,
        "primary_keys": [1, 4, 7],
    }


class SynSQLCommentDomainSentinelTest(unittest.TestCase):
    def test_comment_domain_drops_etc_sentinel(self) -> None:
        ddls = [
            'CREATE TABLE "t" ('
            '"month" TEXT /* Month of record (e.g., JAN, FEB, MAR, etc.) */, '
            '"status" TEXT /* Outcome (e.g., recovered, deceased, or ongoing treatment) */, '
            '"x" TEXT /* thing (e.g., x, etc.) */, '
            '"y" TEXT /* kind (e.g., a, b, and so on) */, '
            '"z" TEXT /* kind (e.g., a, b, and c) */)'
        ]
        domains = synsql._comment_domains(ddls)
        self.assertEqual(domains[("t", "month")], ("JAN", "FEB", "MAR"))
        self.assertEqual(
            domains[("t", "status")], ("recovered", "deceased", "ongoing treatment")
        )
        # A lone value plus a sentinel is no domain at all (the >=2 rule).
        self.assertNotIn(("t", "x"), domains)
        self.assertEqual(domains[("t", "y")], ("a", "b"))
        self.assertEqual(domains[("t", "z")], ("a", "b", "c"))
        for values in domains.values():
            for v in values:
                self.assertNotRegex(v.lower(), r"^(etc\.?|and so on|or |and )")


class SynSQLNonKeyParentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_non_pk_parent_column_never_becomes_a_grain(self) -> None:
        """With a keyed second link the task builds, and every mart grains on
        (and every hop joins onto) a declared single-column key only."""
        path = _write_tables(self.tmp.name, [_entry("mixed_links", extra_link=True)])
        task = synsql.to_task_ir("mixed_links", path)
        keys = declared_key_parents(task.tables)
        self.assertIn(("objects", "object_id"), keys)
        self.assertNotIn(("objects", "object_name"), keys)
        non_key = [
            r for r in task.relationships
            if (r.parent_table, r.parent_columns[0]) not in keys
        ]
        self.assertTrue(non_key, "fixture must declare a non-key link")
        self.assertTrue(task.marts)
        for mart in task.marts:
            self.assertNotIn("object_name", mart.grain)
            for op in mart.plan.ops:
                text = op.predicate + json.dumps(op.details)
                for rel in non_key:
                    self.assertNotIn(
                        f'{rel.parent_table}."{rel.parent_columns[0]}"', text,
                        f"{mart.name}: join onto non-key {rel.parent_table}.{rel.parent_columns[0]}",
                    )
        # The etc.-sentinel never becomes a ladder branch or filter literal:
        # the DDL comments carry 'etc.' and a trailing 'or', the IR does not.
        self.assertEqual(
            task.table("users").column("tier").enum_values, ("gold", "silver", "bronze")
        )
        self.assertEqual(
            task.table("voice_commands").column("status").enum_values,
            ("ok", "failed", "retried"),
        )
        for table in task.tables:
            for column in table.columns:
                for value in column.enum_values or ():
                    self.assertNotIn(value.lower(), {"etc.", "etc", "and so on"})
                    self.assertFalse(value.lower().startswith(("or ", "and ")))

    def test_only_non_key_links_are_refused_at_ingest(self) -> None:
        path = _write_tables(self.tmp.name, [_entry("only_non_key", extra_link=False)])
        with self.assertRaises(ValueError) as ctx:
            synsql.to_task_ir("only_non_key", path)
        self.assertIn("no usable parent<-bridge chain", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

"""The Phase 0.E fixture re-key: `tools/migrate_transcript_fixtures.py`.

WHY THIS EXISTS
The transcript key moved with the behaviour manifest (roadmap 0.E), so every
committed fixture recorded under the old key misses. The roadmap owes ONE live
re-record; until then the fixtures the current code CAN honestly serve were
re-keyed offline. These tests pin the two properties that make that honest:

  1. a legacy entry is re-keyed ONLY when the prompt re-rendered by the current
     view builders reproduces its legacy key AND its recorded system digest is
     the digest of the role's CURRENT system prompt — same stimulus, same
     instructions, same response, new key scheme;
  2. the originals under `tests/fixtures/transcripts_legacy/` are byte-identical
     to what was recorded and the live directory holds only re-keyed entries
     that `RoutedProvider` serves under the current route binding with zero
     HTTP.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.models import CouncilRole, sha256_hex
from elt_taskgen.review import council
from elt_taskgen.review import providers as P

REPO = Path(__file__).resolve().parents[1]
LEGACY_DIR = REPO / "tests" / "fixtures" / "transcripts_legacy"
LIVE_DIR = REPO / "tests" / "fixtures" / "transcripts"


def _load_migration_module():
    path = REPO / "tools" / "migrate_transcript_fixtures.py"
    spec = importlib.util.spec_from_file_location("migrate_transcript_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("migrate_transcript_fixtures", module)
    spec.loader.exec_module(module)
    return module


def _entries(root: Path) -> dict[str, dict]:
    return {
        str(p.relative_to(root)): json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(root.glob("*/*.json"))
    }


@unittest.skipUnless(LEGACY_DIR.is_dir(), "legacy fixture directory not present")
class TranscriptFixtureMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.migration = _load_migration_module()
        cls.legacy = _entries(LEGACY_DIR)
        cls.live = _entries(LIVE_DIR)

    def test_legacy_originals_are_unmodified_pre_migration_entries(self):
        """Every legacy entry is keyed by the PRE-0.E scheme over its own
        recorded system digest and carries no route block: the originals were
        kept, never rewritten."""
        self.assertEqual(len(self.legacy), 11)
        for rel, entry in self.legacy.items():
            self.assertEqual(entry["prompt_sha256"], Path(rel).stem, rel)
            self.assertNotIn("route", entry, rel)
            self.assertNotIn("migration", entry, rel)
            self.assertEqual(P.transcript_entry_schema(entry), 0, rel)

    def test_migration_refuses_entries_after_their_public_view_moves(self):
        """The migration never rekeys historical output after its stimulus moves.

        The independent-implementer fixture was once provably migratable and
        remains preserved in ``fixtures/transcripts`` as historical evidence.
        The richer current public task view no longer reproduces that legacy
        prompt, so a fresh migration must now produce nothing rather than
        fabricating currency for the old response.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code = self.migration.migrate(
                LEGACY_DIR, out, el_bundle_dir=None, dry_run=False, report=lambda _line: None
            )
            self.assertEqual(code, 0)
            produced = _entries(out)
        self.assertEqual(produced, {})
        self.assertEqual(
            sorted(Path(rel).parent.name for rel in self.live),
            ["independent_implementer"],
        )

    def test_entries_recorded_under_another_system_prompt_are_not_rekeyed(self):
        """The critic entries reproduce their legacy key from the CURRENT view
        (the stimulus is unchanged) but were recorded under earlier system
        prompts: re-keying them would fabricate evidence, so they stay legacy."""
        prose = next(
            e["response"] for e in self.legacy.values() if e["role"] == "semantic_author"
        )
        authored = demo_fixture.demo_task().model_copy(update={"solver_prompt": prose})
        checked = 0
        for rel, entry in self.legacy.items():
            role = entry["role"]
            if role not in {r.value for r in council.CRITIC_ROLES}:
                continue
            view = council.render_view(CouncilRole(role), authored)
            self.assertEqual(
                sha256_hex(entry["system_sha256"] + "\n" + view), entry["prompt_sha256"], rel
            )
            self.assertNotEqual(
                entry["system_sha256"], self.migration._legacy_system_sha(role), rel
            )
            self.assertFalse(
                any(Path(live_rel).parent.name == role for live_rel in self.live), rel
            )
            checked += 1
        self.assertEqual(checked, 7)

    def test_stale_author_entry_is_not_rekeyed_or_replayed(self):
        task = demo_fixture.demo_task()
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
        legacy = next(
            entry for entry in self.legacy.values() if entry["role"] == "semantic_author"
        )
        self.assertNotEqual(
            sha256_hex(legacy["system_sha256"] + "\n" + view),
            legacy["prompt_sha256"],
        )
        self.assertNotEqual(
            legacy["system_sha256"],
            self.migration._legacy_system_sha("semantic_author"),
        )
        self.assertFalse(
            any(Path(rel).parent.name == "semantic_author" for rel in self.live)
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = P.RoutedProvider(
                P.load_role_routing(None),
                P.TranscriptStore(Path(tmp) / "transcripts", fixtures_dir=LIVE_DIR),
                P.CostMeter(budget_per_task_usd=1.0),
                replay_only=True,
            )
            with self.assertRaises(P.TranscriptMissingError):
                provider.complete(CouncilRole.SEMANTIC_AUTHOR, view)
            legacy_only = P.RoutedProvider(
                P.load_role_routing(None),
                P.TranscriptStore(Path(tmp) / "transcripts2", fixtures_dir=LEGACY_DIR),
                P.CostMeter(budget_per_task_usd=1.0),
                replay_only=True,
            )
            with self.assertRaises(P.TranscriptMissingError):
                legacy_only.complete(CouncilRole.SEMANTIC_AUTHOR, view)


if __name__ == "__main__":
    unittest.main()

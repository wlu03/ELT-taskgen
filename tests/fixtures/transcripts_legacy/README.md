# Legacy transcript fixtures (pre-Phase 0.E key)

These are the 11 committed demo transcripts exactly as recorded, byte for
byte, under the transcript key that preceded Phase 0.E
(`sha256(sha256(system_prompt) + "\n" + prompt)`). They are kept as EVIDENCE
and are never modified in place.

`tests/fixtures/transcripts/` (the directory `providers.default_fixtures_dir()`
serves) now holds only the entries that `tools/migrate_transcript_fixtures.py`
could re-key HONESTLY under `transcript_key_v3`: the prompt re-rendered from
the demo task through the current view builders reproduces the legacy key AND
the recorded `system_sha256` is the digest of the role's current system prompt,
so the same response answers the same stimulus under the new key scheme and an
`entry_schema: 2` route block. Two entries qualify (`semantic_author`,
`independent_implementer`). The nine others stay here only:

* the seven critic entries were recorded under earlier system prompts, so
  re-keying them would assert that the critics answer today's instructions
  exactly as they answered the old ones (fabricated evidence);
* the two `independent_loader` entries embed an emitted EL bundle that only a
  workspace with frozen gold reproduces (`--el-bundle-dir`).

The roadmap (Phase 0.E, R-G) owes ONE live re-record of the fixture set
(`elt-taskgen record-transcripts`, behind the admission gate, with
credentials). Until then the recorded demo council replay is a VISIBLE skip
(`tests/test_council_screen.py::RecordedDemoCouncilTest`).

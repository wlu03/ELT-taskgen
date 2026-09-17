#!/usr/bin/env python
"""Re-key the committed transcript fixtures under the Phase 0.E transcript key.

WHAT CHANGED. Before 0.E a transcript was keyed
`sha256(sha256(system_prompt) + "\\n" + prompt)`; from 0.E the key is
`transcript_key_v3` over the role's BEHAVIOUR MANIFEST digest (system prompt,
wire tools, tool-choice policy, loop limits, correction text, schema retries,
API version, sandbox pin), the wire-tool digest, the policy digest and the
message prefix, and every entry carries an `entry_schema: 2` route block. Every
fixture recorded under the old scheme therefore misses.

Phase 1 (review finding 1-0): a declared-but-DISABLED runner block
(`roles.semantic_author.session`, `roles.repair_proposer.session`) folds into
the manifest as `{"enabled": false}` (`providers.role_manifest_limits`), so the
one-shot author key no longer moves with the limits of a block no runner
reads; the author fixture was re-keyed by re-running this script from the
legacy originals (the re-keyed copy of an earlier run is replaced, never
edited).

WHAT THIS DOES. A DETERMINISTIC, OFFLINE re-key: no network, no model call, no
money. For every legacy entry it (1) re-renders the prompt from the demo task
through the CURRENT view builders and proves it is the recorded prompt by
reproducing the legacy key, (2) proves the recorded `system_sha256` is the
digest of the CURRENT system prompt of that role, and only then (3) writes a
copy under the new key with the same recorded response, the new behaviour
digest and an entry-schema-2 route block that names the migration. An entry
that fails either proof is NOT re-keyed: re-keying a response recorded under
another system prompt or another view would assert that the model answers
today's stimulus exactly as it answered the old one, which is fabricated
evidence. Such entries stay under `tests/fixtures/transcripts_legacy/` only.

WHAT IT DOES NOT DO. It does not re-record anything. The roadmap (Phase 0.E,
R-G) owes ONE live re-record of the fixture set (`elt-taskgen
record-transcripts` with credentials, behind the admission gate) so the
critic seats' fixtures answer the current prompts; until then the demo
council replay stays a VISIBLE skip. The loader fixtures embed an emitted EL
bundle that only a workspace with frozen gold can reproduce; pass
`--el-bundle-dir` to re-key them from such a workspace.

Usage:
  python tools/migrate_transcript_fixtures.py [--legacy-dir DIR] [--out-dir DIR]
                                              [--el-bundle-dir DIR] [--dry-run]
Exit 0 when every entry was either re-keyed or explicitly skipped; the report
lists both. The originals are never modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from elt_taskgen import demo_fixture  # noqa: E402
from elt_taskgen.models import CouncilRole, canonical_json, sha256_hex  # noqa: E402
from elt_taskgen.reference import independent  # noqa: E402
from elt_taskgen.review import council  # noqa: E402
from elt_taskgen.review import providers as P  # noqa: E402

#: How many resample salts to try for the sampled roles (the demo seeds only
#: sample 0, but a recorded resample is recognised too).
_SAMPLE_INDICES = range(0, 4)


def _legacy_key(system_sha: str, prompt: str) -> str:
    """The pre-0.E transcript key, reconstructed for the proof."""
    return sha256_hex(system_sha + "\n" + prompt)


def _legacy_system_sha(role: str) -> str:
    """The pre-0.E `role_behavior_sha256`: sha256 of the resolved system
    prompt (or of '')."""
    system = P._system_prompt(role, schema_mode=P.uses_findings_schema(role))
    return sha256_hex(system or "")


def _load_entries(legacy_dir: Path) -> list[tuple[Path, dict]]:
    entries = []
    for path in sorted(legacy_dir.glob("*/*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("prompt_sha256") != path.stem:
            raise SystemExit(f"{path}: filename and prompt_sha256 disagree (corrupt store)")
        entries.append((path, entry))
    return entries


def _candidate_prompts(entries: list[tuple[Path, dict]], el_bundle_dir: Path | None) -> dict[str, list[str]]:
    """Every prompt the current code can render for the demo task, per role."""
    task = demo_fixture.demo_task()
    author_view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
    candidates: dict[str, list[str]] = {"semantic_author": [author_view]}

    # The critics, the implementer and the loader read the AUTHORED task: the
    # prose is the recorded author response whose legacy key the author view
    # reproduces (there is exactly one such entry in the committed set).
    prose = None
    for _path, entry in entries:
        if entry.get("role") != "semantic_author":
            continue
        if _legacy_key(str(entry.get("system_sha256") or ""), author_view) == entry["prompt_sha256"]:
            prose = str(entry["response"])
            break
    tasks = [task]
    if prose is not None:
        tasks.insert(0, task.model_copy(update={"solver_prompt": prose}))

    for role in council.CRITIC_ROLES:
        candidates[role.value] = [council.render_view(role, t) for t in tasks]
    candidates[independent.ROLE_NAME] = [
        independent.sample_prompt(t, i) for t in tasks for i in _SAMPLE_INDICES
    ]
    if el_bundle_dir is not None:
        candidates[independent.LOADER_ROLE_NAME] = [
            independent.load_sample_prompt(t, el_bundle_dir, i) for t in tasks for i in _SAMPLE_INDICES
        ]
    else:
        candidates[independent.LOADER_ROLE_NAME] = []
    return candidates


def _migrated_entry(entry: dict, role: str, prompt: str) -> tuple[str, dict]:
    """The re-keyed copy: same response, new key, new behaviour digest, an
    entry-schema-2 route block naming what was migrated."""
    new_key = P.transcript_key(role, prompt)
    behavior = P.role_behavior_sha256(role)
    policy = P.session_policy_for(role)
    provider = str(entry.get("provider") or "")
    model = str(entry.get("model") or "")
    if not provider or not model:
        raise SystemExit(f"legacy entry for {role} carries no provider/model binding")
    out = dict(entry)
    out["prompt_sha256"] = new_key
    out["system_sha256"] = behavior
    # The legacy entries recorded no max_tokens/effort, and this migration
    # invents none: `transcript_route_mismatch` checks them only when present.
    out["route"] = {
        "provider": provider,
        "model": model,
        "behavior_sha256": behavior,
        "tools_sha256": P.role_tools_sha256(role),
        "policy_sha256": policy.sha256(),
        "diagnostics_version": P.DIAGNOSTICS_VERSION,
        "entry_schema": P.TRANSCRIPT_ENTRY_SCHEMA,
    }
    out["migration"] = {
        "tool": "tools/migrate_transcript_fixtures.py",
        "scheme": f"legacy -> transcript_key_v{P.TRANSCRIPT_KEY_VERSION}",
        "legacy_prompt_sha256": str(entry["prompt_sha256"]),
        "legacy_system_sha256": str(entry.get("system_sha256") or ""),
        "prompt_reproduced_from": "demo task through the current view builders",
        "response_unchanged": True,
        "live_rerecord_owed": True,
    }
    return new_key, out


def migrate(
    legacy_dir: Path,
    out_dir: Path,
    *,
    el_bundle_dir: Path | None,
    dry_run: bool,
    report=print,
) -> int:
    entries = _load_entries(legacy_dir)
    if not entries:
        report(f"no legacy entries under {legacy_dir}")
        return 1
    candidates = _candidate_prompts(entries, el_bundle_dir)
    migrated: list[str] = []
    skipped: list[str] = []
    for path, entry in entries:
        role = str(entry.get("role") or path.parent.name)
        recorded_system = str(entry.get("system_sha256") or "")
        current_system = _legacy_system_sha(role)
        prompt = next(
            (c for c in candidates.get(role, []) if _legacy_key(recorded_system, c) == entry["prompt_sha256"]),
            None,
        )
        rel = f"{role}/{path.stem[:12]}"
        if prompt is None:
            skipped.append(
                f"{rel}: prompt not reproducible from the demo task through the current "
                "view builders" + (
                    " (loader prompts embed an emitted EL bundle; pass --el-bundle-dir)"
                    if role == independent.LOADER_ROLE_NAME else ""
                )
            )
            continue
        if recorded_system != current_system:
            skipped.append(
                f"{rel}: recorded under system prompt {recorded_system[:12]}, current "
                f"{current_system[:12]}; re-keying would fabricate evidence — live re-record owed"
            )
            continue
        new_key, out = _migrated_entry(entry, role, prompt)
        target = out_dir / role / f"{new_key}.json"
        migrated.append(f"{rel} -> {role}/{new_key[:12]}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(canonical_json(out), encoding="utf-8")
    report(f"legacy dir: {legacy_dir}\nout dir:    {out_dir}{' (dry run)' if dry_run else ''}")
    report(f"\nre-keyed ({len(migrated)}):")
    for line in migrated:
        report(f"  {line}")
    report(f"\nnot re-keyed ({len(skipped)}), kept under the legacy dir only:")
    for line in skipped:
        report(f"  {line}")
    report(
        "\nNOTE: this is an offline re-key, not a recording. The roadmap (Phase 0.E, "
        "R-G) still owes one live `elt-taskgen record-transcripts` of the fixture set."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy-dir", type=Path, default=REPO / "tests" / "fixtures" / "transcripts_legacy")
    parser.add_argument("--out-dir", type=Path, default=REPO / "tests" / "fixtures" / "transcripts")
    parser.add_argument("--el-bundle-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return migrate(
        args.legacy_dir.resolve(),
        args.out_dir.resolve(),
        el_bundle_dir=args.el_bundle_dir.resolve() if args.el_bundle_dir else None,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    raise SystemExit(main())

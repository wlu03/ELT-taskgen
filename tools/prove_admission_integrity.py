"""Offline proof of the two Aug-13 admission-mechanism defects and their fix.

WHY THIS EXISTS. Both defects are safety properties of "agents propose, code
certifies", and a safety property is only worth what its demonstration is
worth. This script reconstructs the exact situations that occurred — the five
stale marker copies that kept admitting after a revocation, and the view edit
that invalidated 384 of 415 transcripts without moving the fingerprint — and
shows the fixed code refusing, out loud, in each. ZERO live calls: every
provider here is a local oracle, and nothing reads .env.

Run: .venv/bin/python tools/prove_admission_integrity.py
Exit 0 = every proof held; 1 = a proof failed (fail closed).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from elt_taskgen.review import metrology as M  # noqa: E402
from elt_taskgen.review import providers as P  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    if not condition:
        FAILURES.append(label)


def _oracle_report(seed: int = 8944342589527049266, fingerprint: str = ""):
    """An ADMITTED report, produced offline by the in-repo oracle provider."""
    sys.path.insert(0, str(REPO / "tests"))
    from test_council_efficacy import OracleProvider  # noqa: PLC0415

    return M.run_metrology(
        OracleProvider(),
        routing_fingerprint=fingerprint or M.council_routing_fingerprint(
            P.load_role_routing(None)
        ),
        seed=seed,
    )


#: The seats whose harness validator ran on every submitted payload under
#: the harness-6 protocol (SoT T1.1): one validator run per trajectory,
#: recorded as a tool turn with `fresh = True`.
_VALIDATED_SEATS = {"population_adversary", "shortcut_attacker"}


def _fresh_live_rows(report):
    """Offline proof helper: a complete fresh-live TRAJECTORY manifest — one
    schema-4 row per (trial, seat), canary trials included, every model call
    live, the validated seats' rows carrying ONE fresh validator run each
    (`validator_run_count > 0`, harness "6"), no stale result (SoT T8)."""
    evidence = []
    for role, metrics in sorted(report.per_role.items()):
        count = metrics.tampered_count + metrics.clean_count + metrics.canary_trials
        validator_runs = 1 if role in _VALIDATED_SEATS else 0
        for index in range(count):
            nonce = M.sha256_hex(f"{role}:nonce:{index}")[:32]
            evidence.append(
                {
                    "role": role,
                    "trial_nonce": nonce,
                    "trial_index": index,
                    "prompt_sha256": M.sha256_hex(f"{role}:prompt:{index}"),
                    "response_sha256": M.sha256_hex(f"{role}:response:{index}"),
                    "attempt_count": 1,
                    "model_call_count": 1,
                    "correction_count": 0,
                    "correction_kinds": {"schema": 0, "compile": 0},
                    "tool_call_count": 0,
                    "refused_count": 0,
                    "nudge_count": 0,
                    "validator_run_count": validator_runs,
                    "terminal": "SUBMITTED",
                    "live_model_call_count": 1,
                    "stale_tool_result_count": 0,
                    "trajectory_sha256": M.sha256_hex(f"{role}:trajectory:{nonce}"),
                    "provider": "offline-proof-provider",
                    "model": "offline-proof-model",
                    "replayed": False,
                }
            )
    return evidence


def _write_admission_marker(workspace: Path, report, **kwargs):
    """Offline proof helper: write a schema-4 record from a complete
    non-replayed trajectory manifest."""
    return M.write_admission_marker(
        workspace, report, exchange_evidence=_fresh_live_rows(report), **kwargs
    )


# ---------------------------------------------------------------------------
# PROOF 1 — the five stale copies, reconstructed
# ---------------------------------------------------------------------------

def proof_stale_copies() -> None:
    """The quarantined-record situation, byte for byte.

    An admitted run wrote a marker in the metrology workspace; the runbook's
    copy step put it in five pool workspaces; a later run BLOCKED and revoked.
    Under the old design each copy kept admitting. Under the new one every
    consultation refuses and says why.
    """
    print("\nPROOF 1 — a revoked/superseded admission admits NOWHERE")
    fingerprint = M.council_routing_fingerprint(P.load_role_routing(None))
    report = _oracle_report(fingerprint=fingerprint)
    check("the oracle run is admitted (there is something to revoke)", report.admitted)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        metrology_ws = root / "metrology"
        pools = [root / name for name in
                 ("dbt", "dlt", "schemapile", "synsql", "wikidbs")]

        marker = _write_admission_marker(metrology_ws, report)
        check(
            "metrology workspace admits before revocation",
            M.live_admission_ok(metrology_ws, routing_fingerprint=fingerprint),
        )

        # (1a) THE OLD WORKFLOW, typed by hand exactly as the runbook used to
        # prescribe. Every copy is now inert ON ARRIVAL — a copy is precisely
        # the thing that cannot be revoked, so it never admits at all.
        for pool in pools:
            (pool / "state").mkdir(parents=True)
            shutil.copy2(marker, M.marker_path(pool))
        for pool in pools:
            st = M.admission_status(pool, routing_fingerprint=fingerprint)
            check(
                f"a hand-copied record in {pool.name}/ never admits, even "
                "BEFORE any revocation",
                (not st.ok) and "COPY" in st.reason,
                st.reason if pool is pools[0] else "",
            )

        # (1b) A later run BLOCKS. Revocation writes the tombstone ONCE.
        tombstone = M.revoke_admission(
            metrology_ws,
            reason="a metrology run BLOCKED on ambiguity_critic, "
                   "population_adversary",
            seed=15596299314441845844,
        )
        check("revocation wrote a tombstone, not an absence", tombstone.is_file())

        st = M.admission_status(metrology_ws, routing_fingerprint=fingerprint)
        check(
            "the revoked record refuses AND says it was revoked",
            (not st.ok) and "REVOKED" in st.reason and "15596299314441845844"
            in st.reason,
            st.reason,
        )

        # (1c) THE DEFECT, restated: under the OLD design each copy was a
        # verbatim {"admitted": true, routing_fingerprint: <matching>} file
        # that kept admitting after the revocation above. Now none does.
        for pool in pools:
            check(
                f"copy in {pool.name}/ refuses after revocation",
                not M.live_admission_ok(pool, routing_fingerprint=fingerprint),
            )

        # (1d) …and the reason a copy refuses is not luck. With the record
        # CONSULTED (the replacement workflow) there is nothing to go stale.
        env_before = os.environ.get(M.ADMISSION_ENV)
        try:
            os.environ[M.ADMISSION_ENV] = str(M.marker_path(metrology_ws))
            for pool in pools:
                st = M.admission_status(pool, routing_fingerprint=fingerprint)
                check(
                    f"{pool.name}/ consulting the ONE record sees the revocation",
                    (not st.ok) and "REVOKED" in st.reason,
                )
            # Re-earning the admission re-admits every consumer at once.
            _write_admission_marker(metrology_ws, report)
            check(
                "re-earning at the single record re-admits all five pools",
                all(
                    M.live_admission_ok(p, routing_fingerprint=fingerprint)
                    for p in pools
                ),
            )
            # A named-but-missing record is a REFUSAL, never a fallback to a
            # local copy (the copies are still lying on disk in this scenario).
            os.environ[M.ADMISSION_ENV] = str(root / "nowhere" / "record")
            st = M.admission_status(pools[0], routing_fingerprint=fingerprint)
            check(
                "a missing $ELT_TASKGEN_ADMISSION record refuses instead of "
                "falling back to the local copy",
                (not st.ok) and "authoritative" in st.reason,
                st.reason,
            )
        finally:
            if env_before is None:
                os.environ.pop(M.ADMISSION_ENV, None)
            else:
                os.environ[M.ADMISSION_ENV] = env_before

    # (1e) Historical quarantined records are optional: cleanup may remove
    # them. When present, every one must still refuse; when absent, the fully
    # reconstructed copy/revocation proof above remains the executable proof.
    print("  -- optional records quarantined by hand --")
    quarantined = sorted(
        (REPO / "council" / "revoked").glob("*council.live_admitted.revoked-*")
    )
    check(
        "historical quarantine is absent or complete",
        len(quarantined) in (0, 5),
        f"{len(quarantined)} found",
    )
    with tempfile.TemporaryDirectory() as tmp:
        for src in quarantined:
            pool = Path(tmp) / src.parent.parent.name
            (pool / "state").mkdir(parents=True)
            shutil.copy2(src, M.marker_path(pool))
            st = M.admission_status(pool, routing_fingerprint=fingerprint)
            check(
                f"un-quarantined {src.parent.parent.name} record still refuses",
                not st.ok,
                st.reason,
            )
            # And it refuses even with NO fingerprint supplied — the caller
            # that only asks "is anything admitted here?" must not be fooled.
            check(
                f"…and refuses even unbound to a routing ({src.parent.parent.name})",
                not M.live_admission_ok(pool),
            )


# ---------------------------------------------------------------------------
# PROOF 2 — a view edit moves the fingerprint and stales the record
# ---------------------------------------------------------------------------

_VIEW_MUTATION = '''
# --- injected by tools/prove_admission_integrity.py ---
_orig_public_source_schema_lines = _public_source_schema_lines


def _public_source_schema_lines(task):  # noqa: F811
    """The Aug-13 enrichment, in miniature: one extra shipped line."""
    return _orig_public_source_schema_lines(task) + [
        "  (relationship optionality: every FK is optional unless stated)"
    ]
'''

_PRINT_FP = (
    "import sys; sys.path.insert(0, %r)\n"
    "from elt_taskgen.review import metrology as M, providers as P\n"
    "print(M.view_digest())\n"
    "print(M.council_routing_fingerprint(P.load_role_routing(None)))\n"
)


def _copy_src_as_wheel(dest: Path) -> Path:
    """A byte-identical copy of `src/` laid out as an INSTALLED WHEEL: no
    sibling `pyproject.toml`, so `package_resources.checkout_root()` is None
    and the packaged `config/agents.yaml` is read from
    `elt_taskgen/_resources/` (what `setup.py` bundles).  There is no
    embedded default roster any more — `providers.DEFAULT_ROUTING_DOC` reads
    the packaged file at import time and fails closed without it — so the
    one resource the fingerprint needs is copied beside the sources exactly
    as a wheel carries it."""
    shutil.copytree(REPO / "src", dest)
    bundled = dest / "elt_taskgen" / "_resources" / "config"
    bundled.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "config" / "agents.yaml", bundled / "agents.yaml")
    return dest


def _fingerprint_in(src_root: Path, cwd: Path, hashseed: str) -> tuple[str, str]:
    """(view_digest, routing_fingerprint) from a FRESH process."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    env.pop("ELT_TASKGEN_ADMISSION", None)
    out = subprocess.run(
        [sys.executable, "-c", _PRINT_FP % str(src_root)],
        capture_output=True, text=True, check=True, cwd=str(cwd), env=env,
    )
    view, fingerprint = out.stdout.split()
    return view, fingerprint


def proof_view_covered() -> None:
    print("\nPROOF 2 — a VIEW edit moves the fingerprint and stales the record")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scratch = _copy_src_as_wheel(root / "src")

        base_view, base_fp = _fingerprint_in(scratch, root, "0")
        repo_view, repo_fp = _fingerprint_in(REPO / "src", REPO, "0")
        # The VIEW digest and the routing fingerprint must both be identical
        # from an unmodified copy of the sources (and the same packaged
        # config) at a different absolute path: neither hashes a path.
        check(
            "an unmodified copy of src/ at another path yields the same view "
            "digest",
            base_view == repo_view,
            f"{base_view[:16]}… == {repo_view[:16]}…",
        )
        check(
            "…and the same routing fingerprint (the packaged config, not a "
            "path, is what it reads)",
            base_fp == repo_fp,
            f"{base_fp[:16]}… == {repo_fp[:16]}…",
        )

        # Mutate ONLY the view renderer: no prompt, no model, no pool, no
        # harness constant. Under the old fingerprint this was invisible.
        council = scratch / "elt_taskgen" / "review" / "council.py"
        council.write_text(
            council.read_text(encoding="utf-8") + _VIEW_MUTATION, encoding="utf-8"
        )
        mut_view, mut_fp = _fingerprint_in(scratch, root, "0")
        check("the view digest moves", mut_view != base_view,
              f"{base_view[:16]}… -> {mut_view[:16]}…")
        check("the routing fingerprint moves with it", mut_fp != base_fp,
              f"{base_fp[:16]}… -> {mut_fp[:16]}…")

        # …and an admission earned on the pre-edit view stops admitting.
        report = _oracle_report(fingerprint=base_fp)
        ws = root / "ws"
        _write_admission_marker(ws, report)
        check("the record admits at the pre-edit view",
              M.live_admission_ok(ws, routing_fingerprint=base_fp))
        st = M.admission_status(ws, routing_fingerprint=mut_fp)
        check(
            "the same record is STALE at the post-edit view, and says so",
            (not st.ok) and "STALE" in st.reason and "critic view" in st.reason,
            st.reason,
        )

        # Reverting restores it exactly: the digest tracks content, not events.
        shutil.rmtree(scratch)
        _copy_src_as_wheel(scratch)
        rev_view, rev_fp = _fingerprint_in(scratch, root, "0")
        check("reverting the edit restores the exact fingerprint",
              (rev_view, rev_fp) == (base_view, base_fp))


def proof_determinism() -> None:
    """The trap named in the brief: no PYTHONHASHSEED, path or clock leakage."""
    print("\nPROOF 3 — the digest is stable across processes, seeds and paths")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        elsewhere = root / "a" / "deeper" / "workspace"
        elsewhere.mkdir(parents=True)
        copied_src = _copy_src_as_wheel(root / "copied-src")

        runs = {
            "repo src, cwd=repo, PYTHONHASHSEED=0":
                _fingerprint_in(REPO / "src", REPO, "0"),
            "repo src, cwd=deep tmp dir, PYTHONHASHSEED=1":
                _fingerprint_in(REPO / "src", elsewhere, "1"),
            "repo src, cwd=/, PYTHONHASHSEED=12345":
                _fingerprint_in(REPO / "src", Path("/"), "12345"),
            "repo src, cwd=repo, PYTHONHASHSEED=random":
                _fingerprint_in(REPO / "src", REPO, "random"),
            # A byte-identical COPY of the sources at a different absolute
            # path: the view digest must not notice, because a view is a
            # function of (role, task) and of nothing on disk.
            "COPIED src at another path, cwd=/, PYTHONHASHSEED=random":
                _fingerprint_in(copied_src, Path("/"), "random"),
        }
        for label, (view, fingerprint) in runs.items():
            print(f"         view {view[:16]}…  fp {fingerprint[:16]}…  {label}")
        check(
            "the VIEW digest is identical in all five processes — different "
            "PYTHONHASHSEED, different cwd, different source path",
            len({view for view, _ in runs.values()}) == 1,
        )
        repo_runs = [fp for label, (_, fp) in runs.items()
                     if not label.startswith("COPIED")]
        check(
            "the routing fingerprint is identical across seeds and cwds "
            "(same config root)",
            len(set(repo_runs)) == 1,
        )


def proof_supersession_and_tamper() -> None:
    print("\nPROOF 4 — a record cannot outlive the bar it cleared, or be edited")
    fingerprint = M.council_routing_fingerprint(P.load_role_routing(None))
    report = _oracle_report(fingerprint=fingerprint)
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        path = _write_admission_marker(ws, report)
        check("admits as written", M.live_admission_ok(ws, routing_fingerprint=fingerprint))

        # Hand-edit a seat's recall upward: the digest catches it.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["evidence"]["per_role"]["ambiguity_critic"]["recall"] = 1.0
        data["evidence"]["per_role"]["ambiguity_critic"]["detected_count"] = 99
        path.write_text(json.dumps(data), encoding="utf-8")
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check("an edited record refuses (evidence vs its own digest)",
              (not st.ok) and "edited or truncated" in st.reason, st.reason)

        # A SELF-CONSISTENT record — correct digest, `admitted: true` — whose
        # numbers do not clear the bar. Only a re-derived verdict catches it;
        # a code path that trusted the flag would admit here.
        _write_admission_marker(ws, report)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["evidence"]["per_role"]["ambiguity_critic"]["recall"] = 0.0
        data["evidence"]["per_role"]["ambiguity_critic"]["detected_count"] = 0
        data["evidence_sha256"] = M.sha256_hex(
            M.canonical_json(data["evidence"])
        )
        path.write_text(M.canonical_json(data), encoding="utf-8")
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check(
            "a self-consistent record claiming admission on a FAILING seat is "
            "refused — the verdict is re-derived, not trusted",
            (not st.ok) and "does NOT clear the current bar" in st.reason,
            st.reason,
        )

        # THE CONVERSE, which was unguarded (finding p4-1-1): tightening the
        # bar retires an admission, and LOOSENING it must not resurrect one.
        # The `metrology:` bars are NOT in `council_routing_fingerprint`, so
        # a loosened bar moves no digest at all; the record's own recorded
        # thresholds are what pin it.
        _write_admission_marker(ws, report)
        check("admits under the shipped bar", M.live_admission_ok(ws, routing_fingerprint=fingerprint))
        loose = ws / "loose-agents.yaml"
        import yaml as _yaml

        document = _yaml.safe_load(
            P.default_agents_config_path().read_text(encoding="utf-8")
        )
        document["metrology"] = {
            **dict(document.get("metrology") or {}),
            "min_recall": 0.0,
            "min_precision": 0.0,
            "max_nitpick_rate": 1.0,
        }
        loose.write_text(_yaml.safe_dump(document), encoding="utf-8")
        st = M.admission_status(
            ws, routing_fingerprint=fingerprint, agents_config=loose
        )
        check(
            "a LOOSENED admission bar cannot resurrect a record: the record "
            "carries the bar it was measured under, and a looser current bar "
            "is refused [bar_loosened]",
            (not st.ok) and "bar_loosened" in st.reason,
            st.reason,
        )

        # A v1-shaped record (the pre-fix format) is refused outright.
        _write_admission_marker(ws, report)
        path.write_text(json.dumps({
            "admitted": True, "routing_fingerprint": fingerprint,
            "harness_version": M.HARNESS_VERSION, "seed": 1,
        }), encoding="utf-8")
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check("a v1-format record is refused as SUPERSEDED",
              (not st.ok) and "SUPERSEDED" in st.reason, st.reason)


# Proof 5: validator changes move the critic tool surface and stale its record.
# Compile validators appear only for enabled POP/SHC sessions.
_VALIDATOR_DESCRIPTION_MUTATION = '''
# --- injected by tools/prove_admission_integrity.py (PROOF 5) ---
_orig_tool_description_for = _tool_description_for


def _tool_description_for(role_name):  # noqa: F811
    """A one-line edit to the description the critics' payload validator
    carries on the wire: no prompt, no model, no pool, no view."""
    return _orig_tool_description_for(role_name) + " (edited by PROOF 5)"
'''

_PRINT_SURFACE = (
    "import sys; sys.path.insert(0, %r)\n"
    "from elt_taskgen.review import metrology as M, providers as P\n"
    "print(M.view_digest())\n"
    "print(M.tool_surface_sha256())\n"
    "print(M.council_routing_fingerprint(P.load_role_routing(None)))\n"
)

_PRINT_STATUS = (
    "import sys; sys.path.insert(0, %r)\n"
    "from pathlib import Path\n"
    "from elt_taskgen.review import metrology as M, providers as P\n"
    "fp = M.council_routing_fingerprint(P.load_role_routing(None))\n"
    "st = M.admission_status(Path(%r), routing_fingerprint=fp)\n"
    "print('OK' if st.ok else 'REFUSED')\n"
    "print(st.reason)\n"
)


def _run_in(script: str, cwd: Path) -> list[str]:
    """stdout lines of a fully formatted script run in a FRESH process."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env.pop("ELT_TASKGEN_ADMISSION", None)
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=True, cwd=str(cwd), env=env,
    )
    return out.stdout.splitlines()


def _surface_in(src_root: Path, cwd: Path) -> tuple[str, str, str]:
    """(view_digest, tool_surface_sha256, routing_fingerprint) from a FRESH
    process importing `src_root`."""
    view, surface, fingerprint = _run_in(_PRINT_SURFACE % str(src_root), cwd)
    return view, surface, fingerprint


def proof_tool_surface_covered() -> None:
    print("\nPROOF 5 — a critic-wired VALIDATOR description edit moves the tool "
          "surface, not the view, and stales the record naming the tool surface")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # A wheel-layout copy carrying the SAME packaged config, so its
        # fingerprint equals the repository's and a record earned here reads
        # under it.
        scratch = _copy_src_as_wheel(root / "src")

        base_view, base_surface, base_fp = _surface_in(scratch, root)
        repo_view, repo_surface, repo_fp = _surface_in(REPO / "src", REPO)
        check(
            "an unmodified copy of src/ + the packaged config yields the "
            "repository's view, tool surface and fingerprint",
            (base_view, base_surface, base_fp) == (repo_view, repo_surface, repo_fp),
            f"surface {base_surface[:16]}…  fp {base_fp[:16]}…",
        )

        # Mutate ONLY the validator description on the wire.
        providers = scratch / "elt_taskgen" / "review" / "providers.py"
        providers.write_text(
            providers.read_text(encoding="utf-8") + _VALIDATOR_DESCRIPTION_MUTATION,
            encoding="utf-8",
        )
        mut_view, mut_surface, mut_fp = _surface_in(scratch, root)
        check("the view digest is UNCHANGED (no stimulus moved)",
              mut_view == base_view, f"{base_view[:16]}… == {mut_view[:16]}…")
        check("tool_surface_sha256 moves", mut_surface != base_surface,
              f"{base_surface[:16]}… -> {mut_surface[:16]}…")
        check("the routing fingerprint moves with it", mut_fp != base_fp,
              f"{base_fp[:16]}… -> {mut_fp[:16]}…")

        # A record earned under the pre-edit surface: admits there, and under
        # the post-edit surface refuses NAMING the tool surface (checked in a
        # process whose tool surface IS the mutated one).
        report = _oracle_report(fingerprint=base_fp)
        ws = root / "ws"
        _write_admission_marker(ws, report)
        check("the record admits under the pre-edit tool surface",
              M.live_admission_ok(ws, routing_fingerprint=base_fp))
        verdict, reason = _run_in(_PRINT_STATUS % (str(scratch), str(ws)), root)[:2]
        check(
            "the same record is STALE under the post-edit tool surface, and the "
            "refusal names the tool surface",
            verdict == "REFUSED" and "STALE" in reason and "tool surface" in reason,
            reason,
        )

        # Reverting restores it exactly: the digest tracks content, not events.
        shutil.rmtree(scratch)
        _copy_src_as_wheel(scratch)
        rev_view, rev_surface, rev_fp = _surface_in(scratch, root)
        check("reverting the edit restores the exact surface and fingerprint",
              (rev_view, rev_surface, rev_fp) == (base_view, base_surface, base_fp))

    # Disabled POP/SHC validators affect neither validator digests nor the
    # view/tool fingerprint; toggling either seat requires re-admission.
    print("  -- the one-shot rollback: no validator wired, nothing hashed --")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scratch = root / "src"
        shutil.copytree(REPO / "src", scratch)
        shutil.copytree(REPO / "config", root / "config")
        # A CHECKOUT marker (`package_resources.checkout_root`), so the scratch
        # tree reads ITS OWN config/agents.yaml — the disabled seats must come
        # from the document.
        shutil.copy2(REPO / "pyproject.toml", root / "pyproject.toml")
        for role in ("population_adversary", "shortcut_attacker"):
            _set_seat(root / "config" / "agents.yaml", role, enabled=False)
        digests = _run_in(_PRINT_VALIDATORS % str(scratch), root)
        check(
            "no harness validator is wired while every seat is one-shot "
            "(validators.code and .binaries are empty)",
            digests == ["", ""],
            f"code {digests[0]!r} binaries {digests[1]!r}",
        )
        oneshot_view, oneshot_surface, oneshot_fp = _surface_in(scratch, root)
        check("disabling the seats leaves the view digest unchanged",
              oneshot_view == repo_view)
        check("disabling the seats moves the tool surface and the fingerprint "
              "(the seat flip is a re-earn in either direction)",
              oneshot_surface != repo_surface and oneshot_fp != repo_fp,
              f"surface {repo_surface[:16]}… -> {oneshot_surface[:16]}…")

    # Enabled compile_proposal participates in the validator/tool fingerprint.
    # Editing its projection stales admission without changing the critic view.
    print("  -- harness 6: a WIRED compile validator's projection description --")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scratch = _copy_src_as_wheel(root / "src")

        wired_view, wired_surface, wired_fp = _surface_in(scratch, root)
        check("the enabled scratch tree is the repository's tree (view, "
              "surface, fingerprint)",
              (wired_view, wired_surface, wired_fp) == (repo_view, repo_surface, repo_fp),
              f"surface {wired_surface[:16]}…  fp {wired_fp[:16]}…")
        digests = _run_in(_PRINT_VALIDATORS % str(scratch), root)
        check("the wired validator's modules (attacks.py included) and the "
              "pinned duckdb / sqlglot are hashed",
              "elt_taskgen.verification.attacks" in digests[0]
              and "duckdb" in digests[1] and "sqlglot" in digests[1],
              f"code {digests[0]}\nbinaries {digests[1]}")

        # The record, earned by an oracle run IN the enabled pre-edit tree.
        ws = root / "ws"
        earned = _run_in(_EARN_SCRIPT % (str(scratch), str(REPO / "tests"), str(ws)), root)
        check("an oracle run in the enabled tree earns a harness-6 record",
              earned and earned[0] == "ADMITTED", "\n".join(earned[:3]))
        verdict, reason = _run_in(_PRINT_STATUS % (str(scratch), str(ws)), root)[:2]
        check("the record admits under the pre-edit (enabled) tool surface",
              verdict == "OK", reason)

        # Mutate ONLY the validator's projection description.
        validators = scratch / "elt_taskgen" / "review" / "tools" / "critic_validators.py"
        validators.write_text(
            validators.read_text(encoding="utf-8") + _COMPILE_DESCRIPTION_MUTATION,
            encoding="utf-8",
        )
        mut_view, mut_surface, mut_fp = _surface_in(scratch, root)
        check("the view digest is UNCHANGED (no stimulus moved)",
              mut_view == wired_view, f"{wired_view[:16]}… == {mut_view[:16]}…")
        check("tool_surface_sha256 moves with the validator description",
              mut_surface != wired_surface,
              f"{wired_surface[:16]}… -> {mut_surface[:16]}…")
        check("the routing fingerprint moves with it", mut_fp != wired_fp,
              f"{wired_fp[:16]}… -> {mut_fp[:16]}…")
        verdict, reason = _run_in(_PRINT_STATUS % (str(scratch), str(ws)), root)[:2]
        check(
            "the same record is STALE under the edited validator, and the "
            "refusal names the tool surface",
            verdict == "REFUSED" and "STALE" in reason and "tool surface" in reason,
            reason,
        )


def _set_seat(agents_yaml: Path, role: str, *, enabled: bool) -> None:
    """Set `roles.<role>.session.enabled` in a scratch config."""
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load(agents_yaml.read_text(encoding="utf-8"))
    doc["roles"][role]["session"]["enabled"] = bool(enabled)
    agents_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


_PRINT_VALIDATORS = (
    "import sys; sys.path.insert(0, %r)\n"
    "from elt_taskgen.review import metrology as M\n"
    "d = M.validator_digests()\n"
    "print(' '.join(sorted(d['code'])))\n"
    "print(' '.join(sorted(d['binaries'])))\n"
)

#: An oracle run and a schema-4 record written IN the scratch tree: the
#: source and the config the subprocess imports are the scratch tree's, so
#: the record is earned under ITS tool surface (the enabled seat's).
_EARN_SCRIPT = (
    "import sys; sys.path.insert(0, %r); sys.path.append(%r)\n"
    "from pathlib import Path\n"
    "from elt_taskgen.review import metrology as M, providers as P\n"
    "from test_council_efficacy import OracleProvider\n"
    "fp = M.council_routing_fingerprint(P.load_role_routing(None))\n"
    "report = M.run_metrology(OracleProvider(), routing_fingerprint=fp, seed=8944342589527049266)\n"
    "validated = {'population_adversary', 'shortcut_attacker'}\n"
    "rows = []\n"
    "for role, m in sorted(report.per_role.items()):\n"
    "    for i in range(m.tampered_count + m.clean_count + m.canary_trials):\n"
    "        rows.append({'role': role, 'prompt_sha256': M.sha256_hex(f'{role}:p:{i}'),\n"
    "                     'response_sha256': M.sha256_hex(f'{role}:r:{i}'), 'attempt_count': 1,\n"
    "                     'model_call_count': 1, 'correction_count': 0, 'tool_call_count': 0,\n"
    "                     'refused_count': 0, 'nudge_count': 0,\n"
    "                     'validator_run_count': 1 if role in validated else 0,\n"
    "                     'terminal': 'SUBMITTED', 'live_model_call_count': 1,\n"
    "                     'stale_tool_result_count': 0, 'provider': 'offline-proof-provider',\n"
    "                     'model': 'offline-proof-model', 'replayed': False})\n"
    "M.write_admission_marker(Path(%r), report, exchange_evidence=rows)\n"
    "print('ADMITTED' if report.admitted else 'BLOCKED')\n"
    "print(fp)\n"
)

_COMPILE_DESCRIPTION_MUTATION = '''
# --- injected by tools/prove_admission_integrity.py (PROOF 5, harness 6) ---
CompileProposalTool.description = (
    CompileProposalTool.description + " (projection description edited by PROOF 5)"
)
'''


# ---------------------------------------------------------------------------
# PROOF 6 — a schema-4 record with one stale tool result is refused
# ---------------------------------------------------------------------------

def _resigned(path: Path, mutate) -> None:
    """Re-sign the record at `path` after `mutate(data)`: a SELF-CONSISTENT
    record, so only a re-derived check can refuse it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    data["evidence_sha256"] = M.sha256_hex(M.canonical_json(data["evidence"]))
    path.write_text(M.canonical_json(data), encoding="utf-8")


def _summarize(rows, report) -> tuple[bool, str]:
    """(refused, reason) of summarizing `rows` against `report`'s shape."""
    try:
        M.summarize_fresh_live_trajectories(
            rows, expected_by_role=M.expected_trajectories_by_role(report)
        )
    except M.LiveExchangeEvidenceError as exc:
        return True, str(exc)
    return False, ""


def proof_stale_tool_result_refused() -> None:
    print("\nPROOF 6 — a schema-4 record recording ONE stale tool result is refused")
    fingerprint = M.council_routing_fingerprint(P.load_role_routing(None))
    report = _oracle_report(fingerprint=fingerprint)
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        path = _write_admission_marker(ws, report)
        data = json.loads(path.read_text(encoding="utf-8"))
        validated_rows = sum(
            seat["tampered_count"] + seat["clean_count"] + seat["canary_trials"]
            for role, seat in data["evidence"]["per_role"].items()
            if role in _VALIDATED_SEATS
        )
        check("the record is schema 4, harness 6, with the trajectory fields and "
              "validator runs recorded (validator_run_count_total > 0)",
              data["schema"] == 4
              and data["evidence"]["harness_version"] == "6"
              and data["evidence"]["stale_tool_result_count"] == 0
              and data["evidence"]["replayed_model_call_count"] == 0
              and data["evidence"]["tool_call_count_total"] == 0
              and data["evidence"]["validator_run_count_total"] == validated_rows > 0
              and all(seat["validator_run_count"] > 0
                      for role, seat in data["evidence"]["per_role"].items()
                      if role in _VALIDATED_SEATS)
              and all(seat["canary_hits"] == 0 and seat["canary_trials"] == M.CANARY_PER_ROLE
                      for seat in data["evidence"]["per_role"].values()),
              f"validator runs recorded: {data['evidence']['validator_run_count_total']}")
        check("it names its pool families and attests per-trial isolation",
              len(data["evidence"]["pool_families"]) >= 3
              and data["evidence"]["isolation"]["per_trial_fresh_workspace"] is True
              and data["evidence"]["isolation"]["teardown_verified"] is True
              and data["evidence"]["isolation"]["cross_trial_cache"] is False,
              f"families {data['evidence']['pool_families']}")
        check("it admits as written",
              M.live_admission_ok(ws, routing_fingerprint=fingerprint))

        # SELF-CONSISTENT (re-signed) record claiming one validator result was
        # served from a cache rather than computed in its trial.
        _resigned(path, lambda d: d["evidence"].__setitem__("stale_tool_result_count", 1))
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check(
            "a re-signed record with stale_tool_result_count = 1 is refused, "
            "naming the stale result",
            (not st.ok) and "stale tool result" in st.reason
            and "edited or truncated" not in st.reason,
            st.reason,
        )

        # The per-SEAT twin: the run total says zero, one seat's own counter
        # says one — refused all the same, naming the seat.
        _write_admission_marker(ws, report)
        _resigned(
            path,
            lambda d: d["evidence"]["per_role"]["shortcut_attacker"].__setitem__(
                "stale_tool_result_count", 1
            ),
        )
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check(
            "a re-signed record whose SHORTCUT ATTACKER seat records one stale "
            "result is refused, naming the seat",
            (not st.ok) and "stale tool result" in st.reason
            and "shortcut_attacker" in st.reason,
            st.reason,
        )

        # A replayed model call, run-level and per seat: not live, not evidence.
        _write_admission_marker(ws, report)
        _resigned(
            path,
            lambda d: d["evidence"].__setitem__(
                "live_model_call_count_total", d["evidence"]["model_call_count_total"] - 1
            ),
        )
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check("a re-signed record with one model call not live is refused",
              (not st.ok) and "live" in st.reason and "model call" in st.reason,
              st.reason)
        _write_admission_marker(ws, report)
        _resigned(
            path,
            lambda d: d["evidence"]["per_role"]["population_adversary"].__setitem__(
                "live_model_call_count",
                d["evidence"]["per_role"]["population_adversary"]["model_call_count"] - 1,
            ),
        )
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check("a re-signed record whose POPULATION ADVERSARY seat replayed one "
              "model call is refused, naming the seat",
              (not st.ok) and "live" in st.reason and "population_adversary" in st.reason,
              st.reason)

        # The validator-side twin at the manifest: a row carrying a stale
        # result — on a seat that ran a validator — never summarizes into
        # admission evidence in the first place; nor does a replayed row.
        rows = _fresh_live_rows(report)
        stale_index = next(i for i, r in enumerate(rows) if r["role"] in _VALIDATED_SEATS)
        stale_rows = list(rows)
        stale_rows[stale_index] = {**rows[stale_index], "stale_tool_result_count": 1}
        refused, reason = _summarize(stale_rows, report)
        check("a trajectory manifest with a stale validator result cannot be summarized",
              refused and "stale" in reason, reason)
        replayed_rows = list(rows)
        replayed_rows[0] = {**rows[0], "replayed": True, "live_model_call_count": 0}
        refused, reason = _summarize(replayed_rows, report)
        check("a trajectory manifest with a replayed model call cannot be summarized",
              refused and "replayed" in reason, reason)
        faulted_rows = list(rows)
        faulted_rows[0] = {**rows[0], "terminal": "HARNESS_FAULT"}
        refused, reason = _summarize(faulted_rows, report)
        check("a trajectory that ended in a harness fault is never evidence",
              refused and "fault" in reason, reason)

        # A harness-5 record (the trajectory protocol with one-shot seats)
        # is SUPERSEDED outright, naming both versions.
        _write_admission_marker(ws, report)
        _resigned(path, lambda d: (d.__setitem__("harness_version", "5"),
                                   d["evidence"].__setitem__("harness_version", "5")))
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check("a harness-5 record is refused as SUPERSEDED, naming '5' and '6'",
              (not st.ok) and "SUPERSEDED" in st.reason and "'5'" in st.reason
              and "'6'" in st.reason, st.reason)

        # And a schema-3 record (the harness-4 shape) is SUPERSEDED outright.
        _write_admission_marker(ws, report)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema"] = 3
        path.write_text(M.canonical_json(data), encoding="utf-8")
        st = M.admission_status(ws, routing_fingerprint=fingerprint)
        check("a schema-3 record is refused as SUPERSEDED",
              (not st.ok) and "SUPERSEDED" in st.reason and "schema 3" in st.reason,
              st.reason)


def main() -> int:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("note: a key is present in the environment; this script makes no "
              "network calls and consults only local oracles.")
    proof_stale_copies()
    proof_view_covered()
    proof_determinism()
    proof_supersession_and_tamper()
    proof_tool_surface_covered()
    proof_stale_tool_result_refused()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} proof(s) did not hold")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PROOFS HELD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

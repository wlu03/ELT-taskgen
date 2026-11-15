"""Author and review task specifications with identity-bound provider evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from elt_taskgen.engine import (
    Engine,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    CouncilRole,
    Finding,
    RepairRoute,
    Severity,
    TaskIR,
    canonical_json,
    readable_json,
    sha256_hex,
)
from elt_taskgen.pipeline import attacks as _pipeline_attacks
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import handoff as _pipeline_handoff
from elt_taskgen.pipeline import payloads as _pipeline_payloads


def _admission_gate(provider, workspace: Path | None) -> tuple[str | None, dict[str, str]]:
    """(failure detail or None, admission PROVENANCE) for one live-capable call site.

    A LIVE-capable provider needs the council.live_admitted marker from a passing
    `metrology` run, bound to the current critic routing; absent, unreadable or
    mismatched fails the stage CLOSED. The provenance is returned because
    revocation is PROSPECTIVE: unless each gated call records WHICH admission it
    passed under, revoking one cannot list what ran on it."""
    # EXEMPTION IS A POSITIVE CAPABILITY TEST, never absence of an attribute: exempt
    # only when replay_only is True (cannot call live) or there is no `routing` at
    # all. Anything routable to a real model must show a marker.
    from elt_taskgen.review import metrology as metrology_mod

    replay_only = getattr(provider, "replay_only", None)
    routing = getattr(provider, "routing", None)
    if replay_only is True:
        return None, metrology_mod.AdmissionStatus(
            ok=True, reason="replay-only: no live call is possible"
        ).provenance(metrology_mod.ADMISSION_MODE_REPLAY_ONLY)
    if replay_only is None and routing is None:
        # not a live-capable provider: no endpoint, no exposure
        return None, metrology_mod.AdmissionStatus(
            ok=True, reason="no routing: not a live-capable provider"
        ).provenance(metrology_mod.ADMISSION_MODE_NO_ROUTING)

    fingerprint = (
        metrology_mod.council_routing_fingerprint(routing)
        if routing is not None
        else None
    )
    status = metrology_mod.admission_status(
        workspace,
        routing_fingerprint=fingerprint,
        # The record is checked against the agents document THIS provider's
        # routing was loaded from (a custom --agents-config), never always
        # the repository default.
        agents_config=getattr(provider, "agents_config", None),
        record_path=getattr(provider, "admission_reference", None),
    )
    if status.ok:
        return None, status.provenance(metrology_mod.ADMISSION_MODE_ADMITTED)
    # Name the ACTUAL cause: the five failure modes have different remedies, and a
    # generic "absent or stale" sent operators hunting for the wrong fix.
    return f"{metrology_mod.NOT_ADMITTED_MESSAGE}: {status.reason}", {}


def _stamp_admission(provider, provenance: dict[str, str]) -> None:
    """Carry the admission this call site passed into the provider, so every
    transcript it records is stamped with it. Never fatal: provenance is evidence,
    not a gate."""
    if not provenance:
        return
    try:
        provider.admission_provenance = dict(provenance)
    except (AttributeError, TypeError):  # frozen/slotted stub: nothing to do
        pass


def _begin_task_evidence(provider, task: TaskIR) -> bool:
    """Start a task-bound exchange manifest when the provider supports it.

    A provider whose `begin_task_evidence` takes `task=` (the `RoutedProvider`)
    receives the TaskIR too: that opens the TASK CONTEXT under which a
    harness-validated critic seat (`providers.AGENTIC_ROLES`, its block
    `enabled: true`) runs a bounded session inside `run_council`'s one
    `complete` call; a double that takes the identity only stays valid."""
    begin = getattr(provider, "begin_task_evidence", None)
    if not callable(begin):
        return False  # lightweight test doubles remain valid
    if _accepts_keyword(begin, "task"):
        begin(task.task_id, task.content_hash(), task=task)
    else:
        begin(task.task_id, task.content_hash())
    return True


def _accepts_keyword(fn, name: str) -> bool:
    """Does callable `fn` accept keyword `name` (or `**kwargs`)?"""
    import inspect

    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _validated_review_manifest(
    provider, task: TaskIR, findings: list[Finding], *, required: bool
) -> tuple[tuple[dict, ...], list[str]]:
    """Validate one task-bound exchange row per critic role.

    Accept legacy one-shot rows and schema-3 session rows only when their call
    counts and finding counts reconcile exactly.
    """
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.providers import exchange_row_problems

    raw = getattr(provider, "exchange_evidence", None)
    if not required:
        return (), []
    if not isinstance(raw, list):
        return (), ["routed provider exposed no exchange_evidence list"]
    expected_roles = {
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
    }
    by_role: dict[str, list[dict]] = {}
    problems: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            problems.append("exchange manifest contains a non-object entry")
            continue
        role = str(item.get("role") or "")
        if role in expected_roles:
            by_role.setdefault(role, []).append(dict(item))
    for role in sorted(expected_roles):
        entries = by_role.get(role, [])
        if len(entries) != 1:
            problems.append(f"{role}: expected one exchange, found {len(entries)}")
            continue
        entry = entries[0]
        if entry.get("task_id") != task.task_id:
            problems.append(f"{role}: task_id binding mismatch")
        if entry.get("task_content_hash") != task.content_hash():
            problems.append(f"{role}: task_content_hash binding mismatch")
        for field in ("prompt_sha256", "response_sha256"):
            value = str(entry.get(field) or "")
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                problems.append(f"{role}: invalid {field}")
        agents_document = getattr(provider, "agents_document", None)
        current_manifest = providers_mod.role_behavior_manifest(
            role, agents_config=agents_document
        )
        try:
            entry_schema = int(entry.get("entry_schema") or 0)
        except (TypeError, ValueError):
            entry_schema = 0
        if entry_schema >= providers_mod.SESSION_TRANSCRIPT_ENTRY_SCHEMA:
            from elt_taskgen.review.tools import critic_validators as critic_tools

            if role in critic_tools.CRITIC_VALIDATOR_ROLES:
                current_policy = critic_tools.critic_policy(
                    role,
                    critic_tools.critic_limits(
                        role, agents_config=agents_document
                    ),
                    agents_config=agents_document,
                )
            else:
                current_policy = providers_mod.session_policy_for(
                    role, agents_config=agents_document
                )
            current_tools_sha256 = current_policy.tools_sha256()
            current_policy_sha256 = current_policy.sha256()
        else:
            current_tools_sha256 = sha256_hex(
                canonical_json(current_manifest["tools"])
            )
            current_policy_sha256 = str(current_manifest["policy_sha256"])
        expected_bindings = {
            "behavior_sha256": providers_mod.role_behavior_sha256(
                role, agents_config=agents_document
            ),
            "tools_sha256": current_tools_sha256,
            "policy_sha256": current_policy_sha256,
        }
        for field, expected in expected_bindings.items():
            actual = str(entry.get(field) or "")
            if actual != expected:
                problems.append(
                    f"{role}: {field} does not match current role behavior"
                )
        if str(entry.get("diagnostics_version") or "") != str(
            providers_mod.DIAGNOSTICS_VERSION
        ):
            problems.append(
                f"{role}: diagnostics_version does not match current runtime"
            )
        row = dict(entry)
        model_calls = row.get("model_call_count")
        if "attempt_count" not in row and isinstance(model_calls, int) and not isinstance(model_calls, bool):
            # OQ-19: the alias `attempt_count := model_call_count`, one release.
            row["attempt_count"] = model_calls
        problems.extend(exchange_row_problems(row))
        role_count = sum(1 for finding in findings if finding.role.value == role)
        if entry.get("finding_count") != role_count:
            problems.append(
                f"{role}: transcript finding_count={entry.get('finding_count')!r}, "
                f"parsed={role_count}"
            )
        if entry.get("zero_findings") is not (role_count == 0):
            problems.append(f"{role}: zero_findings outcome is inconsistent")
    manifest = tuple(
        by_role[role][0] for role in sorted(expected_roles) if len(by_role.get(role, [])) == 1
    )
    return manifest, problems


def _write_review_transcript_manifest(
    engine: Engine, task: TaskIR, manifest: tuple[dict, ...], problems: list[str]
) -> None:
    """Persist the same deterministic evidence carried by ReviewPayload."""
    reports = engine.task_dir(task.task_id) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "review_transcript_manifest.json").write_text(
        readable_json(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "roles": list(manifest),
                "integrity_problems": list(problems),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _admission_failure_detail(provider, engine) -> str | None:
    """Back-compatible wrapper: the failure detail only (see `_admission_gate`)."""
    return _admission_gate(provider, getattr(engine, "workspace", None))[0]


def _structural_failure_detail(task: TaskIR) -> str | None:
    """Deterministic structural-completeness precondition on live spend.

    "Every table, key and relationship the rules or the mart reference is published
    in the source schemas" is static analysis, not judgment, so it is certified
    ABOVE the provider: a plan naming an unpublished table is refused before the
    author writes a word. Returns the failure detail naming every missing object
    and its reference site, or None. Routes to SPECIFICATION."""
    from elt_taskgen.verification import structural_completeness

    problems = structural_completeness.check_structural_completeness(task)
    if not problems:
        return None
    return (
        f"{structural_completeness.GATE_NAME}: {len(problems)} referenced "
        "object(s) absent from the published bundle — no live call was made; "
        + "; ".join(problems)
    )


def _attribution_failure_detail(engine: Engine, task: TaskIR) -> str | None:
    """Deterministic column-lineage precondition on live spend.

    The author states where each passthrough column comes from; the frozen
    reference is what actually produces gold. When they disagree, a solver that
    believes the prose cannot reproduce gold — which the dual-build gate would only
    discover one council run later. Returns the failure detail, or None, including
    when either side says nothing, which is NOT a certification. SPECIFICATION."""
    from elt_taskgen.verification import attribution

    workspace = getattr(engine, "workspace", None)
    if workspace is None:
        return None
    return attribution.check_attribution(task, workspace)


#: sha256 of the prose an `author` PASS produced — THE ONLY RECORD of what
#: authoring last wrote, and the sole basis for telling "this prose came from me"
#: from "this prose was repaired since I last ran".
AUTHOR_PROSE_SHA_KEY = "prose_sha256"
AUTHOR_BEHAVIOR_SHA_KEY = "behavior_sha256"

#: Payload key naming WHERE that prose came from. `AUTHOR_SOURCE_PRESERVED` means
#: the stage kept prose a certified repair patch committed, and it must be STICKY:
#: otherwise a later re-attestation matches its own recorded sha, re-authors from
#: the unchanged view and quietly undoes the repair one stage later.
AUTHOR_SOURCE_KEY = "source"
AUTHOR_SOURCE_PRESERVED = "repair_patch_preserved"

#: A draft the author submitted but the deterministic prose-fidelity gate
#: rejected.  It is still the exact artifact a SPECIFICATION repair must edit;
#: dropping it leaves the proposer staring at an empty ``solver_prompt`` and
#: makes every otherwise-repairable author failure an inert round.
AUTHOR_SOURCE_CANDIDATE = "authored_candidate"

#: Provenance keys for bounded author revisions. One-shot rows omit them and
#: remain byte-identical when the session is disabled.
AUTHOR_SOURCE_REVISED = "authored_revised"
AUTHOR_REVISIONS_KEY = "revisions"
AUTHOR_DRAFTS_KEY = "drafts"
AUTHOR_SESSION_TERMINAL_KEY = "session_terminal"
AUTHOR_SESSION_SHA_KEY = "session_sha256"
AUTHOR_SESSION_REPORTS_REL = Path("reports") / "sessions"


def _author_session_block(provider) -> dict | None:
    """The author's declared `session:` block, read from the agents document
    THIS provider's routing was loaded from, or None unless the bounded
    revision is enabled — `enabled` AND `max_revisions > 0`
    (`validators.author_session_enabled`; either key alone is the rollback)
    — and the provider can run a session. Under the shipped config this is
    None and the stage calls `council.author_prose` byte for byte."""
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.tools import validators as author_tools

    if not callable(getattr(provider, "run_session", None)):
        return None  # one-shot providers and lightweight doubles stay one-shot
    try:
        block = providers_mod.role_loop_limits(
            "semantic_author", agents_config=getattr(provider, "agents_config", None)
        )
    except Exception:  # noqa: BLE001 - an unreadable declaration enables nothing
        return None
    return dict(block) if author_tools.author_session_enabled(block) else None


def _persist_author_session(workspace: Path | None, task: TaskIR, record: dict) -> None:
    """Write the session record under `tasks/<id>/reports/sessions/` (the
    stage runner's job per `RoutedProvider.run_session`; `reports/` is outside
    the repair snapshot). The transcript entries and the evidence row are the
    records of authority, so a write failure loses a convenience copy only."""
    result = record.get("result")
    if workspace is None or result is None:
        return
    as_dict = getattr(result, "as_dict", None)
    precheck = record.get("precheck")
    document = {
        "role": "semantic_author",
        "session": as_dict() if callable(as_dict) else None,
        "revisions": int(record.get("revisions", 0) or 0),
        "drafts": int(record.get("drafts", 0) or 0),
        "terminal": str(record.get("terminal", "") or ""),
        "contamination_precheck": (
            precheck.model_dump(mode="json") if hasattr(precheck, "model_dump") else None
        ),
    }
    digest = str(getattr(result, "session_sha256", "") or "")[:12] or "session"
    target = Path(workspace) / "tasks" / task.task_id / AUTHOR_SESSION_REPORTS_REL
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / f"semantic_author.{digest}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _development_snapshot_for(engine: Engine, task: TaskIR) -> str:
    """Example DEVELOPMENT rows for the author's view, or '' when unavailable.

    Two tasks of one library shape were described in the same sentences while
    the author saw no data; those repeats then queued as borderline collisions
    between our own tasks (batch50, 2026-09-19). Duck-typed stand-ins without a
    task directory, and stages that run before `generate`, simply get no rows.
    """
    from elt_taskgen.generation import dataset_context

    task_dir = getattr(engine, "task_dir", None)
    if not callable(task_dir):
        return ""
    try:
        return dataset_context.development_snapshot(
            task, Path(task_dir(task.task_id)) / "populations"
        )
    except OSError:
        return ""


def _author_prose_for(engine: Engine, task: TaskIR, provider) -> tuple[str, dict[str, str]]:
    """The author's prose and the ledger data its provenance adds: the
    unchanged `council.author_prose` (no data) unless the bounded revision
    session is enabled, then `council.author_prose_session` under the author
    tools and policy (`validators.author_policy`), the workspace's
    contamination index armed for the final-draft precheck, and the session
    record persisted beside the task's reports."""
    from elt_taskgen.review import council
    from elt_taskgen.review.tools import validators as author_tools

    snapshot = _development_snapshot_for(engine, task)
    block = _author_session_block(provider)
    if block is None:
        return council.author_prose(task, provider, dataset_snapshot=snapshot), {}
# Salt reruns from the current ledger count so each gets a distinct transcript.
    counter = getattr(engine, "session_limit_reruns", None)
    salt = int(counter(task.task_id, "author")) if callable(counter) else 0
    policy = author_tools.author_policy(author_tools.author_limits(block), session_salt=salt)
    workspace = getattr(engine, "workspace", None)
    index = None
    if workspace is not None:
        index = _pipeline_generation._contamination_index(engine)
        _pipeline_generation._arm_contamination_index(index, announce=False)
    record: dict = {}
    try:
        prose = council.author_prose_session(
            task,
            provider,
            tools=policy.tools,
            policy=policy,
            contamination_index=index,
            coverage_requirement=_pipeline_generation._required_firewall_coverage(),
            record=record,
            dataset_snapshot=snapshot,
        )
    except council.AuthorSessionLimitStop as exc:
# Persist capped sessions and return a salted BLOCKED result without a repair round.
        _persist_author_session(workspace, task, exc.record)
        raise
    _persist_author_session(workspace, task, record)
    data = {
        AUTHOR_SOURCE_KEY: AUTHOR_SOURCE_REVISED,
        AUTHOR_REVISIONS_KEY: str(int(record.get("revisions", 0) or 0)),
        AUTHOR_DRAFTS_KEY: str(int(record.get("drafts", 0) or 0)),
        AUTHOR_SESSION_TERMINAL_KEY: str(record.get("terminal", "") or ""),
    }
    digest = str(getattr(record.get("result"), "session_sha256", "") or "")
    if digest:
        data[AUTHOR_SESSION_SHA_KEY] = digest
    return prose, data


def _author_answered_current_state(engine: Engine, task_id: str) -> bool:
    """True iff the LATEST `author` ledger row is a pass.

    After `repair.apply_repair` the latest row is the invalidation FAIL it appended,
    so False means "the outstanding repair has not been answered yet". No ledger, no
    proof anything answered anything — fail closed to False."""
    latest = getattr(engine, "latest_report", None)
    if latest is None:
        return False
    row = latest(task_id, "author")
    return row is not None and row.verdict == VERDICT_PASS


def _last_author_prose_record(engine: Engine, task_id: str) -> tuple[str | None, str]:
    """Return the latest authored prose digest and source, excluding admin rows."""
    latest = getattr(engine, "latest_report", None)
    by_verdict = getattr(engine, "latest_report_with_verdict", None)
    if latest is None or by_verdict is None:
        return None, ""

    history = getattr(engine, "report_history", None)
    if callable(history):
        rows = list(history(task_id, "author"))
    else:
        # Older engines/doubles have no history reader.  Inspect the latest row
        # first (it may itself be an authored candidate), then the most recent
        # PASS as the compatibility fallback.  De-duplication is by object id
        # because minimal doubles need not carry a ledger ``id`` attribute.
        rows = []
        current = latest(task_id, "author")
        passing = by_verdict(task_id, "author", VERDICT_PASS)
        for row in (current, passing):
            if row is not None and all(row is not seen for seen in rows):
                rows.append(row)

    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        value = data.get(AUTHOR_PROSE_SHA_KEY)
        source = data.get(AUTHOR_SOURCE_KEY)
        source = str(source) if isinstance(source, str) else ""
        if not isinstance(value, str) or not value:
            continue
        # A FAIL is authored provenance only when the author runner explicitly
        # labels it.  This excludes repair invalidations, proposer adjudication
        # rows and arbitrary stage failures even if a future payload happens to
        # grow a similarly named digest.  PASS rows predate ``source`` and keep
        # their compatibility semantics.
        if row.verdict == VERDICT_FAIL and source not in {
            AUTHOR_SOURCE_CANDIDATE,
            AUTHOR_SOURCE_REVISED,
        }:
            continue
        if row.verdict not in (VERDICT_PASS, VERDICT_FAIL):
            continue
        return value, source
    return None, ""


def make_author_runner(provider):
    """Build semantic prose, then require deterministic fidelity to the TaskIR.

    Preserve repaired prose. Reject unchanged prose after an unresolved
    specification repair.
    """

    def run_author(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.review import council, prose_fidelity
        from elt_taskgen.review.model_specification import author_if_configured
        from elt_taskgen.review import providers as providers_mod

        author_behavior = {
            AUTHOR_BEHAVIOR_SHA_KEY: providers_mod.role_behavior_sha256(
                "semantic_author",
                agents_config=getattr(provider, "agents_document", None),
            )
        }

        # FIRST, before the provider is consulted at all: a task referencing a
        # table, column, key or relationship the published bundle does not contain
        # cannot be authored into a solvable spec, and code certifies that.
        incomplete = _structural_failure_detail(task)
        if incomplete is not None:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error=incomplete),
                route=RepairRoute.SPECIFICATION,
            )

        # Verify required mutants before provider spend.
        unreproducible = _pipeline_generation._attack_matrix_failure_detail(engine, task)
        if unreproducible is not None:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error=unreproducible),
                route=RepairRoute.POPULATION,
            )

        # Same admission gate as the review stage: no live authoring spend
        # before the council is proven effective (replay-only is exempt).
        not_admitted, admission = _admission_gate(
            provider, getattr(engine, "workspace", None)
        )
        if not_admitted is not None:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error=not_admitted, infrastructure="not_admitted"),
            )
        _stamp_admission(provider, admission)
        _begin_task_evidence(provider, task)

        stored = task.solver_prompt or ""
        last_sha, last_source = _last_author_prose_record(engine, task.task_id)
        stored_sha = hashlib.sha256(stored.encode("utf-8")).hexdigest()
        # (A) The prose moved since authoring last wrote it => a certified
        # SPECIFICATION patch committed. Do not call the provider: the only
        # completion it can return is the one the patch replaced. And once
        # preserved, STAY preserved while the bytes are unchanged.
        repaired_externally = (
            bool(stored)
            and last_sha is not None
            and (last_sha != stored_sha or last_source == AUTHOR_SOURCE_PRESERVED)
        )
        if repaired_externally:
            problems = prose_fidelity.check_prose_fidelity(task)
            if problems:
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(
                        error=(
                            f"prose fidelity: {len(problems)} MartSpec item(s) "
                            "not represented in the REPAIRED prose — "
                            + "; ".join(problems)
                        ),
                        data={"gate": prose_fidelity.GATE_NAME, **author_behavior},
                    ),
                    route=RepairRoute.SPECIFICATION,
                )
            try:
                public_task = author_if_configured(task, provider)
            except ValueError as exc:
                return StageOutcome(VERDICT_FAIL, StagePayload(error=str(exc)),
                                    route=RepairRoute.SPECIFICATION)
            return StageOutcome(
                VERDICT_PASS,
                StagePayload(
                    detail=(
                        f"repaired solver prose PRESERVED ({len(stored)} "
                        f"chars, sha {stored_sha[:12]}): "
                        + (
                            "still the repaired text this stage preserved "
                            "before"
                            if last_sha == stored_sha
                            else "it was patched since this stage last "
                            f"authored (recorded {last_sha[:12]})"
                        )
                        + "; re-authoring would revert it, because the author "
                        "view does not contain solver_prompt. Fidelity gate "
                        "green"
                    ),
                    data={
                        AUTHOR_PROSE_SHA_KEY: stored_sha,
                        AUTHOR_SOURCE_KEY: AUTHOR_SOURCE_PRESERVED,
                        **author_behavior,
                        **admission,
                    },
                ),
                task=public_task if public_task != task else None,
            )

        # Use bounded revisions only when enabled; otherwise keep one-shot rows
        # unchanged. A cap with no draft blocks the seat without starting a round.
        try:
            prose, session_data = _author_prose_for(engine, task, provider)
        except council.AuthorSessionLimitStop as exc:
            return council.author_session_limit_outcome(exc, engine, task)
        updated = task.model_copy(update={"solver_prompt": prose})

        prose_sha = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        problems = prose_fidelity.check_prose_fidelity(updated)
        if problems:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(
                    error=(
                        f"prose fidelity: {len(problems)} MartSpec item(s) "
                        "not represented in the authored prose — "
                        + "; ".join(problems)
                    ),
                    data={
                        "gate": prose_fidelity.GATE_NAME,
                        AUTHOR_PROSE_SHA_KEY: prose_sha,
                        AUTHOR_SOURCE_KEY: AUTHOR_SOURCE_CANDIDATE,
                        **author_behavior,
                        # A bounded-session candidate retains the more precise
                        # ``authored_revised`` source and its trajectory fields;
                        # the one-shot rollback path uses authored_candidate.
                        **session_data,
                    },
                ),
                route=RepairRoute.SPECIFICATION,
                # A red draft is not accepted prose, but it is the exact
                # specification artifact the repair route must see and edit.
                # Engine persists an outcome task before invoking the proposer,
                # binding this candidate and its provenance row to one hash.
                task=updated,
            )

        try:
            updated = author_if_configured(updated, provider)
        except ValueError as exc:
            return StageOutcome(VERDICT_FAIL, StagePayload(error=str(exc)),
                                route=RepairRoute.SPECIFICATION, task=updated)

        if prose == stored:
            # (B) Unchanged prose is only honest OUTSIDE an UNANSWERED repair;
            # `_author_answered_current_state` keeps an ordinary re-attestation from
            # being read as the repair's first, empty answer.
            spec_repair = _pending_specification_repair(task)
            if spec_repair is not None and not _author_answered_current_state(
                engine, task.task_id
            ):
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(
                        error=(
                            "INERT SPECIFICATION REPAIR: revision "
                            f"{spec_repair.revision} was a specification repair "
                            f"({spec_repair.reason!r}) whose whole purpose is "
                            "new solver prose, and re-authoring produced "
                            f"BYTE-IDENTICAL prose (sha {prose_sha[:12]}). "
                            "Nothing patched task_ir.json, and the author view "
                            "is a pure function of an IR the repair did not "
                            "touch, so the rendered view — and therefore the "
                            "transcript key and the provider's answer — could "
                            "not move. Failing on this round rather than "
                            "recording 'solver prose unchanged' as a PASS and "
                            "handing the same prose back to the same council: "
                            "the defect is in the repair path, not the prose."
                        ),
                        data={
                            AUTHOR_PROSE_SHA_KEY: prose_sha,
                            "repair_route": spec_repair.route.value,
                            "repair_revision": str(spec_repair.revision),
                            **author_behavior,
                        },
                    ),
                    # EXPLICIT route, never keyword-guessed ("rendered view" would
                    # read as RUNTIME evidence). The engine's inert-repair rule then
                    # rejects on the unchanged fingerprint — ONE round, not three.
                    route=RepairRoute.SPECIFICATION,
                )
            return StageOutcome(
                VERDICT_PASS,
                StagePayload(
                    detail="solver prose unchanged; fidelity gate green",
                    data={
                        AUTHOR_PROSE_SHA_KEY: prose_sha,
                        **author_behavior,
                        **admission,
                        **session_data,
                    },
                ),
                task=updated if updated != task else None,
            )
        return StageOutcome(
            VERDICT_PASS,
            StagePayload(
                detail=f"solver prose authored ({len(prose)} chars); fidelity "
                "gate green; content hash moves, all stages re-attest",
                data={
                    AUTHOR_PROSE_SHA_KEY: prose_sha,
                    **author_behavior,
                    **admission,
                    **session_data,
                },
            ),
            task=updated,
        )

    return run_author


def _pending_specification_repair(task: TaskIR):
    """The task's current revision when it is a SPECIFICATION repair, else None.

    Read off `TaskIR.revisions`, so it survives a crash, a resume and a trial copy
    with no ledger query. Revision 1 is the intake anchor, never a repair."""
    if not task.revisions:
        return None
    entry = task.revisions[-1]
    return entry if entry.route is RepairRoute.SPECIFICATION else None


def make_review_runner(provider):
    """`review`: council review. Findings only; the council can block (fatal
    findings fail the stage) but has no acceptance vocabulary."""

    def run_review(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review import council

        # BEFORE THE FOUR CRITICS ARE PAID: the structural half of the feasibility
        # question is certified by code, not by a seat — a plan naming an object the
        # published schemas lack is refused here, with the reference site named.
        incomplete = _structural_failure_detail(task)
        if incomplete is not None:
            return StageOutcome(
                VERDICT_FAIL,
                _pipeline_payloads.ReviewPayload(findings=(), fatal_count=0, detail=incomplete),
                route=RepairRoute.SPECIFICATION,
            )

        # Independently verify required mutants before paying critics.
        unreproducible = _pipeline_generation._attack_matrix_failure_detail(engine, task)
        if unreproducible is not None:
            return StageOutcome(
                VERDICT_FAIL,
                _pipeline_payloads.ReviewPayload(findings=(), fatal_count=0, detail=unreproducible),
                route=RepairRoute.POPULATION,
            )

        # Reject prose attribution that contradicts the frozen reference before spend.
        misattributed = _attribution_failure_detail(engine, task)
        if misattributed is not None:
            return StageOutcome(
                VERDICT_FAIL,
                _pipeline_payloads.ReviewPayload(findings=(), fatal_count=0, detail=misattributed),
                route=RepairRoute.SPECIFICATION,
            )

        not_admitted, admission = _admission_gate(
            provider, getattr(engine, "workspace", None)
        )
        if not_admitted is not None:
            return StageOutcome(
                VERDICT_FAIL,
                _pipeline_payloads.ReviewPayload(findings=(), fatal_count=0, detail=not_admitted),
                # STRUCTURED, because the detail text must not be keyword-
                # scanned: a council finding is free prose and may say anything.
                infrastructure="not_admitted",
            )
        _stamp_admission(provider, admission)
        manifest_required = _begin_task_evidence(provider, task)

        try:
            findings = council.run_council(task, provider)
            # The same protocol boundary protects one-shot and harness-validated
            # critics. It runs before a finding can be mistaken for a task defect.
            validated_findings = _pipeline_attacks._validated_executable_findings(task, findings)
        except council.ProviderProtocolError as exc:
            return _pipeline_handoff._critic_protocol_block(
                f"critic protocol failure: {type(exc).__name__}: {exc}"
            )
        fatal = [f for f in findings if f.severity is Severity.FATAL]
        transcript_manifest, manifest_problems = _validated_review_manifest(
            provider, task, findings, required=manifest_required
        )
        if manifest_required:
            _write_review_transcript_manifest(
                engine, task, transcript_manifest, manifest_problems
            )
        payload = _pipeline_payloads.ReviewPayload(
            findings=tuple(findings),
            fatal_count=len(fatal),
            detail=f"{len(findings)} finding(s), {len(fatal)} fatal",
            admission=admission,
            transcript_manifest=transcript_manifest,
        )
        if manifest_problems:
            return _pipeline_handoff._critic_protocol_block(
                payload.detail
                + "; transcript manifest integrity failure: "
                + "; ".join(manifest_problems),
                failure_code=_pipeline_handoff.CRITIC_MANIFEST_FAILURE_CODE,
                recovery_prerequisite=_pipeline_handoff.CRITIC_MANIFEST_RECOVERY,
            )
        # The screened list: a voided finding (withdrawn by its provider's
        # disposition, or a proposal against a rule no mart declares) is INFO
        # and holds nothing.
        pending_adjudication = _pipeline_handoff._critic_adjudication_block(validated_findings)
        if pending_adjudication is not None:
            return pending_adjudication
        if fatal:
            # A prose leak is repairable by rewriting the prose (specification
            # route); the bounded repair budget rejects a non-converging task.
            return StageOutcome(
                VERDICT_FAIL, payload, route=RepairRoute.SPECIFICATION
            )
        # Diligence requires a validated executable probe; prose and bare
        # suggestions are not evidence. Use the shared predicate everywhere.
        shortcut = [
            finding
            for finding in validated_findings
            if finding.role is CouncilRole.SHORTCUT_ATTACKER
        ]
        probes = [finding for finding in shortcut if council.is_executable_probe(finding)]
        if not probes:
            diligence_detail = (
                "diligence protocol failure: shortcut_attacker returned "
                f"{len(shortcut)} finding(s) and zero executable probes "
                "(requires severity minor or higher plus a matching validated "
                "suggested_attack/proposed_case) — no evidence the shortcut "
                "surface was examined"
            )
            if _pipeline_attacks._attacker_corrected_in_session(transcript_manifest):
                # A corrected session that still lacks a probe is a repairable
                # seat outcome. Fail on its route so review can rerun fresh.
                return StageOutcome(
                    VERDICT_FAIL,
                    payload.model_copy(
                        update={
                            "detail": (
                                payload.detail + "; " + diligence_detail
                                + "; the attacker session was corrected in-session "
                                "and still handed off no executable probe"
                            )
                        }
                    ),
                    route=RepairRoute.SPECIFICATION,
                )
            return _pipeline_handoff._critic_protocol_block(
                diligence_detail,
                failure_code=_pipeline_handoff.CRITIC_DILIGENCE_FAILURE_CODE,
                recovery_prerequisite=_pipeline_handoff.CRITIC_DILIGENCE_RECOVERY,
            )
        return StageOutcome(VERDICT_PASS, payload)

    return run_review

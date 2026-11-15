"""Execute proposed attacks and preserve measured mutation fidelity."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from elt_taskgen.engine import (
    Engine,
    StageName,
    StageOutcome,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    AttackKind,
    CouncilRole,
    Finding,
    FindingScreenStatus,
    PopulationName,
    RepairRoute,
    RLVR_TASK_VARIANTS,
    Severity,
    TaskIR,
    TaskVariant,
)
from elt_taskgen.pipeline import handoff as _pipeline_handoff
from elt_taskgen.pipeline import payloads as _pipeline_payloads
from elt_taskgen.pipeline import support as _pipeline_support


def _attacker_corrected_in_session(transcript_manifest) -> bool:
    """Whether the shortcut attacker's exchange row is a harness-validated
    SESSION row (entry schema 3) on which the seat was corrected: a compile
    correction was issued (`correction_kinds.compile`), or the accepted
    payload was still red at submit and voided post-session
    (`submitted_with_red_validators`).  A one-shot row, a duck-typed
    provider without evidence, or a session that was never corrected says
    False — the diligence failure then stays the protocol block it was."""
    for row in tuple(transcript_manifest or ()):
        if not isinstance(row, dict) or str(row.get("role") or "") != "shortcut_attacker":
            continue
        try:
            if int(row.get("entry_schema") or 0) < 3:
                return False
            kinds = row.get("correction_kinds")
            compile_corrections = (
                int(kinds.get("compile") or 0) if isinstance(kinds, dict) else 0
            )
            red_at_submit = int(row.get("submitted_with_red_validators") or 0)
        except (TypeError, ValueError):
            return False
        return compile_corrections > 0 or red_at_submit > 0
    return False


def _recorded_variant_rewards(
    engine: Engine, task: TaskIR, rewards: dict[str, dict[str, float]]
) -> dict[str, dict[str, dict[str, float]]]:
    """variant -> case -> population -> reward, read back from attacks/*/rewards.json.

    Reading back (never re-scoring) keeps the composite diagnostic and both unit
    rewards on ONE execution. A record whose `task_content_hash` is not the current
    one is DROPPED, so the batteries see MISSING evidence rather than stale."""
    current = task.content_hash()
    out: dict[str, dict[str, dict[str, float]]] = {}
    attacks_root = engine.task_dir(task.task_id) / "attacks"
    for case_name in sorted(rewards):
        path = attacks_root / case_name / "rewards.json"
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if record.get("task_content_hash") != current:
            continue
        by_variant = record.get("rewards_by_variant")
        if not isinstance(by_variant, dict):
            continue
        # Transpose to the shape the batteries consume: they ask for ONE
        # variant's whole attack matrix, so the variant is the outer key.
        for variant_value, per_population in by_variant.items():
            if isinstance(per_population, dict):
                out.setdefault(str(variant_value), {})[case_name] = {
                    str(pop): float(value)
                    for pop, value in per_population.items()
                }
    return out


_ATTACK_FINDING_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,127}")
_MAX_ATTACK_FINDING_IDENTIFIERS = 16


def _confirmed_live_exploit(finding: Finding, outcomes_by_id: dict[str, object]) -> bool:
    """Whether measurement proved this finding's wrong program passes everywhere."""
    outcome = outcomes_by_id.get(finding.finding_id)
    return bool(
        outcome is not None
        and not outcome.promoted
        and outcome.measured_pass
        and all(outcome.measured_pass.values())
    )


def _selected_blocking_finding(findings: list[Finding], outcomes=()) -> Finding | None:
    """Select exactly one repair subject without consulting model-authored prose.

    A measured live exploit has priority because it is the strongest artifact
    and retains the historical POPULATION rule.  Otherwise the finding id that
    the harness assigned is the stable order; role and severity are tie-breakers
    only for malformed hand-built inputs with duplicate ids.
    """
    by_id = {outcome.finding_id: outcome for outcome in outcomes}
    ordered = sorted(
        findings,
        key=lambda finding: (
            finding.finding_id,
            finding.role.value,
            finding.severity.value,
        ),
    )
    measured = [
        finding for finding in ordered if _confirmed_live_exploit(finding, by_id)
    ]
    if measured:
        return measured[0]
    return ordered[0] if ordered else None


def _attack_finding_descriptor(task: TaskIR, finding: Finding) -> _pipeline_payloads.AttackFindingDescriptor:
    """Project one finding to role/severity plus public identifiers in its text.

    Token extraction sees only ``summary`` and ``detail``.  Exact membership in
    ``PublicIdentifierSet`` removes hidden population names, literal values,
    paths and arbitrary prose.  Sorting/deduplication and the fixed cap make the
    projection canonical and keep the repair evidence bounded.
    """
    from elt_taskgen.review.tools.projection import PublicIdentifierSet

    public = PublicIdentifierSet(task)
    text = f"{finding.summary}\n{finding.detail}"
    identifiers = tuple(
        sorted(
            {
                token
                for token in _ATTACK_FINDING_TOKEN_RE.findall(text)
                if token in public
            }
        )[:_MAX_ATTACK_FINDING_IDENTIFIERS]
    )
    return _pipeline_payloads.AttackFindingDescriptor(
        role=finding.role,
        severity=finding.severity,
        identifiers=identifiers,
    )


def _proposal_failure_route(findings: list[Finding], outcomes=()) -> RepairRoute:
    """Route blocking handoff failures from trusted role and promoter evidence."""
    outcomes = tuple(outcomes)
    selected = _selected_blocking_finding(findings, outcomes)
    if selected is None:
        return RepairRoute.SPECIFICATION
    by_id = {outcome.finding_id: outcome for outcome in outcomes}
    if _confirmed_live_exploit(selected, by_id):
        return RepairRoute.POPULATION
    if selected.role is CouncilRole.POPULATION_ADVERSARY:
        return RepairRoute.POPULATION
    return RepairRoute.SPECIFICATION


_SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF = (
    "source_distinct_over_declared_primary_key_v1"
)
#: ``zero_is_missing`` is an identity when every touched default is zero, so it
#: cannot be treated as a live exploit for that task.
_SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF = "zero_is_missing_over_zero_defaults_v1"


def _zero_default_equivalence_proof(task: TaskIR, finding: Finding, outcome, attacks_mod) -> dict[str, object] | None:
    """Prove every realized ``zero_is_missing`` rewrite is a zero-default identity."""
    import sqlglot
    from sqlglot import exp

    proposal = finding.proposed_case
    if not _names_zero_is_missing(proposal):
        return None
    if not _measured_all_true_with_fidelity(outcome):
        return None
    compiler = outcome.fidelity.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("passed") is not True:
        return None
    targets = _zero_is_missing_realized_targets(compiler.get("realized"))
    if targets is None:
        return None
    marts = {mart.name: mart for mart in task.marts}
    sites = 0
    for name in targets:
        mart = marts.get(str(name))
        gold_sql = dict(task.reference.sql_by_mart).get(str(name))
        if mart is None or not gold_sql:
            return None
        try:
            mutated = attacks_mod._apply_kind(
                AttackKind.NO_NULL_DEFAULT, gold_sql, mart, "zero_is_missing"
            )
        except Exception:  # noqa: BLE001 - a compiler error is not a proof
            return None
        if not mutated:
            return None
        try:
            ast = sqlglot.parse_one(mutated, read="duckdb")
            gold_ast = sqlglot.parse_one(gold_sql, read="duckdb")
        except Exception:  # noqa: BLE001
            return None
        # The gold's OWN NULLIFs (a divisor guard such as `x / NULLIF(n, 0)`)
        # are not sites the variant introduced; they must survive unchanged.
        gold_nullifs = [node.sql(dialect="duckdb") for node in gold_ast.find_all(exp.Nullif)]
        for nullif in ast.find_all(exp.Nullif):
            parent = nullif.parent
            zero = nullif.expression
            if not (isinstance(parent, exp.Coalesce) and parent.this is nullif):
                if nullif.sql(dialect="duckdb") in gold_nullifs:
                    gold_nullifs.remove(nullif.sql(dialect="duckdb"))
                    continue
                return None
            if not (
                isinstance(zero, exp.Literal)
                and not zero.is_string
                and float(zero.name) == 0.0
            ):
                return None
            defaults = list(parent.expressions)
            if len(defaults) != 1:
                return None
            default = defaults[0]
            if not (isinstance(default, exp.Literal) and not default.is_string):
                return None
            try:
                if float(default.name) != 0.0:
                    return None
            except ValueError:
                return None
            sites += 1
        if gold_nullifs:
            # A gold NULLIF the mutant no longer carries: not this variant.
            return None
    if sites == 0:
        return None
    return {
        "proved": True,
        "proof": _SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF,
        "operation": "zero_is_missing",
        "form": (
            "variant"
            if "variant" in dict(proposal.params)
            else "operation_key"
        ),
        "marts": [str(name) for name in targets],
        "sites": sites,
    }


def _names_zero_is_missing(proposal) -> bool:
    """Recognize either schema spelling of only the ``zero_is_missing`` rewrite."""
    if proposal is None:
        return False
    params = dict(proposal.params)
    if params.get("zero_is_missing") is True:
        # The compiler admits the operation key under `no_null_default` or
        # `custom` and materializes both through the same rewrite.
        if proposal.kind not in (AttackKind.NO_NULL_DEFAULT, AttackKind.CUSTOM):
            return False
        return not (set(params) - {"zero_is_missing"})
    if params.get("variant") == "zero_is_missing":
        if proposal.kind is not AttackKind.NO_NULL_DEFAULT:
            return False
        return not (set(params) - {"variant"})
    return False


def _zero_is_missing_realized_targets(realized) -> list[str] | None:
    """The marts the compiler's fidelity record says the `zero_is_missing`
    rewrite changed, read from either record shape the compiler emits for
    it: `{"operation": "zero_is_missing", "marts": [...]}` for the
    operation-key proposal and `{"operation": "kind", "kind":
    "no_null_default", "variant": "zero_is_missing", "target_marts": [...]}`
    for the variant proposal. None when the record is not this rewrite or
    names no mart."""
    if not isinstance(realized, dict):
        return None
    if realized.get("operation") == "zero_is_missing":
        targets = realized.get("marts")
    elif (
        realized.get("operation") == "kind"
        and realized.get("kind") == AttackKind.NO_NULL_DEFAULT.value
        and realized.get("variant") == "zero_is_missing"
    ):
        targets = realized.get("target_marts")
    else:
        return None
    if not isinstance(targets, list) or not targets:
        return None
    if not all(isinstance(target, str) and target for target in targets):
        return None
    return [str(target) for target in targets]


#: Dropping the frame is an identity for MAX/DESC or MIN/ASC over the same
#: column, so those cases are not live exploits.
_SCHEMA_EQUIVALENT_DROP_FRAME_PROOF = "drop_frame_over_self_ordered_extremes_v1"


def _names_drop_frame(proposal) -> bool:
    if proposal is None or proposal.kind is not AttackKind.WRONG_WINDOW:
        return False
    params = dict(proposal.params)
    if str(params.get("variant", "") or "") != "drop_frame":
        return False
    return not (set(params) - {"variant"})


def _drop_frame_equivalence_proof(task: TaskIR, finding: Finding, outcome, attacks_mod) -> dict[str, object] | None:
    """Prove `drop_frame` is an identity on this task's gold: every framed
    window the variant touches is MAX(c) ordered by c DESC or MIN(c) ordered
    by c ASC, with that one ordering key. Anything else fails closed."""
    import sqlglot
    from sqlglot import exp

    if not _names_drop_frame(finding.proposed_case):
        return None
    if not _measured_all_true_with_fidelity(outcome):
        return None
    compiler = outcome.fidelity.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("passed") is not True:
        return None
    realized = compiler.get("realized")
    if not isinstance(realized, dict) or realized.get("variant") != "drop_frame":
        return None
    targets = realized.get("target_marts")
    if not isinstance(targets, list) or not targets:
        return None
    sites = 0
    for name in targets:
        gold_sql = dict(task.reference.sql_by_mart).get(str(name))
        if not gold_sql:
            return None
        try:
            ast = sqlglot.parse_one(gold_sql, read="duckdb")
        except Exception:  # noqa: BLE001
            return None
        for window in ast.find_all(exp.Window):
            if window.args.get("spec") is None:
                continue
            func = window.this
            if not isinstance(func, (exp.Max, exp.Min)):
                return None
            column = func.this
            if not isinstance(column, exp.Column):
                return None
            order = window.args.get("order")
            keys = list(order.expressions) if order is not None else []
            if len(keys) != 1 or not isinstance(keys[0], exp.Ordered):
                return None
            key = keys[0]
            if not (isinstance(key.this, exp.Column) and key.this.name == column.name):
                return None
            descending = bool(key.args.get("desc"))
            if isinstance(func, exp.Max) and not descending:
                return None
            if isinstance(func, exp.Min) and descending:
                return None
            sites += 1
    if sites == 0:
        return None
    return {
        "proved": True,
        "proof": _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF,
        "operation": "drop_frame",
        "marts": [str(name) for name in targets],
        "sites": sites,
    }


def _drop_frame_identity_accepted(proposal, outcome) -> bool:
    """The read-side check of `_drop_frame_equivalence_proof`'s record."""
    if not _names_drop_frame(proposal):
        return False
    proof = outcome.fidelity.get("schema_equivalence")
    if not isinstance(proof, dict):
        return False
    if (
        proof.get("proved") is not True
        or proof.get("proof") != _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF
        or proof.get("operation") != "drop_frame"
    ):
        return False
    sites = proof.get("sites")
    marts = proof.get("marts")
    if not isinstance(sites, int) or sites < 1:
        return False
    if not isinstance(marts, list) or not marts or not all(isinstance(m, str) and m for m in marts):
        return False
    return _measured_all_true_with_fidelity(outcome)


def _zero_default_identity_accepted(proposal, outcome) -> bool:
    """The read-side check of `_zero_default_equivalence_proof`'s record."""
    if not _names_zero_is_missing(proposal):
        return False
    proof = outcome.fidelity.get("schema_equivalence")
    if not isinstance(proof, dict):
        return False
    if (
        proof.get("proved") is not True
        or proof.get("proof") != _SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF
        or proof.get("operation") != "zero_is_missing"
    ):
        return False
    sites = proof.get("sites")
    marts = proof.get("marts")
    if not isinstance(sites, int) or sites < 1:
        return False
    if not isinstance(marts, list) or not marts or not all(isinstance(m, str) and m for m in marts):
        return False
    return _measured_all_true_with_fidelity(outcome)


def _validated_executable_findings(
    task: TaskIR, findings: list[Finding]
) -> list[Finding]:
    """Return compiler-valid executable findings or a protocol failure.

    Malformed executable content blocks as provider protocol. Missing major or
    fatal proposals remain unresolved claims for bounded repair; bare prose
    observations remain reviewable.
    """
    from elt_taskgen.review.council import ProviderProtocolError
    from elt_taskgen.verification import attacks as attacks_mod

    # Preserve the provider's exact kind and parameters.  The closed compiler
    # either accepts that handoff as written or reports a protocol failure; it
    # never rewrites a kind merely to reach a compatible implementation.
    from elt_taskgen.review.council import _void

    executable = list(findings)
    problems: list[str] = []
    # Malformed output after an available compile correction is a protocol
    # failure. One-shot output is voided while unresolved major claims still repair.
    voided: dict[int, Finding] = {}

    def _refuse(index: int, finding: Finding, detail: str) -> None:
        if _pipeline_handoff._seat_has_compile_channel(finding.role):
            problems.append(detail)
            return
        voided[index] = _void(
            finding,
            [_pipeline_handoff.UNCOMPILABLE_NO_CHANNEL_SIGNAL],
            "proposal withheld: this seat runs no compile validator, so it "
            "was never told its executable content was malformed",
        )

    for index, finding in enumerate(executable):
        claimed_severity, claimed_attack, claimed_proposal = (
            _pipeline_handoff._claimed_critic_handoff(finding)
        )
        if (
            claimed_severity in {Severity.MAJOR, Severity.FATAL}
            and claimed_proposal is not None
            and _pipeline_handoff._proposal_targets_a_rule_the_task_never_states(task, claimed_proposal)
        ):
            voided[index] = _void(
                finding,
                [_pipeline_handoff.MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL],
                "the proposed mutation removes a rule no mart of this task "
                "declares, so omitting it is not a wrong implementation and "
                "there is no fork; the claim is kept on record and does not block",
            )
            continue
        if (
            claimed_severity in {Severity.MAJOR, Severity.FATAL}
            and claimed_proposal is not None
            and _pipeline_handoff._proposal_names_a_hop_the_task_never_joins(task, claimed_proposal)
        ):
            voided[index] = _void(
                finding,
                [_pipeline_handoff.MUTATION_NAMES_NO_SECOND_HOP_SIGNAL],
                "the proposed mutation flips a second source-table join that no "
                "mart of this task has, so it realizes no wrong implementation; "
                "the claim is kept on record and does not block",
            )
            continue
        if (
            claimed_severity in {Severity.MAJOR, Severity.FATAL}
            and claimed_proposal is None
            and finding.withdrawn
        ):
            # Only the provider's structured disposition withdraws a finding,
            # and only its own. A withdrawal never withholds an executable
            # proposal here: a finding that carries one is measured as usual.
            voided[index] = _void(
                finding,
                [_pipeline_handoff.WITHDRAWN_BY_DISPOSITION_SIGNAL],
                "the providing critic filed this finding with disposition "
                "'withdrawn'; the claim is kept on record and does not block",
            )
            continue
        claimed = finding.model_copy(
            update={
                "severity": claimed_severity,
                "suggested_attack": claimed_attack,
                "proposed_case": claimed_proposal,
                "screen": None,
            }
        )
        if claimed_severity is Severity.INFO:
            if claimed_proposal is not None:
                _refuse(
                    index,
                    finding,
                    f"{finding.finding_id}: informational finding carries a "
                    "proposed_case even though informational findings are not "
                    "executable",
                )
            continue

        proposal = claimed_proposal
        if proposal is None:
            if claimed_attack is None:
                # A bare observation: reviewed as prose, and at MAJOR an
                # unresolved claim the repair route answers.
                continue
            if claimed_severity in {Severity.MAJOR, Severity.FATAL}:
                # An unresolved major claim naming a coarse attack: routed
                # to repair by `_blocking_proposal_failures`, never guessed
                # into a probe and never a protocol block.
                continue
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: actionable {claimed_severity.value} "
                "finding names an attack but has no proposed_case",
            )
            continue
        if set(proposal.expected_pass_by_stage) != set(RLVR_TASK_VARIANTS):
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: proposed_case has no complete exact "
                "extract_load and transform prediction matrix",
            )
            continue
        try:
            _case, fidelity = attacks_mod.validate_proposed_case(
                task, claimed, proposal, required=False
            )
        except ValueError as exc:
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: proposed_case is not executable "
                f"({type(exc).__name__}: {exc})",
            )
            continue
        if not bool(fidelity.get("passed")):
            errors = tuple(str(value) for value in fidelity.get("errors", ()))
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: proposed_case failed claim fidelity"
                + (f" ({'; '.join(errors)})" if errors else ""),
            )
    if problems:
        raise ProviderProtocolError(
            "critic-to-attack protocol failure: " + "; ".join(problems)
        )
    for index, replacement in voided.items():
        executable[index] = replacement
    return executable


def _claim_suffix(finding) -> str:
    """Return value-free critic prose needed to describe a blocking claim."""
    claim = f"{(finding.summary or '').strip()} {(finding.detail or '').strip()}".strip()
    if not claim:
        return ""
    # Apply the repair view's leak screen here so unsafe claim details are
    # removed before they can halt the entire repair.
    from elt_taskgen.review.tools import projection as projection_mod

    # Redact refused value shapes but retain the actionable claim text.
    claim = _redact_claim_shapes(claim, projection_mod)
    detector = getattr(projection_mod, "_detect_value_shapes", None)
    if detector is not None and detector(claim) is not None:
        return ""
    if projection_mod._MEASURED_VALUE_RE.search(claim):  # noqa: SLF001
        return ""
    return f" — the claim to resolve is: {claim}"


_CLAIM_WITHHELD = "[withheld]"


def _redact_claim_shapes(claim: str, projection_mod) -> str:
    """Replace every refused value shape in a critic's claim with a marker.

    A count-vector cell keeps its name and loses its number
    (`child_count=3` -> `child_count=[withheld]`), so the sentence still says
    which column the critic meant; a key tuple and a measured per-population
    value are replaced whole. The caller re-runs the detectors afterwards, so
    anything this misses still costs only the claim, never the repair.
    """
    text = claim
    count_vector = getattr(projection_mod, "_COUNT_VECTOR_RE", None)
    if count_vector is not None:
        text = count_vector.sub(
            lambda m: m.group(0).split("=", 1)[0].rstrip() + "=" + _CLAIM_WITHHELD,
            text,
        )
    key_tuple = getattr(projection_mod, "_KEY_TUPLE_RE", None)
    if key_tuple is not None:
        text = key_tuple.sub(_CLAIM_WITHHELD, text)
    measured = getattr(projection_mod, "_MEASURED_VALUE_RE", None)
    if measured is not None:
        text = measured.sub(_CLAIM_WITHHELD, text)
    return text


def _blocking_proposal_failures(
    findings: list[Finding], outcomes
) -> tuple[list[Finding], list[str]]:
    """Return unresolved major claims and confirmed live exploits.

    Discharge only compiler-faithful proposals with confirmed matrices, plus
    shortcuts killed by every applicable variant on graded populations.
    """
    def schema_equivalent_non_exploit(finding: Finding, outcome) -> bool:
        """Accept only exact identity proofs measured by the harness.

        Fidelity evidence can prove ``DISTINCT`` over primary-keyed rows,
        zero-default, and drop-frame identities. Require complete, all-true
        combined and EL/T measurements with passing fidelity through
        ``_measured_all_true_with_fidelity``. Critic forecasts cannot satisfy
        this exception or excuse other all-pass shortcuts or mismatches.
        """
        proposal = finding.proposed_case
        if _zero_default_identity_accepted(proposal, outcome):
            return True
        if _drop_frame_identity_accepted(proposal, outcome):
            return True
        if _names_add_dedup(proposal) is None:
            return False
        proof = outcome.fidelity.get("schema_equivalence")
        if not isinstance(proof, dict):
            return False
        if (
            proof.get("proved") is not True
            or proof.get("proof") != _SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF
            or proof.get("operation") != "add_dedup"
        ):
            return False
        proof_tables = proof.get("tables")
        if not isinstance(proof_tables, list) or not proof_tables:
            return False
        for table in proof_tables:
            if not isinstance(table, dict) or set(table) != {
                "name",
                "key_kind",
                "columns",
            }:
                return False
            if table["key_kind"] != "primary_key":
                return False
            if not isinstance(table["name"], str) or not table["name"]:
                return False
            columns = table["columns"]
            if (
                not isinstance(columns, list)
                or not columns
                or not all(isinstance(column, str) and column for column in columns)
            ):
                return False

        return _measured_all_true_with_fidelity(outcome)

    outcomes = {outcome.finding_id: outcome for outcome in outcomes}
    blocking_findings: list[Finding] = []
    problems: list[str] = []
    for finding in findings:
        if (
            finding.screen is not None
            and finding.screen.status is FindingScreenStatus.VOID
        ):
            continue
        outcome = outcomes.get(finding.finding_id)
        if outcome is not None and schema_equivalent_non_exploit(finding, outcome):
            continue
        is_blocking = False
        if finding.severity is Severity.MAJOR:
            if finding.proposed_case is None:
                is_blocking = True
                problems.append(
                    # Include the critic's complaint, not just its ID, so the
                    # specification repair has actionable evidence. Revalidate
                    # the public prose before transport to keep it value-free.
                    f"{finding.finding_id}: major finding has no structured "
                    f"proposed_case; it is unresolved{_claim_suffix(finding)}"
                )
            elif outcome is None and finding.role is CouncilRole.AMBIGUITY_CRITIC:
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major ambiguity claim; its executable "
                    "alternative is withheld from promotion (a two-reading fork is "
                    "repaired in the prose, never frozen by a mutant), so the claim "
                    f"is unresolved{_claim_suffix(finding)}"
                )
            elif outcome is None:
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal has no promotion "
                    f"outcome{_claim_suffix(finding)}"
                )
            elif (
                not outcome.promoted
                and not _faithful_shortcut_is_empirically_killed(finding, outcome)
                and not _population_blind_spot_refuted(finding, outcome)
            ):
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal unresolved — "
                    f"{outcome.reason}{_claim_suffix(finding)}"
                )
            elif set(outcome.predicted_by_stage) != {
                TaskVariant.EXTRACT_LOAD.value,
                TaskVariant.TRANSFORM.value,
            }:
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal has no exact EL/T "
                    f"prediction matrix{_claim_suffix(finding)}"
                )
            elif not bool(outcome.fidelity.get("passed")):
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal lacks passing mutation "
                    f"fidelity evidence{_claim_suffix(finding)}"
                )
        # Even a minor critic may discover an exploit that keeps full reward
        # everywhere. It is not safe merely because the severity label was low.
        if (
            outcome is not None
            and not outcome.promoted
            and outcome.measured_pass
            and all(outcome.measured_pass.values())
        ):
            is_blocking = True
            problems.append(
                f"{finding.finding_id}: confirmed attack keeps FULL reward on "
                f"every population{_claim_suffix(finding)}"
            )
        if is_blocking:
            blocking_findings.append(finding)
    return blocking_findings, problems


def _population_blind_spot_refuted(finding: Finding, outcome) -> bool:
    """Return whether trusted complete matrices refute a blind-spot claim."""
    proposal = finding.proposed_case
    if (
        finding.role is not CouncilRole.POPULATION_ADVERSARY
        or proposal is None
        or outcome.promoted is not False
        or outcome.kind != proposal.kind.value
        or outcome.fidelity.get("passed") is not True
    ):
        return False
    populations = {population.value for population in PopulationName}

    def complete_bool_map(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(type(value) is bool for value in values.values())
        )

    by_stage = outcome.measured_pass_by_stage
    transform = (
        by_stage.get(TaskVariant.TRANSFORM.value) if isinstance(by_stage, dict) else None
    )
    if not complete_bool_map(outcome.measured_pass) or not complete_bool_map(transform):
        return False
    hidden = [p.value for p in PopulationName if p is not PopulationName.DEVELOPMENT]
    return any(
        outcome.measured_pass[p] is False and transform[p] is False for p in hidden
    )


def _measured_all_true_with_fidelity(outcome) -> bool:
    """Was one rejected proposal MEASURED all-pass on EL and T, with passing
    fidelity, whatever it predicted? A proved schema identity does not need
    the critic's forecast to be right: the population adversary on
    synsql__educational (batch10 run P, 2026-09-11) predicted that stress
    would catch an added DISTINCT, measured FULL everywhere, and the
    primary-key proof was never consulted because the forecast mismatched."""
    populations = {population.value for population in PopulationName}

    def complete_all_true(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(value is True for value in values.values())
        )

    stages = {variant.value for variant in RLVR_TASK_VARIANTS}
    return (
        outcome.promoted is False
        and bool(outcome.fidelity.get("passed"))
        and complete_all_true(outcome.measured_pass)
        and set(outcome.measured_pass_by_stage) == stages
        and all(complete_all_true(outcome.measured_pass_by_stage[stage]) for stage in stages)
    )


def _complete_all_true_promotion(outcome) -> bool:
    """Did one promoter outcome fully predict and measure all-pass EL and T?"""
    populations = {population.value for population in PopulationName}

    def complete_all_true(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(value is True for value in values.values())
        )

    stages = {variant.value for variant in RLVR_TASK_VARIANTS}
    return (
        outcome.promoted is False
        and not outcome.mismatches
        and bool(outcome.fidelity.get("passed"))
        and complete_all_true(outcome.predicted)
        and complete_all_true(outcome.measured_pass)
        and set(outcome.predicted_by_stage) == stages
        and set(outcome.measured_pass_by_stage) == stages
        and all(
            complete_all_true(outcome.predicted_by_stage[stage])
            and complete_all_true(outcome.measured_pass_by_stage[stage])
            for stage in stages
        )
    )


def _faithful_shortcut_is_empirically_killed(finding: Finding, outcome) -> bool:
    """Return whether complete faithful matrices kill a shortcut in every applicable variant."""
    proposal = finding.proposed_case
    if (
        finding.role is not CouncilRole.SHORTCUT_ATTACKER
        or proposal is None
        or outcome.promoted is not False
        or outcome.kind != proposal.kind.value
        or outcome.fidelity.get("passed") is not True
    ):
        return False

    populations = {population.value for population in PopulationName}
    stages = {variant.value for variant in RLVR_TASK_VARIANTS}

    def complete_bool_map(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(type(value) is bool for value in values.values())
        )

    combined = outcome.measured_pass
    measured_by_stage = outcome.measured_pass_by_stage
    if (
        not complete_bool_map(combined)
        or not isinstance(measured_by_stage, dict)
        or set(measured_by_stage) != stages
        or not all(
            complete_bool_map(measured_by_stage[stage]) for stage in stages
        )
    ):
        return False

    extract_load = measured_by_stage[TaskVariant.EXTRACT_LOAD.value]
    transform = measured_by_stage[TaskVariant.TRANSFORM.value]
    if any(
        combined[population] is not (
            extract_load[population] and transform[population]
        )
        for population in populations
    ):
        return False

    # Import at the seam so the CLI keeps its existing module dependency shape;
    # these are the exact kind sets whose gates consume the same measurements.
    from elt_taskgen.verification import gates as gates_mod

    applicable_stages: list[str] = []
    if proposal.kind in gates_mod.EL_SHORTCUT_KINDS:
        applicable_stages.append(TaskVariant.EXTRACT_LOAD.value)
    if proposal.kind in gates_mod.TRANSFORM_SHORTCUT_KINDS:
        applicable_stages.append(TaskVariant.TRANSFORM.value)
    if not applicable_stages:
        return False

    graded = {population.value for population in gates_mod.GRADED_POPULATIONS}
    return all(
        any(measured_by_stage[stage][population] is False for population in graded)
        for stage in applicable_stages
    )


def _names_add_dedup(proposal) -> str | None:
    """Return whether a proposal uses the operation-key or variant dedup spelling."""
    if proposal is None:
        return None
    params = dict(proposal.params)
    # The critic schema uses ``no_dedup`` for the dedup mutation family even
    # when ``add_dedup=true`` asks the compiler for the inverse operation.
    # ``custom`` is retained for hand-authored/internal proposals.  The exact
    # compiler requested/realized operation checks in the proof remain
    # authoritative.
    if params.get("add_dedup") is True:
        if proposal.kind.value not in {"custom", "no_dedup"}:
            return None
        if set(params) - {"add_dedup", "dedup_table"}:
            return None
        return "operation_key"
    if params.get("variant") == "add_dedup":
        if proposal.kind is not AttackKind.CUSTOM:
            return None
        if set(params) - {"variant"}:
            return None
        return "variant"
    return None


def _source_dedup_equivalence_proof(
    task: TaskIR, finding: Finding, outcome, attacks_mod=None
) -> dict[str, object] | None:
    """Prove deduplication is an identity on scans with declared primary keys."""
    proposal = finding.proposed_case
    form = _names_add_dedup(proposal)
    if form is None:
        return None
    if not _measured_all_true_with_fidelity(outcome):
        return None

    compiler = outcome.fidelity.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("passed") is not True:
        return None
    requested = compiler.get("requested")
    realized = compiler.get("realized")
    if not isinstance(requested, dict) or not isinstance(realized, dict):
        return None
    target: str | None = None
    variant_targets: list[str] = []
    if form == "operation_key":
        if requested.get("operation") != "add_dedup":
            return None
        if realized.get("operation") != "add_dedup":
            return None
        if not isinstance(realized.get("distinct_source_scans"), int) or int(
            realized["distinct_source_scans"]
        ) < 1:
            return None
        target_raw = requested.get("dedup_table")
        target = str(target_raw) if target_raw else None
        if target != (
            str(proposal.params.get("dedup_table"))
            if proposal.params.get("dedup_table")
            else None
        ):
            return None
    else:
        # The kind-directive record names no DISTINCT count and no table; the
        # rewrite is re-derived from the gold below and must match the marts
        # the compiler says it changed.
        for record in (requested, realized):
            if (
                record.get("operation") != "kind"
                or record.get("kind") != AttackKind.CUSTOM.value
                or record.get("variant") != "add_dedup"
            ):
                return None
        raw_targets = realized.get("target_marts")
        if not isinstance(raw_targets, list) or not raw_targets:
            return None
        if not all(isinstance(name, str) and name for name in raw_targets):
            return None
        variant_targets = sorted(str(name) for name in raw_targets)

    import sqlglot
    from sqlglot import exp

    table_specs = {table.name: table for table in task.tables}
    folded_table_names: dict[str, str] = {}
    for name in table_specs:
        folded = name.casefold()
        if folded in folded_table_names:
            return None
        folded_table_names[folded] = name
    affected: set[str] = set()
    for sql in task.reference.sql_by_mart.values():
        try:
            ast = sqlglot.parse_one(sql, read="duckdb")
        except Exception:  # noqa: BLE001 - inability to prove is simply red
            return None
        cte_names = {
            cte.alias_or_name.casefold()
            for cte in ast.find_all(exp.CTE)
            if cte.alias_or_name
        }
        # DuckDB resolves unquoted identifiers case-insensitively. A differently
        # cased CTE can therefore shadow a physical TaskIR table even though
        # sqlglot preserves their distinct spellings.
        if cte_names & set(folded_table_names):
            return None
        for table in ast.find_all(exp.Table):
            name = table.name
            if name not in table_specs:
                if form == "variant":
                    # `custom@add_dedup` wraps this scan too (a CTE reference,
                    # a function table, anything the reference names), and
                    # no declared key proves DISTINCT is an identity there.
                    return None
                continue
            if target is not None and name != target:
                continue
            # The admitted compiler reconstructs the outer alias from
            # ``table.alias``.  It cannot preserve qualified/table-valued
            # identifiers or an alias column list such as ``c(x, y)``.  Those
            # rewrites may rebind columns, so PK uniqueness is not an identity
            # proof for them even when today's five populations all pass.
            if not isinstance(table.this, exp.Identifier):
                return None
            alias = table.args.get("alias")
            if alias is not None:
                if not isinstance(alias, exp.TableAlias) or not isinstance(
                    alias.this, exp.Identifier
                ):
                    return None
                if any(
                    key != "this" and value not in (None, False, (), [])
                    for key, value in alias.args.items()
                ):
                    return None
            # Only a raw local scan with an optional alias earns the identity
            # proof; table modifiers can change rows inside the DISTINCT query.
            if any(
                key not in {"this", "alias"}
                and value not in (None, False, (), [])
                for key, value in table.args.items()
            ):
                return None
            affected.add(name)
    if not affected:
        return None

    proof_tables: list[dict[str, object]] = []
    for name in sorted(affected):
        primary_key = table_specs[name].primary_key
        if not primary_key:
            return None
        proof_tables.append(
            {
                "name": name,
                "key_kind": "primary_key",
                "columns": list(primary_key),
            }
        )
    if form == "variant":
        # Re-derive the directive exactly as the compiler does and require
        # that it changes precisely the recorded marts, each with at least
        # one DISTINCT scan; anything else is not this rewrite.
        if attacks_mod is None:
            from elt_taskgen.verification import attacks as attacks_mod
        changed: list[str] = []
        for mart in task.marts:
            gold_sql = dict(task.reference.sql_by_mart).get(mart.name)
            if not gold_sql:
                return None
            try:
                mutated = attacks_mod._apply_kind(
                    AttackKind.CUSTOM, gold_sql, mart, "add_dedup"
                )
            except Exception:  # noqa: BLE001 - a compiler error is not a proof
                return None
            if mutated is None:
                continue
            try:
                mutated_ast = sqlglot.parse_one(mutated, read="duckdb")
            except Exception:  # noqa: BLE001
                return None
            distincts = sum(
                1
                for select in mutated_ast.find_all(exp.Select)
                if select.args.get("distinct") is not None
            )
            if distincts < 1:
                return None
            changed.append(mart.name)
        if sorted(changed) != variant_targets:
            return None
    return {
        "proved": True,
        "proof": _SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF,
        "operation": "add_dedup",
        "form": form,
        "tables": proof_tables,
    }


def _normalize_schema_equivalent_promotions(
    task: TaskIR,
    findings: list[Finding],
    workspace: Path,
    promotion,
    attacks_mod,
):
    """Reclassify only compiler-proved, fully measured relational identities.

    The promoter correctly refuses to create a required attack that predicts no
    kill. Usually that is a blocking live exploit. For the one schema identity
    proved above, retain the rejected outcome but attach the proof, correct its
    reason, and rewrite the audit sidecar with that normalized evidence.
    """
    findings_by_id = {finding.finding_id: finding for finding in findings}
    normalized: dict[str, object] = {}
    for outcome in promotion.outcomes:
        finding = findings_by_id.get(outcome.finding_id)
        if finding is None:
            continue
        proof = _source_dedup_equivalence_proof(task, finding, outcome, attacks_mod)
        if proof is None:
            proof = _zero_default_equivalence_proof(task, finding, outcome, attacks_mod)
        if proof is None:
            proof = _drop_frame_equivalence_proof(task, finding, outcome, attacks_mod)
        if proof is None:
            continue
        updated = outcome.model_copy(
            update={
                "reason": (
                    "proposal is provably output-equivalent on the declared "
                    "primary-key domain, and was confirmed to keep FULL combined "
                    "reward on all five populations; record as a non-exploit"
                    if proof.get("proof") == _SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF
                    else "proposal is provably an identity on this task's gold "
                    "(every window it unframes is an extreme ordered by its own "
                    "column), and was confirmed to keep FULL combined reward on "
                    "all five populations; record as a non-exploit"
                    if proof.get("proof") == _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF
                    else "proposal is provably an identity on this task's gold "
                    "(every default it rewrites is the number 0), and was "
                    "confirmed to keep FULL combined reward on all five "
                    "populations; record as a non-exploit"
                ),
                "fidelity": {
                    **outcome.fidelity,
                    "schema_equivalence": proof,
                },
            }
        )
        normalized[outcome.finding_id] = updated
        attacks_mod._record_rejected_proposal(workspace, task, updated)
    if not normalized:
        return promotion

    replace = lambda outcome: normalized.get(outcome.finding_id, outcome)
    return attacks_mod.PromotionResult(
        task=promotion.task,
        promoted=promotion.promoted,
        rejected=tuple(replace(outcome) for outcome in promotion.rejected),
        outcomes=tuple(replace(outcome) for outcome in promotion.outcomes),
        rewards=promotion.rewards,
    )


def _normalize_inapplicable_promotion(
    task: TaskIR,
    findings: list[Finding],
    workspace: Path,
    promotion,
    attacks_mod,
):
    """Bind an inapplicable promotion to a deterministic failed-fidelity record."""
    by_id = {finding.finding_id: finding for finding in findings}
    normalized = []
    changed = False
    for outcome in promotion.outcomes:
        if (
            outcome.promoted
            or outcome.measured
            or "FileNotFoundError" not in outcome.reason
            or attacks_mod.MUTATION_FIDELITY_FILENAME not in outcome.reason
        ):
            normalized.append(outcome)
            continue
        finding = by_id.get(outcome.finding_id)
        proposal = finding.proposed_case if finding is not None else None
        if finding is None or proposal is None:
            normalized.append(outcome)
            continue
        attack_dir = (
            Path(workspace)
            / "tasks"
            / task.task_id
            / "attacks"
            / outcome.case_name
        )
        inapplicable_path = attack_dir / "inapplicable.json"
        try:
            inapplicable = json.loads(inapplicable_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            normalized.append(outcome)
            continue
        if (
            inapplicable.get("case") != outcome.case_name
            or inapplicable.get("task_content_hash") != task.content_hash()
        ):
            normalized.append(outcome)
            continue
        detail = str(inapplicable.get("inapplicable") or "").strip()
        if not detail:
            normalized.append(outcome)
            continue

        candidate = attacks_mod._candidate_case(finding, proposal, required=False)
        mutation = candidate.mutation.strip()
        exception_name = "InertAstMutationError"
        if mutation.startswith(attacks_mod.LOAD_DIRECTIVE_PREFIX):
            exception_name = "InapplicableLoadMutationError"
        elif mutation.startswith(attacks_mod.STRUCTURED_DIRECTIVE_PREFIX):
            structured = attacks_mod.parse_structured_directive(
                mutation[len(attacks_mod.STRUCTURED_DIRECTIVE_PREFIX):]
            )
            if structured.get("operation") in {"skip_backend", "skip_tables"}:
                exception_name = "InapplicableLoadMutationError"

        claim = dict(outcome.fidelity)
        compiler = attacks_mod._write_fidelity_record(
            workspace,
            task,
            candidate,
            passed=False,
            requested={"mutation": mutation},
            realized={},
            checks=("mutation applicability checked before reward execution",),
            errors=(detail,),
        )
        fidelity = {
            "passed": False,
            "claim": claim,
            "compiler": compiler,
        }
        replacement = outcome.model_copy(
            update={
                "reason": (
                    "proposal could not be executed: "
                    f"{exception_name}: {detail}"
                ),
                "fidelity": fidelity,
            }
        )
        attacks_mod._record_rejected_proposal(workspace, task, replacement)
        normalized.append(replacement)
        changed = True

    if not changed:
        return promotion
    outcomes = tuple(normalized)
    return attacks_mod.PromotionResult(
        task=promotion.task,
        promoted=promotion.promoted,
        rejected=tuple(outcome for outcome in outcomes if not outcome.promoted),
        outcomes=outcomes,
        rewards=promotion.rewards,
    )


def run_attack_stage(engine: Engine, task: TaskIR) -> StageOutcome:
    """Compile, measure, and promote exactly confirmed attack proposals.

    Promotion changes task identity, so all attack evidence is remeasured at the
    new hash.
    """
    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.review.council import ProviderProtocolError
    from elt_taskgen.verification import attacks as attacks_mod

    gold = gold_mod.load_gold(_pipeline_support._answer_key_dir(engine, task))
    review_payload = _pipeline_support._require_pass_payload(engine, task, StageName.REVIEW)
    findings = [Finding.model_validate(f) for f in review_payload.get("findings", [])]
    # Revalidate persisted review evidence too: older/current ledgers may have
    # been produced before the critic protocol enforced explicit variants.
    try:
        executable_findings = _validated_executable_findings(task, findings)
    except ProviderProtocolError as exc:
        return _pipeline_handoff._critic_protocol_block(
            f"critic protocol failure: {type(exc).__name__}: {exc}"
        )
    pending_adjudication = _pipeline_handoff._critic_adjudication_block(findings)
    if pending_adjudication is not None:
        return pending_adjudication

    original_hash = task.content_hash()
    promoted: tuple[str, ...] = ()
    rejected: tuple[dict, ...] = ()
    proposal_outcomes: dict[str, object] = {}
    # Two passes at most: the second re-measures everything at the identity
    # promotion produced. A third would mean promotion is not idempotent.
    for _pass in range(2):
        cases = attacks_mod.compile_attacks(task, executable_findings)
        rewards: dict[str, dict[str, float]] = {}
        inapplicable: dict[str, str] = {}
        for case in cases:
            measured = attacks_mod.run_attack(task, case, gold, engine.workspace)
            if not measured:
                # An informational probe whose surface this task's DATA does not
                # offer. run_attack recorded the reason; it must NOT enter the
                # measured payload, where an empty entry would read as deleted
                # evidence. A REQUIRED case can never take this path.
                record_path = (
                    engine.task_dir(task.task_id)
                    / "attacks" / case.name / "inapplicable.json"
                )
                record = json.loads(record_path.read_text(encoding="utf-8"))
                inapplicable[case.name] = str(record.get("inapplicable", ""))
                continue
            rewards[case.name] = {p.value: float(r) for p, r in measured.items()}

        promotion = _normalize_inapplicable_promotion(
            task,
            findings,
            engine.workspace,
            attacks_mod.promote_proposed_cases(
                task,
            # Withhold ambiguity alternatives from promotion; route the claim to prose repair.
                [f for f in executable_findings if f.role is not CouncilRole.AMBIGUITY_CRITIC],
                engine.workspace,
                gold,
            ),
            attacks_mod,
        )
        promotion = _normalize_schema_equivalent_promotions(
            task,
            findings,
            engine.workspace,
            promotion,
            attacks_mod,
        )
        for proposal_outcome in promotion.outcomes:
            previous = proposal_outcomes.get(proposal_outcome.finding_id)
            if previous is None or proposal_outcome.fidelity or proposal_outcome.measured:
                proposal_outcomes[proposal_outcome.finding_id] = proposal_outcome
        # Rejected proposals keep their measured rewards in the payload: their
        # probes exist on disk, and evidence for a probe that ran is never missing.
        for name, measured in promotion.rewards.items():
            rewards.setdefault(
                name, {p.value: float(r) for p, r in measured.items()}
            )
        # Read the outcomes, not just this pass's new promotions: on the re-measure
        # pass an already-promoted case reports promoted with no fresh AttackCase.
        promoted = tuple(o.case_name for o in promotion.outcomes if o.promoted)
        rejected = tuple(o.model_dump(mode="json") for o in promotion.rejected)
        if not promotion.task_changed:
            break
        task = promotion.task
    else:  # pragma: no cover - promotion is idempotent by construction
        raise RuntimeError(
            f"task {task.task_id!r}: attack-case promotion did not converge "
            "after two passes (fail closed)"
        )

    changed = task.content_hash() != original_hash
    by_variant = _recorded_variant_rewards(engine, task, rewards)
    detail = (
        f"{len(cases) - len(inapplicable)} attack case(s) executed on "
        f"{len(task.populations)} population(s); {len(promoted)} proposal(s) "
        f"promoted, {len(rejected)} rejected"
        + (
            f"; {len(inapplicable)} probe(s) recorded inapplicable "
            f"({', '.join(sorted(inapplicable))})"
            if inapplicable
            else ""
        )
    )
        # Pass only screened findings so voided claims cannot block promotion.
    blocking_findings, proposal_problems = _blocking_proposal_failures(
        executable_findings, proposal_outcomes.values()
    )
    if proposal_problems:
        outcomes = tuple(proposal_outcomes.values())
        selected = _selected_blocking_finding(blocking_findings, outcomes)
        # A non-empty problem list must have a finding.  Keep the assertion at
        # this producer boundary: emitting an opaque attack failure would send
        # the repair proposer back to the abstention loop this descriptor closes.
        if selected is None:  # pragma: no cover - guarded by the builder above
            raise RuntimeError("blocking proposal failure has no finding")
        route = _proposal_failure_route(blocking_findings, outcomes)
        return StageOutcome(
            VERDICT_FATAL if route is RepairRoute.FATAL else VERDICT_FAIL,
            _pipeline_payloads.AttackPayload(
                rewards=rewards,
                rewards_by_variant=by_variant,
                cases=tuple(c.name for c in cases if c.name not in inapplicable),
                inapplicable_cases=inapplicable,
                promoted_proposals=promoted,
                rejected_proposals=rejected,
                proposal_outcomes=tuple(
                    outcome.model_dump(mode="json")
                    for outcome in sorted(
                        proposal_outcomes.values(), key=lambda value: value.finding_id
                    )
                ),
                blocking_finding=_attack_finding_descriptor(task, selected),
                detail=(
                    detail
                    + "; critic-to-mutation handoff BLOCKED: "
                    + "; ".join(proposal_problems)
                ),
            ),
            route=route,
            task=task if changed else None,
        )
    return StageOutcome(
        VERDICT_PASS,
        _pipeline_payloads.AttackPayload(
            rewards=rewards,
            rewards_by_variant=by_variant,
            cases=tuple(c.name for c in cases if c.name not in inapplicable),
            inapplicable_cases=inapplicable,
            promoted_proposals=promoted,
            rejected_proposals=rejected,
            proposal_outcomes=tuple(
                outcome.model_dump(mode="json")
                for outcome in sorted(
                    proposal_outcomes.values(), key=lambda value: value.finding_id
                )
            ),
            detail=detail,
        ),
        task=task if changed else None,
    )

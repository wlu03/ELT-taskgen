# Bounded agents, Phase 3 — change log

Status: in the working tree on 2026-09-03 (this log). Phase 3 is §6 of the
bounded-agents roadmap (Table 7, its interface block, its test list and its
definition of done; the limits vocabulary addendum T1.1, T3, T7, T8; the
repair/certify addendum §3.1, §4.6, §4.8; the constraint addendum A24;
Output 11 for pilot P4), built on the Phase 0, 1 and 2 change logs
(`docs/plans/bounded_agents_phase0.md`, `docs/plans/bounded_agents_phase1.md`,
`docs/plans/bounded_agents_phase2.md`). Three concurrent efforts share the
tree: item 1 (the POPULATION route and the `attack` member of `certify`),
item 2 with items 3, 4 and 5 (the critic validators, the correction channel,
`audit list`, `config/agents.yaml`) and item 6 (this log, `PILOT-P4.md`, the
interface and README entries). Every item is recorded here from its tests;
item 1's builder died mid-work on 2026-09-03 and its section was first
written from the code in the tree — it was completed on 2026-09-04 (the
nine roadmap tests, the POPULATION planted corpus, the `DIAGNOSTICS_VERSION`
bump; §1 item 1 and §4 list what shipped). The owner actions in §5 are NOT
done and gate the phase's definition of done: the P4 pilot, the
`measured_match_bit` decision, the Phase 4 re-earn.

What ships is built DEFAULT-OFF, three ways at once. The two critic seats'
harness validators are declared but their `session:` blocks say `enabled:
false`, so nothing reaches the wire: each seat's wire `tools[]` is still
the forced `report_findings` object alone, `tool_choice` is unchanged, the
manifest's `harness_validators` is `[]`, `validator_digests` is empty,
`tool_surface_sha256` (`a387488579fefb38…`) and the `ambiguity_critic` /
`feasibility_reviewer` / `repair_proposer` digests did not move, and
`HARNESS_VERSION` stays "5". What DID move — precisely because a critic
block is hashed verbatim whether or not it is enabled (SoT R0.2, T7 row 229;
Phase 0 log §1 0.E, Phase 1 log §2 finding 1-0) — is the two declared
seats' `loop_limits`, their behaviour digests, their `policy_sha256`, their
one-shot transcript keys and, through them, `council_routing_fingerprint`
(§2 below lists the before/after values; pinned as literals by
`test_phase3_critic_digests_and_fingerprint_are_pinned`;
`test_disabled_critic_tools_do_not_enter_the_wire_manifest_or_fingerprint`
pins that the VALIDATORS enter nothing while disabled; `python
tools/prove_admission_integrity.py` prints `ALL PROOFS HELD`, PROOF 5
included). The bounded proposer's POPULATION route exists only behind the
CLI opt-in `--repair-proposer-mode bounded` (the default stays `one_shot`,
byte-identical). The `attack` member of the in-session `certify` sits behind
`repair.certify.attack_enabled: false`. Rollback of each is the config key —
`roles.population_adversary.session.enabled: false`,
`roles.shortcut_attacker.session.enabled: false`,
`roles.population_adversary.session.measured_match_bit: false`,
`repair.routes_bounded` without `population`, `repair.certify.attack_enabled:
false` — never a code revert. No re-earn is triggered by flipping the
default-off keys back and forth while the seats stay disabled; the ONE
re-earn Phase 3 does owe is the harness-5 fresh-live run that was already
owed since 0.E, which must now be run at or after this tree (§5 item 3).

Review remediation (2026-09-03, findings 0-0, 1-0, 1-1, 1-2, 2-0, 2-1, 2-2,
2-3, 2-4, 3-0 of the Phase 3 implementation review) is folded into the
sections below; §2 lists every observable change it made. A second review
round over the POPULATION route (2026-09-04; findings 0-0, 0-1, 0-2, 1-0,
1-1, 1-2 of the population-route review — cited below as "review finding
(pop) n-m" to keep them apart from the first round's numbers) is folded in
the same way: the view's private-material gate, the schema-only witness
roles, the C7 split by exception class, the `attack` member dropped rather
than the call refused, and the flag's budget validation; the same round
repaired the two suite failures the `DIAGNOSTICS_VERSION` bump had left
(the projection sanitizer pin's literal `"1"`, and the two re-keyed
transcript fixtures, re-produced by `tools/migrate_transcript_fixtures.py`
from the byte-identical legacy originals — only their recorded
`route.diagnostics_version` moved from `"1"` to `"2"`; the keys did not).

## 0. What "population design" means here, and what it never means

The five populations are a fixed enum; seeds, sizes and witness rows are
minted from the star shape; `realized_row_count` is private; every
`generate` re-derives and byte-compares the tree; `populations/**` is in no
repair allowlist. So a model may (a) propose an executable attack case in
the closed grammar, certified only by exact-match promotion, or (b) propose
a route-scoped IR patch to `populations.*.conditions`, `populations.*.scale.*`
or `populations.*.literal_rows`, the last admitted ONLY through the unchanged
`attempt_patch` behind the discrimination guard. No model writes rows, seeds
or realized counts and none sees them; no "population designer" role
exists; OQ-21 (reading `literal_rows`) stays declined.

## 1. What shipped, per item

### Item 1 — the POPULATION route and the `attack` member of `certify`

Shipped in `review/repair_proposer.py`, `review/tools/validators.py`,
`review/tools/certify.py`, `review/tools/projection.py` and
`config/agents.yaml`; completed on 2026-09-04 (the builder died mid-work the
day before). What a model may do on this route is exactly §0: a
`conditions` edit through `apply_edit_trial` (the scales are numbers the
string editor cannot address; a read or a write naming
`populations.*.literal_rows` is a `ForbiddenArgument` halt on every route),
certified in-session by the provider-free `certify` (its `attack` member
only under the flag) and committed ONLY through the unchanged
`attempt_patch` behind the discrimination guard. Pinned by
tests/test_repair_proposer_agentic.py `PopulationRouteTest`:
`test_population_route_read_field_excludes_literal_rows` (the readable set
is `ROUTE_IR_PATHS[POPULATION]` minus `LITERAL_ROWS_PATHS` plus
`PUBLIC_SCHEMA_PATHS`; through the real runner a literal-rows read is a
POLICY_VIOLATION halt with no row value in any message; conditions
acknowledged, a scale withheld, a schema path projected),
`test_population_patch_commits_only_when_discrimination_matrix_is_superset`
(session 1 disarms the dangling-key lever over a measured baseline and is
rejected `discrimination_weakened` with the live tree byte-unchanged;
session 2 keeps it and commits after `attack` re-ran on the certifier's
trial; no re-measurement lands live),
`test_regenerate_after_population_patch_rederives_and_byte_compares` (a REAL
`counterfactual_untargeted` failure repaired by a `conditions` edit alone —
`check_cheap` red then green, the real `generate` on the certify copy, the
commit — then the live `generate` builds all five and a second run finds
them byte-identical with no drift; the counterfactual materializes EMPTY
tables: no row, seed or scale was written),
`test_realized_row_count_never_appears_in_any_turn` (the copy's real
`generate` realizes every hidden count; none is a token of any message,
session record or transcript while the declared scales are in the view),
`test_witness_problem_codes_name_only_public_shapes`,
`test_population_session_certify_reruns_reference_and_attack_when_literal_rows_move`
(flag on: the proof stages run on the copy whatever the failed stage, 6
bits, the 1 200 s deadline, the bit live — a keeping move green, a
neutering move `discrimination_weakened=true`; flag off: `generate` alone,
2 bits, the bit pinned False),
`test_second_certify_after_attack_rerun_does_not_raise_scope_violation`
(the copy's `attacks/*/rewards.json` and ledger rows never reach the held
trial: the next write, `check_scope`, a second executed certify and the
final `attempt_patch` see task_ir.json alone),
`test_certify_attack_member_refused_when_critic_view_changed` (a
`conditions` edit moves the adversary's view: the `attack` MEMBER is
dropped, the Phase 1 members run as a paid call with
`model_stages_deferred=True`, through the tool's hook and as an executed
runner turn; the call-level code survives for an empty stage set only; the
Phase 1 tool never declares the code; a literal-rows move alone keeps the
member admitted), `test_population_conditions_patch_recalls_only_the_adversary_seat`
(`TrialLedgerCurrencyTest`), and
`test_population_route_is_enabled_by_routes_bounded` (`BoundedProposerTest`;
replaces the Phase 1 refusal pin: the shipped key runs the session and
commits, the rollback document refuses before any model call). The
POPULATION planted corpus of tests/test_repair_loop_planted.py
(`test_population_corpus_commits_through_the_unchanged_certifier_behind_the_guard`)
holds 23 induced failures with a known in-route `conditions` fix (12
missing conditions, 8 corrupted conditions, 3 real
`counterfactual_untargeted` coverage failures certified in-session on a
disposable copy under the real `cli.run_generate`) plus a 9-case guard
corpus (3 weakening edits over a measured baseline, 3 keeping edits, 3
literal-rows attempts), driven by the scripted double through the real
session runner and the real `attempt_patch`: DoD (4) reads 26 commits of
32 (Wilson lower bound at 85 % ≈ 0.70 ≥ 0.5), zero `DiscriminationWeakened`
commits (six rejections), zero row deletions past the guard (three
`ForbiddenArgument` halts, the rows byte-identical).

* `repair.routes_bounded: [specification, reference, population]`
  (`repair_proposer.DEFAULT_ROUTES_BOUNDED`, mirrored in
  `providers.DEFAULT_ROUTING_DOC`): with `--repair-proposer-mode bounded` a
  POPULATION failure now runs a session on a held trial copy instead of
  being refused with `STATUS_ROUTE_NOT_BOUNDED`. The route's editable IR
  fields are `ROUTE_IR_PATHS[POPULATION]` (`populations.*.conditions`,
  `populations.*.scale.*`, `populations.*.literal_rows`); `read_field` reads
  those plus `validators.PUBLIC_SCHEMA_PATHS` MINUS `LITERAL_ROWS_PATHS` on
  every route (a read or write naming literal rows is `ForbiddenArgument`,
  the SoT T4 violation, never a projection). A population patch commits
  ONLY through the unchanged `attempt_patch` = `trial_phase` + `_commit`
  behind the discrimination guard (`repair.discrimination_problems`, the
  counterfactual row-count guard, `LITERAL_ROWS_PROOF_STAGES`). The guard's
  arming predicate is `repair_proposer.discrimination_guard_armed(*,
  literal_rows_moved, population_moved, matrix_before)` (review finding
  2-1): literal rows ALWAYS (fail closed, an empty baseline rejects — the
  rows are the coverage check's lever); any other population material —
  `populations.*.conditions`, which drive row generation and the
  dangling-key lever `source_data.declares_dangling` /
  `gates._gate_referential_integrity` read, and `populations.*.scale.*` —
  whenever a MEASURED baseline exists at the live hash
  (`_moved_population_material`, the superset of
  `_moved_counterfactual_rows`). Armed, `reference` and `attack` re-run on
  the trial whatever the failed stage and the superset rule runs; the
  deletion-count rule (`counterfactual_row_problems`) stays
  literal-rows-specific. With no baseline (a failure before `attack` at this
  hash) a `conditions` repair at `generate` stays expressible on the route's
  own stages (`check_population_cheap` is the cheap gate there; the
  post-commit ladder measures the committed populations from scratch), so
  the POPULATION route is not closed at the stage where coverage failures
  occur. The in-session certify's `attack` member arms its `guard` bit by
  the same predicate (`certify.attack_member_context(..., literal_rows_moved,
  population_moved)`, context key `rows_guard` for the deletion rule;
  `ProposerSession.population_moved()` / `guard_armed()`). Pinned by
  `test_population_conditions_patch_reruns_attack_and_runs_the_guard`.
* The submit-time certifier's NO-MEASURE rules (`_revalidate`; review
  findings 2-0 and 2-4, both C7; split by exception CLASS per review
  finding (pop) 1-0): a stage runner that RAISES an exception of a class
  that COULD NOT MEASURE the task's bytes — `certify.
  COULD_NOT_MEASURE_EXCEPTION_NAMES`, matched by name through the MRO:
  `MemoryError`, `OSError` (ENOSPC while gold is frozen), the storage
  engines' `OperationalError` (DuckDB out-of-memory and I/O, a locked
  sqlite ledger), `FatalException`, `InternalError`, `InterruptException`,
  the engine's own `EngineError`; plus every `SessionFault`, every
  `_INFRA_EXCEPTION_NAMES` class and transport prefix through the chain
  walk — is not wrapped into `RevalidationFailed(revalidation_red_<stage>)`;
  it is the in-session certify's rule (`certify.classify_runner_exception`,
  `certify.runner_exception_could_not_measure`) — a `ToolHarnessFault`
  carrying the exception CLASS only, raised as `InfrastructureFailure`
  (marker `ToolHarnessFault`), so both proposers halt on the first attempt
  with no `RejectionCode`, nothing queued, no second model call, and the
  engine halts with zero rounds. ANY OTHER raising runner is a defect the
  patch PRODUCED — `duckdb.ParserException` out of the real reference runner
  on SQL the model broke, a mutant that lost its surface
  (`InertAstMutationError` / `InapplicableLoadMutationError`), the reference
  runner's count / content divergence `ValueError` — which the live ladder
  records as a scored FAIL row and spends a round on, so the trial answers
  the same red verdict: `RevalidationFailed(revalidation_red_<stage>)`, a
  rejection the model pays for (a second attempt, then adjudication), never
  a free halt it could trigger at will. The in-session `certify` answers
  `certify_red_<stage>` for it, and the bounded `_submit` projects a
  certifier exception no `PatchRejected` class named (an IR the edits made
  invalid) as `revalidation_red_unknown`, halting only on a harness fault
  (marker `ToolHarnessFault`). This SUPERSEDES the certify addendum §3.1
  "Faults" row's "anything else → `ToolHarnessFault`" (recorded deviation:
  that row read C7 as "every runner exception is infrastructure", which
  handed the model an unscored abort). Pinned by
  `test_certify_model_caused_runner_exception_is_red_not_a_halt`
  (tests/test_review_tools_validators.py: the REAL reference runner, a
  syntactically broken edit, both workers) and
  `test_model_caused_certifier_exception_is_a_rejection_not_a_halt`
  (tests/test_repair_proposer_agentic.py: `attempt_patch`, both proposers,
  `_submit`); `test_revalidate_runner_raising_oserror_is_infrastructure_not_red`
  keeps the OS / storage-engine classes on the halting side. And a
  CURRENCY PREREQUISITE outside the
  route's own rerun set — `review`, which `_with_currency_prerequisites`
  prepends before `attack` on the REFERENCE / POPULATION routes only so the
  real attack runner finds its PASS row at the new hash — that answers
  non-PASS on the trial (a live critic's FATAL finding at the moved
  adversary view, or the review runner's own SPECIFICATION-route
  pre-checks) is a could-not-certify, not a verdict on the patch (C4): it
  halts as `engine.StageBlocked` under
  `blocked_on:prerequisite_not_current_review`
  (`BLOCKED_ON_PREREQUISITE_PREFIX`), no round, the ladder resumes once the
  wait clears; `revalidation_red_review` survives only where `review` is a
  route member (SPECIFICATION). The live adversary re-call itself
  (`test_population_conditions_patch_recalls_only_the_adversary_seat`) is
  unchanged. Pinned by
  `test_revalidate_runner_raising_oserror_is_infrastructure_not_red` and
  `test_population_patch_is_not_rejected_by_a_live_review_verdict`.
* The proposer view's private-material tripwire (`_assert_scope`; review
  finding 2-2) raises `DiagnosticTripwire` (`_view_trip`, codes
  `view_private_fragment` / `view_private_ast`, source `repair_view`, the
  offending bytes only in the quarantine, never in the message) instead of
  a plain `RuntimeError`, so a leaking view is the SoT T6 `LEAK_TRIPWIRE`
  halt for both proposers — no second paid attempt, no adjudication, no
  round — and `AgenticRepairProposer._run_session` builds the view INSIDE
  its session fault boundary, so the trip lands in `episode.fault`, is
  classified by `_session_halt_marker` and leaves a session record with no
  model call. Pinned by `test_view_leak_tripwire_halts_without_spending_a_round`.
* `failure_detail` on the POPULATION route (review finding 0-0) no longer
  carries the promoter's per-proposal verdict codes (`proposals`: `inert`,
  `inapplicable`, `no_kill_predicted`, `mismatch`, `fidelity_failed`,
  `unknown_kind`); every route projects an attack payload to
  `{failing_gates: [], codes: {attack: <code>}}` and the key is outside
  `_EVIDENCE_KEYS` (the D1 gatekeeper trips on it). The codes stay where the
  trust boundary (rows 2 and 7) and SoT T3 place them: the post-session
  `project_promotion` / `project_proposal_matrix` records in
  `rejected_proposal.json` and `audit list`. Pinned by
  `test_population_failure_detail_never_carries_promoter_verdict_codes`.
* `check_population_cheap(trial, task) -> Diagnostic` and
  `witness_problem_codes(task) -> tuple[str, ...]` (roadmap "New
  interfaces"): the POPULATION member of `check_cheap`
  (`validators.CheapCheckTool`). `validate_population_coverage(task)` is
  projected to `projection.POPULATION_CHEAP_CODES` (`missing_population`,
  `scale_drift`, `no_scale_no_rows`, `counterfactual_untargeted`; any other
  class the generic `population_problems`) and `mart_plan._witness_problems`
  to `projection.WITNESS_PROBLEM_CODES` (`witness_unknown`,
  `witness_no_bridge`, `witness_role_missing`, `witness_no_second_hop`),
  matched on each sentence's fixed clause and never echoed; the Diagnostic's
  `code` is the first present in precedence order, every class a flag,
  `population_ok` and `witness_ok` the whole answer; never a count, a
  population name or a row value. The `FactRoles` behind
  `witness_problem_codes` (`_roles_from_counterfactual(task, anchor,
  bridge, child)`) are read off the PUBLIC schema and the anchor / bridge /
  child the conditions declare ONLY — the bridge's primary key as the link
  key, its foreign key onto the child as the child key, its first non-key
  numeric / text / enum / date column as measure / label / predicate /
  period, the anchor's first enum column as the domain — never off
  `literal_rows` (review finding (pop) 0-1: the earlier derivation read
  which columns and string values the hidden rows carried, so a session
  re-pointing the anchor line at any table pair learned the hidden rows'
  column types through `witness_role_missing`; OQ-21 stays declined). The
  delivered bytes are therefore a pure function of what the view shows,
  pinned by `test_check_population_cheap_is_invariant_under_literal_row_content`
  (schema-valid variants of the hidden rows leave the Diagnostic and the
  roles byte-identical; every role value is a schema column or a declared
  enum value).
* The POPULATION view's private-material gate (review findings (pop) 0-0
  and 0-2): `view_for_route` prints the conditions block through
  `population_conditions_block(task)` — `council._population_summary(task,
  with_conditions=True, condition_text=...)`, the adversary's own rendering
  byte-unchanged when the hook is absent — where every condition line
  `projection.population_condition_private_shape` refuses is shown as
  `CONDITION_WITHHELD` (`[withheld: private material]`, the evidence
  scrub's own words). Every population's lines are held to the gold-free
  transport detectors (a measured value, a path, a secret, executor text);
  a HIDDEN population's (every one but DEVELOPMENT, OQ-23 option C) further
  to the number rules the projector applies to a `Diagnostic`: a count
  vector or key tuple, ANY standalone number that is not that population's
  own declared scale (`standalone_number`), and a digit run outside a
  public identifier equal to a private scalar of the route or to a hidden
  population's REALIZED count — `projection.hidden_realized_counts(task)`,
  `source_data.realized_row_count` over the declared scales, a
  deterministic function of the IR known without gold. What that withholds:
  the generator's `COVERAGE SHORTFALL` notes on a degraded STRESS population
  (`budgeted for N rows` with N = the realized parent count, or that count
  times the public enum domain; `needs at least N rows`), and the demo's
  hand-written `C11: one completed order with 3 items, 1*10 + 2*15 + 1*5 =
  45` / `C12: only a cancelled order worth 100 -> (0, 0)` lines (literal-row
  values, the counterfactual gold mart value, the literal-row count `3`);
  what stays: the digit-free `C10/C11/C12` label, every DEVELOPMENT note,
  every declared scale the route may edit (`table~scale`). A withheld slot
  is never a substring oracle: `ProposerSession.withheld_conditions` holds
  the LIVE task's set once at INIT, `read_field` answers `field_withheld`
  on it, `apply_edit_trial` answers `patch_anchor_not_found` for a replace /
  delete hit and miss alike without reading a byte (the finding 3-0 rule),
  and `insert` (no anchor) stays open, so a scripted repair may still
  append to it. Pinned by
  `test_population_view_withholds_hidden_realized_counts_and_literal_values`
  (the demo and two coverage-degraded generated tasks, the gate's rules per
  population class, the session's three tools, the runner's messages and
  the session record). Two residuals are named, not fixed, here: (i) the
  PRODUCER — `generation/populations.py::coverage_budget_scale` derives the
  note numbers AND the degraded population's DECLARED child scale from
  `realized_row_count` (on the 80-value fixture `facts~45040` is exactly
  563 realized parents × 80), and the declared scale is the route's
  editable subject the view prints by design (`private_scalars(route=
  POPULATION)` excludes it; `test_population_view_declared_scales_agree_with_private_scalars`),
  so the residual is the generation owner's: state a shortfall in declared
  terms and pick a degraded scale that is not a realized product (§4 item
  8; outside this change set's fence); (ii) the population adversary's
  council view still carries every condition verbatim — its digest is
  admission evidence (`metrology.view_digest`), so gating it is a Phase 4
  re-earn item (§4 item 9).
* The flag's own budget (review finding (pop) 1-2):
  `repair_proposer.validate_attack_flag_limits(...)`, run by
  `AgenticRepairProposer.__init__`, refuses to START (a `ValueError` naming
  the key, like `repair_settings`' malformed-value checks) when
  `repair.certify.attack_enabled` — or the proposer block's own
  `session.certify.attack_enabled` — is on while the block keeps a Phase 1
  budget: it requires `max_oracle_bits >= max_certify ×
  CERTIFY_ATTACK_ORACLE_BITS` and, when a wall is declared, `wall_clock_s
  >= max_certify × the certify deadline` (the certify addendum §4.6 arm: 12
  bits, 2 700 s). Before, the shipped `max_oracle_bits: 4` under the flag
  tripped LIMIT_ORACLE at the FIRST certify's PERMIT (6 > 4: no worker,
  `blocked_limit`, no round) — the flag silently disabled certify. Pinned
  by `test_attack_flag_requires_the_declared_oracle_and_wall_budget`.
* The `attack` member (`review/tools/certify.py`, module docstring "PHASE
  3"): behind `repair.certify.attack_enabled` (`RepairSettings.
  certify_attack_enabled`, default `DEFAULT_CERTIFY_ATTACK_ENABLED = False`;
  the proposer's own `session.certify.attack_enabled` declaration is
  honoured too, so a pilot arm carries the flag in its hashed block) the
  stage set gains `CERTIFY_ATTACK_STAGE = "attack"` — `cli.run_attack_stage`,
  provider-free — on `ATTACK_ROUTES = (POPULATION, REFERENCE)` only. The real
  attack runner opens with the `review` PASS at the current hash, and the
  worker holds no provider, so the member is admitted ONLY when that review
  can be memo-served: `memo_served_review(workspace, task)` reads the live
  ledger's `review` PASS at the live hash, `critic_views_changed(live_task,
  trial_task)` names every critic seat whose `council.render_view` moved,
  and with a view moved the `attack` MEMBER is refused — dropped from the
  call (`ProposerSession.attack_member_admitted` = available AND no view
  moved), the Phase 1 members still run on the copy as a paid call at the
  flag's 6 bits and the projection answers `model_stages_deferred=True`
  (the member is left to the submit-time certifier); the CALL is refused at
  PERMIT as `certify_refused_review_view_changed` (0 bits, no worker;
  `ATTACK_NO_COST_REFUSAL_CODES`) only when no other member remains
  (`attack_member_dropped_for_view` over an empty stage set). That is
  refusal (3) of the certify addendum §3.1 read as its O3 asks — "`attack`
  requested" names the member — so turning the flag on never removes a
  capability the flag-off session had (review finding (pop) 1-1: the
  earlier reading refused the WHOLE call, and since a `conditions` line is
  the only field the POPULATION session can edit, the flag then certified
  nothing at `attack` or later). A `conditions` patch moves the
  adversary's view (F3b); a literal-rows patch moves none, and then the
  proof stages `reference` and `attack` join the set whatever the failed
  stage (`provider_free_stages(route, failed_stage, *, attack_enabled,
  literal_rows_moved)`, `model_stages_deferred(...)` likewise; with the
  member dropped the proof stages do not join, exactly as with the flag
  off). The member
  runs under `CERTIFY_ATTACK_DEADLINE_S` (1,200 s; `repair.certify.deadline_s`
  overrides) at `CERTIFY_ATTACK_ORACLE_BITS` (6) per executed call, so
  `max_oracle_bits: 12` admits two; `attack_member_context(workspace, task,
  *, literal_rows_moved)` carries the memo-served payload, the live
  discrimination baseline (`discrimination_baseline`) and whether the guard
  is armed onto the copy as plain data (`ATTACK_CONTEXT_KEYS`), the memo's
  PASS row is recorded on the COPY's ledger at the copy's hash right before
  the member runs (F1 currency), and every side effect lands on the copy.
  The projector is `projection.project_certify_attack(code, *,
  model_stages_deferred, discrimination_weakened)` over `CERTIFY_CODES` plus
  `CERTIFY_ATTACK_CODES` (`certify_red_attack`,
  `certify_refused_review_view_changed`), with the `discrimination_weakened`
  bit LIVE only under the flag (`ok` is green AND not weakened); the Phase 1
  `project_certify` still refuses those codes and the bit. The tool is
  `validators.CertifyTool(attack_enabled=True)` (`PROPOSER_TOOLS_ATTACK`,
  `proposer_policy(..., attack_enabled=)`), which member set a call runs is
  the SESSION's (`ProposerSession.attack_member_available`,
  `certify_stages`, `certify_deferred`, `attack_context`,
  `literal_rows_moved`), and `AgenticRepairProposer.attack_enabled` /
  `_certify_deadline_s` read the flag. Off, the Phase 1 stage set
  (`generate`, `reference`), its 2 bits and its 300 s are byte-identical.

### Item 2 — the critic validators, DEFAULT-OFF (`review/tools/critic_validators.py`, new)

Module placement: roadmap Table 7 names `review/tools/validators.py`; the
four functions live in the new `critic_validators.py` because the
proposer's section of `validators.py` was concurrently edited by item 1.

* `compile_proposal(ctx, {"finding_index": int}) -> Diagnostic` (POP): wraps
  what `council._proposal_mutant_key` runs (`attacks.
  _proposal_structured_payload` / `_proposal_mutation` / `_candidate_case`),
  `validate_proposal_claim_fidelity` and `_proposal_mutant_key` itself for
  the in-payload same-mutant bit. `compile_probe(ctx, {"finding_index":
  int}) -> Diagnostic` (SHC): the `project_compile` projection of
  `council._compiled_mutant_key`'s computation (`attacks.compile_attacks(bare,
  [probe])`) — never the key string (it embeds `case.mutation`). Both take
  `finding_index` from the HARNESS (the payload order the `CriticSession`
  holds), never from the model.
* Projection shape (the roadmap's `{finding_index, compiles, grammar_code,
  missing_identifiers, duplicate_of}` realized under the `Diagnostic`
  contract, which has no int and no free-text field and whose projector
  refuses any digit run outside a public identifier): `source=compile`,
  `code` in {`compiles`, `uncompilable`}, `subject` = the attack KIND (public
  vocabulary), `grammar_code` as ONE boolean flag among `GRAMMAR_CODES`
  (`param_unknown`, `param_conflict`, `kind_operation_mismatch`,
  `uncompilable`, `claim_missing_identifier`), `missing_identifiers` as the
  PUBLIC `names` only (a hidden population is withheld), `duplicate_of` as
  the boolean `same_mutant_as_earlier_proposal` (`SAME_MUTANT_FLAG`: an
  EARLIER proposal of the SAME payload compiles to the same mutant — never a
  duplicate-of-catalogue bit, and named without the screen's lexicon).
  `SCREEN_LEXICON` (the `FindingScreenStatus` values and names: VOID, NOTED,
  DUPLICATE and the rest) never appears in any in-session projection;
  inert / inapplicable sentences never do (inertness needs gold;
  `materialize_mutation` is never called inside any session).
* The payload-level tools the harness runs on every submitted payload:
  `CompileProposalTool` (`fold_compile_proposal`: red iff any proposal is
  red; 3 oracle bits, 20 s, POP), `CompileProbeTool` (`fold_compile_probe`:
  a problem ONLY when no probe of the payload compiles, mirroring the review
  stage's "zero executable probes" diligence check; 3 bits, 20 s, SHC),
  `MeasuredMatchBitTool` (1 bit, `per_session: 1`, 300 s, POP) — all
  `harness_only`, `validator = True`, `trust_domain = "D3"`, `version`
  `CRITIC_VALIDATOR_VERSION = "1"`.
* `measured_match_bit(ctx, {"finding_index": int}) -> Diagnostic
  {exact_match}` (flag F; A24) behind
  `roles.population_adversary.session.measured_match_bit: false`
  (`MEASURED_MATCH_BIT_FLAG`; the validator's NAME is
  `session.MEASURED_MATCH_BIT_VALIDATOR`, declared by the flag, never by
  `harness_validators`): `promote_proposed_cases` on a scratch copy of the
  population rows under the tool-context root with the session's gold
  handle (a gold-bearing D3 worker; `source=promotion`, code `promoted` |
  `mismatch`); the aggregate tool answers ONE bit over the whole payload
  (vacuously exact for a payload with no proposal, still spending its one
  call) and the per-finding function answers `uncompilable` / `exact_match:
  false` for a finding without a proposal. The matrices never leave.
* `project_proposal_matrix(outcomes) -> {finding_id: {promoted,
  per_population: {pop: {predicted, measured_pass}}, fidelity_ok}}` — a
  POST-SESSION projection, booleans only, written into
  `attacks/<case>/rejected_proposal.json` as `projection_matrix` by
  `verification/attacks._record_rejected_proposal` and rendered by
  the `audit list` subcommand (since removed); NEVER a tool of any manifest
  (`POST_SESSION_PROJECTION_NAMES`: `project_proposal_matrix`,
  `proposal_matrix`, `predict_check`, `mutant_applicable`).
* The seat's session state and policy: `CriticSession(*, task, role, gold=None,
  workspace=None)` (`install_payload(payload) -> findings` through the
  council's own `_parse_findings`, `finding_at`, `current_draft`,
  `context(root) -> CriticToolContext`), `declared_validators(role,
  block=None)`, `critic_registry(role, block=None)` (the UNGATED registry a
  pilot session dispatches through), `critic_limits(role, block=None,
  agents_config=None)` (the block VERBATIM: a critic block is enforced as
  declared, never folded), `critic_policy(role, limits=None, session_salt=0,
  agents_config=None)` (submit `report_findings`, `abort_tool ""` — no abort
  vocabulary, abstention is an empty findings list — mode
  `harness_validated`, the one-shot wire) and `critic_validator_worker()`.
* Why nothing moves while the flags are off: `registry.
  _DECLARED_VALIDATOR_ROLES` (POP, SHC) is kept apart from `_DECLARED_ROLES`
  (the bounded-runner roles); `_declared_validators(role)` enters
  `ToolRegistry.for_role` only while the seat's block is enabled
  (`_session_enabled`); `declared_for_role(POP / SHC)` stays `()` because
  `test_manifest_per_role_matches_matrix_table` pins it; `harness_only`
  keeps every validator off the wire even when enabled, so
  `providers.session_tool_choice` keeps the forced `report_findings`;
  `providers._harness_validators_for` names the ACTIVE validators
  (`SessionLimits.active_harness_validators`: the declared list plus
  `measured_match_bit` under its flag) only while `limits.enabled`;
  `metrology.HARNESS_VALIDATOR_MODULES` / `HARNESS_VALIDATOR_BINARIES` gained
  `elt_taskgen.review.tools.critic_validators` and the `measured_match_bit`
  entry, so an ENABLED seat's `validator_digests` hash the validator code
  and pin `duckdb` / `sqlglot`, and the flag enters
  `critic_wired_validators` and the fingerprint
  (`test_measured_match_bit_enters_fingerprint_when_enabled`). Under the
  shipped `enabled: false`: `ToolRegistry.for_role(POP / SHC) == ()`,
  `role_behavior_manifest(POP)["tools"] == [report_findings]` with
  `harness_validators []`, `validator_digests == {"code": {}, "binaries":
  {}}`, and `council_routing_fingerprint()`, every critic manifest and
  `tool_surface_sha256` equal the values computed with the validators absent
  from the registry; enabling either seat moves both (the Phase 4 re-earn).
  What this does NOT say (review finding 1-0): the declared BLOCK is
  hashed verbatim, validator NAME included, so the POP / SHC manifests
  moved against the Phase 0 end state the moment the blocks were declared
  — see §2, "the POP / SHC digests moved once".
* `compile_proposal`'s claim-fidelity step (review finding 3-0) is guarded
  exactly as the promoter guards it: a proposed kind with no default `''`
  variant and no operation (`wrong_agg_stage`, a bare `custom`) compiles to
  `directive:kind:<kind>` and `validate_proposal_claim_fidelity` raises
  inside `split_kind_directive`; that is a red Diagnostic (`uncompilable`,
  flag `kind_operation_mismatch`, `claim_fidelity_ok: false`), never a
  session-ending `ToolHarnessFault`. `CriticSession.finding_checks` keeps
  the per-finding diagnostics of the last installed payload. Pinned by
  `test_compile_proposal_kind_without_default_variant_is_uncompilable_not_a_harness_fault`.
* Correction exhaustion is recorded and screened (review finding 2-3): a
  payload accepted as submitted while a validator or hook is still red
  carries `SessionResult.red_validators_at_submit` (names), the terminal
  turn's `outcome_code = validator_red_at_submit`
  (`session.VALIDATOR_RED_AT_SUBMIT_CODE`) and the SoT T8 counter
  `submitted_with_red_validators` on the session evidence row (the one-shot
  row is byte-identical). The POST-SESSION step
  `critic_validators.void_uncompilable_proposals(findings, session,
  result)` VOIDs every finding whose `proposed_case` the harness already
  compiled red (`FindingScreenStatus.VOID`, signal
  `uncompilable_after_corrections`, words verbatim, executable content
  withheld — `council._void`'s shape), so `cli._blocking_proposal_failures`
  skips it and a critic's own grammar failure reads as no finding (SoT T4),
  never as an attack-stage FAIL on the SPECIFICATION route that pays a
  proposer session and spends the task's round. `council.screen_findings`
  stays untouched (roadmap Table 7): the Phase 4 wiring (or a pilot) applies
  the step to `_parse_findings`'s output before the screen. Pinned by
  `test_uncompilable_major_proposal_after_correction_exhaustion_does_not_spend_a_round`.

Pinned by tests/test_review_tools_critic_validators.py:
`test_mutant_probe_never_returns_key_text_or_duplicate_bit`,
`test_compile_projection_never_surfaces_inert_or_inapplicable_sentences`,
`test_compile_projection_never_echoes_screen_lexicon`,
`test_compile_probe_emits_problem_only_when_no_probe_compiles`,
`test_compile_proposal_reports_grammar_codes_and_public_identifiers_only`,
`test_pre_flight_never_calls_materialize_mutation`,
`test_critic_session_compile_correction_carries_the_diagnostic`,
`test_proposal_matrix_is_post_session_and_never_a_tool_in_any_manifest`,
`test_population_adversary_manifest_has_no_proposal_matrix_verb`,
`test_rejected_proposal_json_carries_booleans_not_rewards`,
`test_measured_match_bit_is_off_by_default_and_counts_one_oracle_bit`,
`test_measured_match_bit_enters_fingerprint_when_enabled`,
`test_critic_validators_stay_off_the_registry_and_the_wire_while_disabled`;
by tests/test_council_efficacy.py
`test_disabled_critic_tools_do_not_enter_the_wire_manifest_or_fingerprint`
(with `test_fingerprint_unchanged_by_non_critic_projection_version` covering
the projection-version bump) and tests/test_providers_session.py
`test_harness_validated_critic_policy_keeps_the_forced_tool_choice`;
`test_a_rephrased_retraction_is_missed_by_design` (tests/test_council_screen.py),
tests/test_attack_promotion.py (19) and tests/test_attack_fidelity.py (7)
untouched and green.

### Item 3 — the correction channel (`review/session.py`)

* `ValidatorHook = Callable[[TaskIR, Mapping[str, Any]], Diagnostic | None]`
  and `run_bounded_session(..., payload_validators: Sequence[ValidatorHook]
  = ())` (passed through `RoutedProvider.run_session`'s `**runner_kwargs`):
  a caller-supplied, TASK-AWARE hook run on every submitted payload after
  the schema check (`providers.validate_payload_for` never reads the task);
  `_Session._run_payload_validators` records every run as a validator turn
  (`validator_run_count`, `fresh`), serializes a red `Diagnostic` with
  `serialize_for_transport`, re-checks it with the D1 gatekeeper
  `assert_value_free` and delivers it by the projection's own `render()` on
  a compile-kind correction — the correction turn carries a Diagnostic, never
  `CORRECTION_TEXT` plus a problem sentence. A hook that raises or returns
  anything but a Diagnostic is a `ToolHarnessFault` (C7: exit 2, reward
  `None`), pinned by `test_payload_validator_hook_faults_are_harness_faults`.
* `session.SCHEMA_RETRIES = PROTOCOL_FAULT_LIMIT - 1`, pinned equal to
  `providers.SCHEMA_RETRIES` (2): the runner's
  `_compile_correction_available` allows a compile correction only while
  `compile_corrections < max_compile_corrections` AND `correction_count <
  SCHEMA_RETRIES` (SoT T1.1 "schema + compile corrections share the 2"), so
  a red payload past either bound is accepted as submitted and screened
  post-session as today. Implemented as a count bound rather than a
  last-turn guard because tests/test_author_session.py pins a last-turn
  compile correction ending in `LIMIT_TURNS` for the author (Phase 1
  preserved). Pinned by `test_semantic_corrections_share_schema_retries_budget`.
* `correction_kinds: {schema: n, compile: n}` on `SessionResult` and its
  evidence row (SoT T8), `SessionLimits.measured_match_bit` and
  `.active_harness_validators`, `MEASURED_MATCH_BIT_VALIDATOR`.
* The runner skips a harness validator whose `per_session` cap is spent
  (the match bit's at-most-one call), counts validator runs in
  `per_tool_calls`, and `step()` resolves the abort terminal only for a
  non-empty name (`if name and name == abort_tool`) because a critic policy
  has `abort_tool=""`.

### Item 4 — untouched, and `audit list`

`council.run_council`, `_parse_findings`, `screen_findings` and the FATAL
leak short-circuit are byte-identical; `attacks.promote_proposed_cases` and
`cli.run_attack_stage` are unchanged. `verification/attacks.
_record_rejected_proposal` adds the `projection_matrix` key beside the
Phase 0 `projection`. `cli.cmd_audit_list` lists a task whose `attacks/`
tree holds a `rejected_proposal.json` bound to its CURRENT hash and renders
one `REJECTED PROPOSAL (not a sign-off item; the post-session matrix
projection for humans)` line per finding (`_rejected_proposal_matrices`,
`_rejected_proposal_matrix_lines`: the case, the finding, the `promoted` and
`fidelity_ok` booleans and, per population, the adversary's own prediction
beside the measured pass boolean — never a reward, never a reason sentence);
a record written before the field existed is rendered from its `projection`
sibling. (The audit queue and triage that rendered it were removed with the
audit stage; this finding is a historical record.)

### Item 5 — `config/agents.yaml`

The POP and SHC blocks of SoT T1.1 with `enabled: false` (POP: `mode:
harness_validated`, `harness_validators: [compile_proposal]`,
`max_model_calls: 3`, `max_compile_corrections: 1`, `max_tool_calls: 0`,
`max_wall_s: 300`, `max_usd: 1.00`, `max_oracle_bits: 6`,
`measured_match_bit: false`; SHC the same with `[compile_probe]` and no
flag); `repair.routes_bounded: [specification, reference, population]`;
`repair.certify.attack_enabled: false` (`repair.certify.deadline_s`
optional). `providers.DEFAULT_ROUTING_DOC` mirrors the `repair` block
(`test_default_routing_doc_matches_agents_yaml`); the `routes_bounded` pin in
tests/test_bounded_session.py
`test_agents_yaml_session_defaults_never_exceed_hard_caps` is re-pinned to
the Phase 3 value. A critic block is hashed VERBATIM whether or not it is
enabled (production = metrology block, R0.2), so DECLARING the two blocks
moved the two seats' behaviour digests once (§2), and editing either block
— including deleting the `measured_match_bit` key — moves them again and
stales any admission; flipping `enabled` is the Phase 4 re-earn. The yaml
comment above the POP block says exactly this (review finding 1-1: the
validator NAME is in the hashed block, the validator CODE is not).

### Item 6 — the records

`docs/experiments/PILOT-P4.md` (the B0 versus B1 contrast, the bit budget,
the echo-rate veto 0.05, the F2 decision, the four arms under their
canonical names as implemented, arm A's digests read from the code and the
B0 / B1 / C digests computed from a pilot document with exactly the stated
keys flipped); this log; `docs/INTERFACES.md` entries for
`critic_validators.py`, the `payload_validators` hook, the POPULATION route
and `certify.attack_enabled`; the README's mention of the flags;
`docs/plans/bounded_agents_phase3.md` joins `CURRENT_DOCS` in
tests/test_docs_consistency.py, which also pins the Phase 3 flags' shipped
values and the P4 file's canonical names and arm-A digests.

## 2. Sanctioned behaviour changes

The brief's rule is that every existing path stays byte-identical unless
the roadmap sanctions the change. These are the observable changes, each
with its authority and the test that pins it.

* **`rejected_proposal.json` gains `projection_matrix`** on every promotion
  rejection, flags off or on (roadmap Table 7 "Critic validators": "written
  to `rejected_proposal.json` and read by `audit list`"). Booleans only.
  Pinned by `test_rejected_proposal_json_carries_booleans_not_rewards`.
* **`audit list` lists a task with a rejected proposal at the current hash**
  (an extra non-sign-off line; the empty-queue sentence is unchanged).
  Roadmap Table 7 "Untouched" row. Pinned by
  `test_audit_list_renders_the_rejected_proposal_matrix`.
* **`repair.routes_bounded` gains `population`**: under the CLI opt-in
  `--repair-proposer-mode bounded` a POPULATION failure runs a session
  (item 1); the default `one_shot` path is byte-identical
  (`test_one_shot_proposer_mode_is_byte_identical_to_today`). Roadmap Table 7
  "POPULATION route" and "`config/agents.yaml`" rows. The Phase 1 pin
  `test_population_route_is_refused_in_phase_1` is replaced by
  `test_population_route_is_enabled_by_routes_bounded` (review finding
  1-2): the shipped tuple runs a POPULATION session that commits through
  `attempt_patch`, and the refusal the old pin proved — no session, no
  transcript, no adjudication, the workspace unchanged — is now proven on
  the documented rollback (a document whose `routes_bounded` omits
  `population`), and RUNTIME / FATAL stay `not_applicable`.
* **`projection.DIAGNOSTICS_VERSION` is "2"** (SoT T7; roadmap "Critic-flag
  discipline" row): the Phase 3 projections — the `certify` attack members
  and the live `discrimination_weakened` bit, the POPULATION cheap codes,
  the critic compile projections, the post-session `projection_matrix`
  record — bump the recorded version. It is recorded in evidence rows and
  transcript route blocks and never compared or hashed (R-H), so no
  transcript is orphaned and neither the tool surface nor
  `council_routing_fingerprint` moves
  (`test_fingerprint_unchanged_by_non_critic_projection_version`); a
  session record written under "1" would be refused by the gatekeeper on
  replay (`diagnostics_version_mismatch`) — none is committed.
* **The POP / SHC digests moved once** (review finding 1-0; SANCTIONED
  here). A critic block is hashed verbatim (R0.2), so declaring the SoT
  T1.1 blocks — `mode: harness_validated`, `harness_validators`,
  `max_compile_corrections`, `max_oracle_bits`, `measured_match_bit` — in
  place of the Phase 0 one-shot blocks moved, with every Phase 3 flag off:

  | Quantity | Phase 0 / Phase 2 end state | Phase 3 (this tree) |
  |---|---|---|
  | `council_routing_fingerprint` | `5a1989472c3137b4…` | `5f817b1faac1c214…` |
  | `role_behavior_sha256(population_adversary)` | `3c0176d25460a983…` | `25d6642e96088612…` |
  | `role_behavior_sha256(shortcut_attacker)` | `37c07b0fd127b41e…` | `53a6b9fabec28ce0…` |
  | `policy_sha256` POP / SHC | `18b31457d86db46b…` / `4802bbed0cb083cb…` | `bb49097556cf9839…` / `9e93549a3704abbe…` |
  | one-shot transcript key POP / SHC (same prompt) | moved | moved |
  | `tool_surface_sha256` | `a387488579fefb38…` | `a387488579fefb38…` (unchanged) |
  | `ambiguity_critic`, `feasibility_reviewer`, `repair_proposer` digests | unchanged | unchanged |

  The fingerprint document literally carries `harness_validators:
  [compile_proposal]` in POP's `loop_limits` while the manifest's active
  `harness_validators` is `[]` and `validator_digests` is empty: the NAME is
  a declared limit, the CODE enters only when enabled. Consequences: (a) any
  POP / SHC one-shot transcript recorded between 0.E and Phase 3 is
  orphaned under `--replay-only` (the committed fixtures under
  `tests/fixtures/transcripts/population_adversary/` and
  `shortcut_attacker/` are empty, so no committed evidence was re-keyed;
  the drive stores are the owner's); (b) the schema-4 harness-5 re-earn,
  still owed since 0.E, must be run at or after this tree — a record
  earned between 0.E and Phase 3 would read STALE against this fingerprint
  (the only committed record, `council/state/council.live_admitted`, is
  schema 2 / harness 4 and was already refused, so nothing live was
  staled). The values are pinned as LITERALS by
  `test_phase3_critic_digests_and_fingerprint_are_pinned`
  (tests/test_council_efficacy.py), which also proves the move is exactly
  the two declarations (restoring the one-shot blocks returns the Phase 0
  values byte for byte); the next phase cannot move a critic seat's
  admission inputs without re-pinning them in its own change log.
* **`_revalidate` no longer answers red for a runner that COULD NOT
  MEASURE, or for a failed currency prerequisite** (review findings 2-0,
  2-4; C7, C4; narrowed by review finding (pop) 1-0): a runner exception of
  a could-not-measure CLASS (`certify.COULD_NOT_MEASURE_EXCEPTION_NAMES`:
  the OS, the storage engines' operational and fatal classes, the engine's
  own errors; plus every session fault and engine infrastructure class)
  halts as `InfrastructureFailure` under the marker `ToolHarnessFault`, and
  a non-PASS `review` prepended for `attack` on the REFERENCE / POPULATION
  routes halts as `StageBlocked` (`blocked_on:prerequisite_not_current_review`).
  Before the first round, both were
  `RevalidationFailed(revalidation_red_<stage>)`: a second paid attempt,
  adjudication and a spent round on the one-shot path, a misleading
  `RejectionCode` in the next session's view on the bounded path. Observable
  on the one-shot path too — the Phase 0 sanctioned change "`_revalidate`
  re-raises harness faults" extended to the faults the engine has no name
  for. Every OTHER raising runner — a defect the patch produced — stays
  `RevalidationFailed(revalidation_red_<stage>)` exactly as the un-split
  certifier answered it (the first round's "anything else is
  infrastructure" over-reached: it handed the model a free, unscored abort
  by breaking the bytes it edits; the in-session `certify` answers
  `certify_red_<stage>` for the same exception, the bounded `_submit`
  `revalidation_red_unknown` for a certifier exception no rejection class
  named, and the live ladder's FAIL row is the same verdict). Pinned by
  `test_revalidate_runner_raising_oserror_is_infrastructure_not_red`,
  `test_population_patch_is_not_rejected_by_a_live_review_verdict`,
  `test_certify_model_caused_runner_exception_is_red_not_a_halt`,
  `test_model_caused_certifier_exception_is_a_rejection_not_a_halt`.
* **The POPULATION view withholds a hidden population's private condition
  lines** (review findings (pop) 0-0, 0-2; §1 item 1): a hidden
  population's condition carrying a standalone number other than its
  declared scale, a count vector, a key tuple, a private scalar, a realized
  count, a path, a secret or a measured value prints as `[withheld:
  private material]`; `read_field` withholds it and a replace / delete
  anchor over it answers one code. Observable on the one-shot POPULATION
  proposer too (the one-shot view is `view_for_route`): the demo's `C11` /
  `C12` lines and a generated task's `COVERAGE SHORTFALL` notes leave the
  one-shot prompt as well, so the one-shot POPULATION transcript key moves
  for such tasks (no committed one-shot POPULATION proposer fixture exists;
  the drive stores are the owner's). `council.render_view` (the adversary's
  view, `metrology.view_digest`) is byte-identical. Pinned by
  `test_population_view_withholds_hidden_realized_counts_and_literal_values`.
* **`witness_problem_codes` reads the schema, never the hidden rows**
  (review finding (pop) 0-1; §1 item 1): `check_population_cheap`'s witness
  flags are invariant under the content of `literal_rows`. On the demo
  (no witness prose) nothing observable moves; on a generated task a
  witness whose discriminating column the schema names is now
  constructible whatever the rows carry. Pinned by
  `test_check_population_cheap_is_invariant_under_literal_row_content`;
  `test_witness_problem_codes_name_only_public_shapes` unchanged and green.
* **The `attack` member is dropped, the call is not refused, when a critic
  view moved** (review finding (pop) 1-1; §1 item 1): under
  `repair.certify.attack_enabled` a `conditions` edit followed by `certify`
  runs the Phase 1 members (6 bits) with `model_stages_deferred=True`
  instead of answering `certify_refused_review_view_changed` at no cost;
  the code survives for an empty stage set. Latent while the flag ships
  `false`. Re-pinned by
  `test_certify_attack_member_refused_when_critic_view_changed` and
  `test_second_certify_after_attack_rerun_does_not_raise_scope_violation`.
* **A flagged document with a Phase 1 budget refuses to start** (review
  finding (pop) 1-2): `AgenticRepairProposer(...)` raises `ValueError`
  when `attack_enabled` is on and the proposer block declares fewer than
  `max_certify × 6` oracle bits or a wall below `max_certify × the certify
  deadline`. Latent while the flag ships `false` (the shipped block passes
  with the flag off). Pinned by
  `test_attack_flag_requires_the_declared_oracle_and_wall_budget`.
* **The two re-keyed transcript fixtures record `diagnostics_version: "2"`**
  (the bump above; hard rule 3's mechanism): re-produced by
  `tools/migrate_transcript_fixtures.py` from the byte-identical legacy
  originals — same keys, same responses, only the recorded-not-hashed
  version field moved — so `test_migration_is_deterministic_and_rekeys_only_provable_entries`
  reads green again; the live re-record stays owed (§5 item 3).
* **A POPULATION `conditions` / `scale` patch over a measured baseline
  re-runs `reference` + `attack` and passes the discrimination guard**
  (review finding 2-1; roadmap §6 "behind the discrimination guard"): a
  weakened matrix can no longer commit through a `conditions` edit
  (`test_population_conditions_patch_reruns_attack_and_runs_the_guard`).
  Unchanged: a literal-rows patch (always armed, fail closed) and a
  `conditions` repair with no baseline (`test_in_scope_population_patch_commits`).
* **The proposer view tripwire is a `DiagnosticTripwire`** (review finding
  2-2): a leaking view halts both proposers with no round and no
  adjudication where it used to be read as a failed proposal
  (`test_view_leak_tripwire_halts_without_spending_a_round`;
  `test_paraphrased_reference_in_evidence_raises` still holds — the class is
  a `RuntimeError`).
* **`failure_detail` drops the POPULATION route's `proposals` key** (review
  finding 0-0): the promoter's verdict codes are post-session only. The
  Phase 0 pin in `test_attack_payload_never_reaches_specification_route_raw`
  is re-pinned; `test_population_failure_detail_never_carries_promoter_verdict_codes`
  pins the absence.
* **A red payload accepted past the correction budget is recorded**
  (review finding 2-3): terminal `outcome_code = validator_red_at_submit`,
  `SessionResult.red_validators_at_submit`, evidence-row
  `submitted_with_red_validators` (session rows only). Latent for every
  shipped path (no validator runs in a disabled session).
* **`compile_proposal` never raises on a bad kind** (review finding 3-0);
  latent while the seats are disabled.
* **`registry._registered_names` includes the declared critic validators**
  (`measured_match_bit` only under its flag): a model of another session
  role naming `compile_proposal` or `compile_probe` gets `tool_not_permitted`
  (a violation) rather than `unknown_tool` (a correction). Observable only
  inside an enabled session; every session ships disabled.
* **A halt raised during submit-time validation closes the model turn**
  (`_Session._close_pending_model_turn`): before, a harness fault or tripwire
  from a registry validator or a hook reached `_attach` before the turn had
  a category record and `result()` raised the SoT T5 identity `ValueError`
  instead of the fault. Now the turn is recorded as a terminal carrying the
  fault code. Latent for every shipped path (no validator runs in a disabled
  session). Pinned by `test_payload_validator_hook_faults_are_harness_faults`.
* **`providers.DEFAULT_ROUTING_DOC["repair"]` mirrors the yaml** (population
  in `routes_bounded`, `certify.attack_enabled: false`);
  `metrology.HARNESS_VALIDATOR_MODULES` / `HARNESS_VALIDATOR_BINARIES` name
  the three validators, so `critic_wired_validators` accepts them — nothing
  moves while disabled (PROOF 5 holds).
* **No VALIDATOR entered any critic seat's wire, manifest
  `harness_validators`, `validator_digests` or the tool surface**
  (`test_disabled_critic_tools_do_not_enter_the_wire_manifest_or_fingerprint`);
  the transcript keys and admission inputs of POP and SHC DID move through
  their declared blocks (the "digests moved once" item above — the earlier
  wording of this entry, "no transcript key and no admission input moved
  for any critic seat", was false for those two seats; review finding 1-1).
  `role_behavior_sha256("repair_proposer")` is unchanged by the unflagged
  `CertifyTool` (its description and cost move only in the flagged instance,
  which only a session with `attack_enabled` builds).
* **A locator's list index has ONE spelling** (re-check verdict 1, §6):
  `read_field.field` / `apply_edit_trial.locator` refuse every alias of an
  unsigned canonical integer (`-1`, `+1`, `01`, ` 1`, `1_0`) on the wire
  schema, at the runner's PERMIT (`ForbiddenArgument(non_canonical_index)`,
  outranking the schema like the population rule, no worker spawned) and
  in every resolver; the withheld-condition guard decides on the RESOLVED
  `(index, slot)` pair. Observable only inside a bounded proposer session
  (its `policy_sha256` / `tools_sha256` moved with the tightened schemas
  and the `certify` description — the proposer's own session records are
  re-keyed harmlessly, as the flagged tool already did); the one-shot
  proposer's `apply_patch_text` now answers `patch_anchor_not_found` for an
  aliased locator where `int()` used to resolve it, and no committed
  transcript, `role_behavior_sha256`, critic digest, `tool_surface_sha256`
  or fingerprint moved. Pinned by
  `test_locator_list_index_must_be_a_canonical_unsigned_integer`,
  `test_withheld_condition_guard_decides_on_the_resolved_pair` and
  `test_non_canonical_locator_index_is_forbidden_argument_at_permit_without_a_worker`.
* **A resource-cap exhaustion the edit caused is the PAID
  `certify_refused_resource_budget`** (re-check verdict 2, §6): DuckDB out
  of memory under `CERTIFY_MEMORY_LIMIT_MB`, an interrupted query or the
  worker deadline on a stage the edit FEEDS answers a scored code (2 bits,
  one of the two executed certifies) where it used to halt as
  `ToolHarnessFault` / `ToolDeadlineExceeded`; on any other stage, in the
  pre-stage window, for the RSS envelope, a worker death and every class of
  the unchanged `COULD_NOT_MEASURE_EXCEPTION_NAMES` the halt stands. Latent
  for every shipped path (a session runs only under `--repair-proposer-mode
  bounded`); `projection.CERTIFY_CODES` gained the code (the vocabulary is
  recorded, never hashed). Re-pinned:
  `test_certify_deadline_is_harness_fault_not_policy`,
  `test_certify_deadline_cancels_the_worker_and_never_resurrects_the_copy`,
  `test_certify_deadline_kills_the_worker_process_group_before_teardown`
  (their session halves stalled the stage the edit feeds); pinned by
  `test_certify_resource_budget_exhausted_by_the_edit_is_a_paid_outcome_not_a_halt`,
  `test_certify_resource_fault_on_an_untouched_stage_stays_a_harness_fault`
  and `test_resource_budget_exhaustion_in_session_certify_is_scored_not_a_halt`.

## 3. What stays default-off, and why

| Key (`config/agents.yaml`) | Shipped | What it gates | Why off | Rollback |
|---|---|---|---|---|
| `roles.population_adversary.session.enabled` | `false` | `compile_proposal` in the registry, the manifest's `harness_validators`, the validator digests in the surface | roadmap R-A: a critic seat flips only with the Phase 4 fresh-live re-earn under `HARNESS_VERSION` "6", after P4 decides B0 / B1 | the key itself; flipping it false again re-keys nothing beyond what the declared block already keys (§2 "digests moved once") |
| `roles.shortcut_attacker.session.enabled` | `false` | `compile_probe` likewise | same | same |
| `roles.population_adversary.session.measured_match_bit` | `false` | the 1-bit gold-bearing `measured_match_bit` (`max_oracle_bits` 7) | A24: an oracle bit is spent only if P4 shows B1 adds at least 0.10 over B0 with echo rate at most 0.05 and zero tripwire hits | the key itself (DELETING it moves the hashed block and the pinned digests: keep it declared false) |
| `repair.routes_bounded` | `[specification, reference, population]` | the bounded proposer's POPULATION sessions (only under `--repair-proposer-mode bounded`) | the CLI default `one_shot` keeps today's proposer byte for byte | the list without `population` |
| `repair.certify.attack_enabled` | `false` | the `attack` member of the in-session `certify` (6 bits, 1,200 s, the member dropped when a critic view moved, the live `discrimination_weakened` bit) | certify addendum §4.8 Q6: the cap (1 or 2 members) is decided on P4 and P6 wall data; a pilot arm that turns it on MUST also declare `max_oracle_bits: 12` and `wall_clock_s: 2700` in the proposer block — `validate_attack_flag_limits` refuses to start otherwise (review finding (pop) 1-2) | the key itself: the Phase 1 stage set returns |

## 4. Engineering follow-ups (owed, by owner)

1. DONE (2026-09-04): item 1's roadmap tests —
   `test_population_route_read_field_excludes_literal_rows`,
   `test_population_patch_commits_only_when_discrimination_matrix_is_superset`,
   `test_regenerate_after_population_patch_rederives_and_byte_compares`,
   `test_realized_row_count_never_appears_in_any_turn`,
   `test_witness_problem_codes_name_only_public_shapes`,
   `test_population_session_certify_reruns_reference_and_attack_when_literal_rows_move`
   (behind the flag),
   `test_second_certify_after_attack_rerun_does_not_raise_scope_violation`
   (behind the flag), `test_certify_attack_member_refused_when_critic_view_changed`
   — join `test_population_conditions_patch_recalls_only_the_adversary_seat`
   in tests/test_repair_proposer_agentic.py, and the POPULATION corpus (23
   fixable cases plus the 9-case guard corpus) joins
   tests/test_repair_loop_planted.py, so the phase's definition of done (1)
   and (4) read green offline (§1 item 1). What the flagged tests exercise
   with fixture stage runners — the real `cli.run_attack_stage` on the copy
   (≈ 590–596 s on the demo) — is the owner's P6 arm under
   `repair.certify.attack_enabled`.
2. DONE (review finding 1-2): `test_population_route_is_refused_in_phase_1`
   is replaced by `test_population_route_is_enabled_by_routes_bounded`,
   which proves the session under the shipped tuple and the refusal on the
   documented rollback document (§2).
3. DONE (2026-09-04): `projection.DIAGNOSTICS_VERSION` is "2" (§2); no
   re-earn, nothing hashed moved.
4. **The registry gate reads the repository document.** `ToolRegistry.
   for_role` / `registry._session_enabled` read `providers.role_loop_limits(role)`
   with no `agents_config`, so a pilot document that enables a critic seat
   moves `role_behavior_manifest(role, agents_config=...)`,
   `metrology.validator_digests(agents_config=...)`, the surface and the
   fingerprint, but `ToolRegistry.for_role(role)` stays `()` and the
   manifest's per-role `validators` field stays `{}` under it. A pilot
   dispatches through `critic_validators.critic_registry(role, block)` (the
   ungated view) and the fingerprint folds the validator code through the
   surface, so the pilot is measurable; the Phase 4 flip of the REPOSITORY
   document needs no change. Making the registry gate document-aware is a
   Phase 4 item for the registry owner.
5. **Phase 4 wiring**: nothing runs a critic session in production
   (`council.run_council` is untouched, as required). The entry points a
   Phase 4 seat flip or the P4 pilot use are `critic_validators.CriticSession`,
   `critic_policy`, `critic_limits`, `critic_validator_worker` and
   `providers.RoutedProvider.run_session(role, view, policy, ctx, worker=...,
   payload_validators=...)`.
6. DONE (2026-09-04): `docs/experiments/README.md` now states that
   `PILOT-P2.md`, `PILOT-P3.md` and `PILOT-P4.md` are committed
   (pre-registrations, none run) and that P1, P5 and P6 are not.
7. **Phase 4 ENTRY item — the council-side entry for critic sessions.**
   No production caller runs a critic session in Phase 3 (item 4:
   `council.run_council`, `_parse_findings` and `screen_findings` are
   byte-identical), so the post-session step of §1 item 2 has no home yet.
   Phase 4 opens by adding `findings_from_session` to the council — the
   one entry that takes a `CriticSession`'s `SessionResult`, parses its
   payload through `_parse_findings` and applies
   `critic_validators.void_uncompilable_proposals` BEFORE `screen_findings`
   — and routing every enabled critic seat through it. Until that entry
   exists, flipping `roles.population_adversary.session.enabled` or
   `roles.shortcut_attacker.session.enabled` would run no session at all
   (the flags gate registries and manifests, not a caller), so the flip and
   the entry ship together with the harness-6 re-earn (§5 item 3).
8. **The coverage budget's realized-derived numbers (generation owner;
   review finding (pop) 0-0, second half).** `generation/populations.py::
   coverage_budget_scale` writes `budgeted for N rows` / `needs at least N
   rows` into a degraded population's conditions with N =
   `realized_row_count` of the parent (or that count × the enum domain),
   and sets the degraded child's DECLARED scale to the same product. The
   view gate withholds the notes (§1 item 1); the declared scale is the
   route's editable subject and is printed by design, so on such a task
   `facts~<realized parents × domain>` still names a realized count for
   anyone who divides by the public domain size. Owed to the generation
   owner (outside this change set's fence: `src/elt_taskgen/generation/`):
   state a shortfall in declared-scale or qualitative terms and choose a
   degraded scale that is not a product of a realized count (round to the
   declared grid), then drop the note lines from the withheld set.
9. **The population adversary's council view (Phase 4, with the re-earn).**
   `council._population_adversary_view` renders every condition verbatim
   (`_population_summary` without the hook) — the same bytes the proposer
   view now withholds. Its rendering is admission evidence
   (`metrology.view_digest`, the one-shot transcript key), so passing it
   through the same gate is a critic-seat change: it ships with the Phase 4
   flip and the harness-6 re-earn, not here. The permission matrix's
   NEVER-ACCESS row ("hidden population conditions, literal rows, realized
   counts") and trust-boundary 6.7 ("projections carry no numbers for graded
   populations") are, for the POPULATION repair route, realized as: the
   conditions ARE model-visible on this route (they are its editable
   subject), gated line by line as §1 item 1 states, with the declared
   scales shown and everything realized, counted or measured withheld.
10. **The certify addendum's "Faults" row.** Superseded by review finding
   (pop) 1-0 (§1 item 1, §2): "any other exception → `ToolHarnessFault`"
   is now "any other exception → the scored red verdict"; the addendum text
   in the design report is the owner's to amend.

## 5. Owner actions

These are the owner's; nothing here was run, and no live command, credential
or network call is part of this change set.

1. **Run pilot P4** as pre-registered in `docs/experiments/PILOT-P4.md`:
   fill the `TBD` cohort digest and seed, commit the `pilot_stats` module
   under `tools/` with its tests (`test_p4_echo_rate_veto` included), run the
   $0 dress rehearsal on the offline doubles until it recovers a planted
   effect, then the three arms A / B0 / B1 (and C under its recorded F2
   override) at $$K = 8$$ on 15 to 20 tasks with frozen gold through a
   pilot `RoutedProvider` — never `cmd_review` or `_admission_gate`, never a
   workspace under `runs/`. Adopt by §6 of that file; the echo-rate veto
   (0.05) and any tripwire hit reject an arm whatever its primary.
2. **Decide `measured_match_bit`.** It ships `false`. Set it `true` (with
   `max_oracle_bits: 7`) in the Phase 4 flipped block ONLY if P4's B1 rule
   holds ($$\bar d_{B1-B0} \ge 0.10$$, echo rate at most 0.05, zero tripwire
   hits); otherwise leave the key declared `false` — deleting it moves the
   hashed critic block and would itself be a re-earn.
3. **The re-earn.** The harness-5 fresh-live re-earn (Phase 0 log §3 item
   3, Phase 1 log §3 item 1) is still owed, and it must be run AT OR AFTER
   this tree: declaring the POP / SHC blocks moved the routing fingerprint
   to `5f817b1faac1c214…` (§2 "digests moved once"), so a schema-4 record
   earned between 0.E and Phase 3 would read STALE here. No record is
   staled today (the committed one is schema 2 / harness 4, refused since
   Phase 0), and the pinned literals in
   `test_phase3_critic_digests_and_fingerprint_are_pinned` are what the
   re-earn's record must name. The Phase 4 flip
   (`roles.population_adversary.session.enabled: true`, likewise the
   shortcut attacker) is admitted only by a further fresh live `elt-taskgen
   metrology` run under `HARNESS_VERSION` "6" whose fingerprint folds the
   validator CODE (`validator_digests`) and `validators.binaries` (`duckdb`
   1.5.5, `sqlglot` 30.16.0) beside the already-hashed block. Also still
   owed: the live fixture re-record, pilots P1, P6, P2 and P3 and the
   credential purge of the earlier logs.
5. **Phase 4 ENTRY item — `findings_from_session`, the council-side entry
   for critic sessions** (§1 item 2, review finding 2-3; §4 item 7): no
   production caller exists in Phase 3, so whoever wires a critic session
   into `run_council` (or the P4 pilot harness) adds the entry that applies
   `critic_validators.void_uncompilable_proposals` to `_parse_findings`'s
   output BEFORE `screen_findings`, so a proposal the harness compiled red
   at submit never reaches `_blocking_proposal_failures` as a MAJOR
   unresolved claim. It is the first Phase 4 item, ahead of the seat flip.
6. **The live ladder's raising-runner rule** (review finding 2-0, "consider
   the same wrap"): `Engine.run` still records a raising runner as a
   keyword-routed FAIL (`engine.py`, `except Exception` around the runner).
   Not changed here — it is a pre-existing one-shot path outside the repair
   proposer — and left to the engine owner as a Phase 4 decision. Review
   finding (pop) 1-0 narrowed what the certifiers do to the SAME split the
   ladder would need: a could-not-measure class
   (`certify.COULD_NOT_MEASURE_EXCEPTION_NAMES`) is infrastructure, every
   other runner exception the scored FAIL the ladder already records — so
   the engine-side wrap, if adopted, should reuse
   `certify.runner_exception_could_not_measure` rather than a second list.
4. **Decide `repair.certify.attack_enabled`** (certify addendum §4.8 Q6) on
   P4 and P6 wall data: each attack member is about 590 to 596 s on the
   demo, so `max_certify: 2` bounds in-session CPU to about 20 min and the
   arm that turns it on declares `max_oracle_bits: 12` and `wall_clock_s:
   2700` in the proposer block. The flag ships `false`.

## 6. Phase 3 re-check residuals, closed on 2026-09-04

The Phase 3 re-check left two verdicts open over the POPULATION route's
tools and the in-session `certify` (permission matrix Output 5 argument
rules; certify addendum §3.1; state machine Output 4 §2 "paid outcomes
versus harness faults"). Both are closed here, in
`review/tools/validators.py`, `review/repair_proposer.py`,
`review/tools/certify.py`, `review/session.py` (the locator argument
policy only) and, for the one code the vocabulary needed,
`review/tools/projection.py`.

### Verdict 1 — locator canonicalization

The problem. `_FIELD_PATH_RE` / `_LOCATOR_RE` admitted any
`[A-Za-z0-9_\-]+` segment, and every resolver read a list segment with
`int()`, so a locator could name a slot under an ALIAS the withheld guard
did not read: `populations.-1.conditions.2` matched
`condition_path_is_withheld`'s `\d+` nowhere (not withheld) while `int("-1")`
resolved it to the LAST population — on the demo the counterfactual whose
`C11` / `C12` lines the view withholds — so a `replace` anchor over it
answered `applied` for the true text and `patch_anchor_not_found` for an
absent one: a substring oracle over withheld material (open to the one-shot
proposer's `RepairEdit.locator` too, through `apply_patch_text`).
`1_0` (ten under `int()`), ` 1`, `+1` and `01` are the same class.

What shipped.

* `session.CANONICAL_INDEX_RE` (`0 | [1-9][0-9]*`),
  `session.canonical_list_index(segment)` — the ONE resolver rule — and
  `session.locator_argument_problem(value)`, which answers
  `NON_CANONICAL_INDEX_CODE` (`non_canonical_index`) for any segment
  `int()` would read as an index in a non-canonical spelling (a sign, a
  zero pad, whitespace, a digit-group underscore, a non-ASCII digit).
  `_forbidden_argument` applies it to the LOCATOR-typed arguments only
  (`_LOCATOR_ARG_NAMES`: `field`, `field_path`, `locator`; a load-plan path
  may hold `parts/01.parquet`), and `SCHEMA_OUTRANKING_ARGUMENT_RULES`
  (`population_argument`, `non_canonical_index`) names the PERMIT rules
  that outrank the schema check: an aliasing index is a probe, never a
  slip, so a strict wire pattern never turns it into a free correction and
  no worker is spawned (`ForbiddenArgument`, terminal `POLICY_VIOLATION`,
  a security event).
* `validators._FIELD_PATH_RE` = `_LOCATOR_RE` =
  `^[a-z][a-z0-9_]*(\.(?:[a-z][a-z0-9_]*|0|[1-9][0-9]*))*$` (one segment
  pattern `_PATH_SEGMENT_RE`), carried into the `read_field` and
  `apply_edit_trial` wire schemas; both tools answer the same
  `ForbiddenArgument` on a direct dispatch before the schema check;
  `validators._resolve` and `repair_proposer._json_get` / `_json_set` use
  `canonical_list_index` (an alias is `_MISSING` / `patch_anchor_not_found`,
  one code for a hit and a miss).
* `repair_proposer.resolved_condition_slot(path)` resolves
  `populations.<i>.conditions[.<j>]` through the same rule and
  `condition_path_is_withheld` — hence
  `ProposerSession.condition_is_withheld` — decides on the RESOLVED
  `(index, slot)` pair, so the guard and the resolvers can never disagree
  (an alias resolves nowhere for all three).

Pinned by tests/test_review_tools_validators.py
`test_locator_list_index_must_be_a_canonical_unsigned_integer` (the wire
schemas, the tool regex, the PERMIT rule with the schema outranked, the
direct dispatch, the three resolvers, and the real runner: the violation
before any worker) and `test_withheld_condition_guard_decides_on_the_resolved_pair`
(every alias of the demo's withheld counterfactual slot — negative,
zero-padded, signed, whitespace, the whole list — is what `int()` reached
before, resolves nowhere now, `apply_patch_text` answers ONE code for the
true text and an absent anchor, and the runner refuses it at PERMIT); and
tests/test_bounded_session.py
`test_non_canonical_locator_index_is_forbidden_argument_at_permit_without_a_worker`
(the runner alone: the violation, zero tool calls, the schema outranked,
the canonical spelling reaching the tool, the rule scoped to locators).

### Verdict 2 — resource-cap exhaustion the edit caused is a PAID outcome

The problem. Every `OperationalError` — DuckDB `OutOfMemoryException`
included — and every `InterruptException` sat in
`COULD_NOT_MEASURE_EXCEPTION_NAMES`, and the worker deadline was always
`ToolDeadlineExceeded`: a model could abort its session, the certifier and
the whole repair for free (reward `None`, no round, exit 2) by writing a
cross join into the SQL it edits — the deadline twin of review finding 1-0.

What shipped (`review/tools/certify.py`, module docstring "RESOURCE-CAP
EXHAUSTION THE EDIT CAUSED").

* `CODE_REFUSED_RESOURCE_BUDGET = "certify_refused_resource_budget"`, a
  member of `projection.CERTIFY_CODES` (so `project_certify` and
  `project_certify_attack` build it and the `Diagnostic` validator admits
  it), NEVER a member of `NO_COST_REFUSAL_CODES` /
  `ATTACK_NO_COST_REFUSAL_CODES`: a PAID outcome — the oracle bits charged
  at spawn stand (2, or 6 under the flag), the executed call counts toward
  `max_certify`, the runner charges its `cost.oracle_bits` like any measured
  outcome, and a `CertifyReceipt` is left with the session (recorded and
  replay-served like every executed certify).
* `RESOURCE_BUDGET_EXCEPTION_NAMES` = {`OutOfMemoryException`,
  `InterruptException`} (matched by name through the MRO, never chain-
  walked), `runner_exception_exhausted_resource_budget(exc)` and the rule
  `resource_budget_exhausted_by_edit(exc, stage=, touched_stages=)`: a
  resource-budget class raised by the runner of a stage the edit FEEDS —
  never a `SessionFault`, never an exception the engine already names
  infrastructure. `EDIT_TOUCHED_STAGES` / `edit_touched_stages(route)`:
  REFERENCE → (`reference`, `attack`) (the SQL is what the freeze and the
  matrix read), POPULATION → (`generate`, `reference`, `attack`)
  (conditions, scales and rows drive generation and everything after it),
  SPECIFICATION → () (prose feeds no provider-free stage).
  `ProposerSession.touched_stages()` hands the route's set to
  `certify_disposable_copy(..., touched_stages=)`; the default `()` keeps
  every direct caller's exhaustion a harness fault.
* `run_provider_free_stages(..., touched_stages=, on_stage=)` consults the
  rule BEFORE `classify_runner_exception` and answers the code; `on_stage`
  announces each member before it runs. The spawned worker sends
  `("stage", name)` progress messages and `_supervise_certify_worker`
  returns `("deadline", stage)` / `("memory", stage)` with the LAST stage
  that began (None before any); `_deadline_receipt` turns a deadline on a
  touched stage into the paid receipt (`resource_stage`, `worker_handle`:
  the killed process or the interrupted thread) in both workers, AFTER the
  kill-first / tear-down-second discipline ran unchanged.
* Unchanged, and pinned: `COULD_NOT_MEASURE_EXCEPTION_NAMES` (the literal
  frozenset), so on a stage the edit did not feed, in the pre-stage window
  (`engine.load_task`, the bootstrap deadline), for the whole-worker RSS
  envelope (`SandboxFault(memory_limit)`), a worker death and every OS /
  storage-engine / engine class the halt is what it was.

Pinned by tests/test_review_tools_validators.py
`test_certify_resource_budget_exhausted_by_the_edit_is_a_paid_outcome_not_a_halt`
(the rule's truth table; both workers through the tool — out of memory,
an interrupted query, the deadline — the seam, the flagged projector, the
supervisor's stage-naming verdict, and the bounded runner charging 2 bits
and submitting) and
`test_certify_resource_fault_on_an_untouched_stage_stays_a_harness_fault`
(the unchanged class list, `generate` on the REFERENCE route, direct calls,
the OS classes on a touched stage, the RSS / pre-stage / untouched
deadline verdicts, the progress protocol); by
tests/test_repair_proposer_agentic.py
`test_resource_budget_exhaustion_in_session_certify_is_scored_not_a_halt`
(a bounded REFERENCE session: the paid code is an ordinary tool turn, the
session submits and commits, no `InfrastructureFailure`, no marker, the
worker's text in no message and no record). Three Phase 1 pins were
RE-PINNED because their session halves stalled the very stage the edit
feeds (the direct-call halves, which name no touched stage, are unchanged):
`test_certify_deadline_is_harness_fault_not_policy` (now also proves the
untouched-stage deadline — `generate` stalled while `reference` is fed —
stays the fault),
`test_certify_deadline_cancels_the_worker_and_never_resurrects_the_copy`
and `test_certify_deadline_kills_the_worker_process_group_before_teardown`
(the bootstrap case, a deadline before any stage began, stays the fault).

### What moved, and what did not

* `proposer_policy().sha256()` / `.tools_sha256()` moved (the tightened
  `read_field` / `apply_edit_trial` schemas and the `certify` description
  naming the new code): the bounded proposer's own session records and
  transcript keys are re-keyed harmlessly, exactly as the flagged
  `CertifyTool` already did (§1 item 1). `role_behavior_sha256` of every
  role (the proposer's included: its session ships disabled, so its tools
  are not in its wire manifest), every critic digest, `tool_surface_sha256`
  (`a387488579fefb38…`) and the fingerprint are byte-identical; PROOF 5's
  inputs did not move.
* `projection.CERTIFY_CODES` gained one member; the literal pin in
  tests/test_review_tools_projection.py
  `test_certify_projection_returns_green_and_stage_only` was extended by
  that one line. `DIAGNOSTICS_VERSION` stays "2": the vocabulary is
  recorded, never hashed or compared (R-H), a record written under "2"
  before this change never carries the code, and a bump would re-produce
  the two migrated transcript fixtures (hard rule 3) for no replay gain —
  the owner may fold a bump into the Phase 4 re-earn if the SoT T7 reading
  demands one.
* Two files outside this change set's fence were touched, minimally and
  additively, because the closed vocabulary lives nowhere else:
  `review/tools/projection.py` (the one tuple entry and its comment) and
  the projection test's literal.

Records: this section; `docs/plans/bounded_agents_phase4.md` (the Phase 4
log) should cite it for the runner's `SCHEMA_OUTRANKING_ARGUMENT_RULES`
and the certify vocabulary.

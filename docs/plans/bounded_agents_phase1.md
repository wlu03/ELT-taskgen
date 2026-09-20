# Bounded agents, Phase 1 — change log

Status: SHIPPED in the working tree on 2026-09-03; full suite green
(`.venv/bin/python -m unittest discover -s tests`), `python
tools/prove_admission_integrity.py` prints `ALL PROOFS HELD`. The owner
actions in §3 are NOT done and gate the phase's definition of done (the
fresh-live re-earn, the live fixture re-record and the two pilots).

Phase 1 is §4 of the bounded-agents roadmap (the limits vocabulary addendum
T1–T9, the repair-certify addendum's Design H, the state machine, the tool
permission matrix and the trust boundary are its authorities), built on the
Phase 0 change log (`docs/plans/bounded_agents_phase0.md`). What ships is the
bounded runner and its wire layer (1.R), the harness-driven author session
(1.A), the F1 currency fix and the `trial_phase` / `_commit` split of the
certifier (1.P.0), the tool-using repair proposer with its provider-free
`certify` (1.P) and the planted repair corpus. Every session role ships with
`session.enabled: false` in `config/agents.yaml` and `--repair-proposer-mode
one_shot` is the CLI default, so every existing path stays byte-identical
except for the changes §2 lists, each with the authority that sanctioned it
and the test that pins it. Rollback of a session is a config key
(`session.enabled: false`, `--repair-proposer-mode one_shot`), never a code
revert. The Phase 1 review's remediation (findings 0-4, 1-1 to 1-4, 1-7, 2-0,
3-0, 3-1 and the session-defaults blocker) is folded into §1 and §2.

A separate, concurrent effort — the `training/` protocol proxy
(`training/airbyte_proxy.py`, `training/warehouse_profiles.py`,
`training/namespace.py`, the `training/scorer.py` and `training/local_sync.py`
cut-over) and the `export/` release schema 3.4 (`export/certification.py`,
`export/release.py`), with `docs/EXECUTION_MODEL.md`,
`docs/WAREHOUSE_CONNECTORS.md`, `runtime-images/`, the Makefile and the
packaging files — is present in the same tree, is OUTSIDE this roadmap and is
NOT covered by this log; the review's findings 1-5 and 1-6 belong to it and
are recorded by the plan that owns it (`docs/plans/cloud_free_elt_agent_rlvr.md`).

## 1. What shipped, per item

### 1.R — the runner and the wire layer

`review/session.py` grew from the Phase 0 fault classes into the bounded
runner: `run_bounded_session(role, initial_view, tools, policy, limits, *,
provider, ctx, worker, ...)` drives the INIT → OBSERVE → MODEL_TURN → PARSE →
PERMIT → RUN_TOOL / HARNESS_VALIDATE → SANITIZE → RECORD loop of the state
machine, `SessionLimits` (the role's `session:` block verbatim, the SoT T1
accessors, `hard_caps`, the `stuck:` override, `format_error_disposition`
read from the block or the document's top-level `session:` defaults —
un-hashed), `SessionPolicy` (tools, terminals, limits, the fixed nudge and
correction texts; `sha256()` is `policy_sha256`), `TerminalState` (the SoT
T4 vocabulary, `STUCK` serialising as `stuck_in_a_loop`), the stuck detector
R1–R5 (three identical `(action, observation)` pairs nudge, four halt; the
flagged call is never executed), `ToolOutcome` / `TurnRecord` /
`FaultRecord` / `SessionResult` with the hash chain (`session_sha256`,
`verify_chain`), `cap_tool_output` (8 KiB codes / 16 KiB rows, the full
observation digest chained), `validate_args` and the argument policy
(`_forbidden_argument`: denied trees, SQL policy, path-shaped values), the
harness-validated submit (`harness_validators`, `max_compile_corrections`),
the auto-submit of the last validator-green draft at a limit stop, the
PROVIDER_FAULT resume-once and SANDBOX_FAULT rebuild-once rules, and the D3
worker seam (`ValidatorWorker`, `WorkerResult`, `InProcessValidatorWorker`).
`review/providers.py` gained `RoutedProvider.run_session` / `_turn` (per-turn
`transcript_key_v3` memo serving, `entry_schema` 3 turn entries under
`SESSION_TRANSCRIPT_ENTRY_SCHEMA`, `session_policy_for`, `role_manifest_limits`,
`session_tool_choice`, `wire_tools_for`, `role_is_agentic`, the one
exchange-evidence row per (task, role, session) of SoT T8) and the
`ReserveAndTrajectoryBudget` per-turn `reserve` / `charge`.
`review/tools/registry.py` gained the enabled gate (`_session_enabled`,
`_role_gate`: a role's tools enter `for_role`, the wire and the manifest only
while its `session:` block is enabled), `permit_refusal` (per-tool caps and
the tool's own `permit` hook, answered at PERMIT with no worker spawned) and
`ToolCost.no_cost_codes`. Contract tests: the 52 tests of
tests/test_bounded_session.py (among them
`test_session_zero_tool_turns_is_byte_identical_to_complete`,
`test_scripted_tool_loop_records_one_transcript_entry_per_turn`,
`test_limits_produce_distinct_terminal_states`,
`test_three_protocol_faults_halt_via_provider_protocol_error`,
`test_output_truncated_halts_on_first_session_turn`,
`test_limit_stop_auto_submits_last_validator_green_draft`,
`test_permit_hook_refusal_spends_no_bits_and_no_worker`,
`test_capped_tool_result_binds_the_full_observation_digest`,
`test_policy_sha256_moves_with_runner_behaviour_fields`,
`test_trajectory_digest_is_independent_of_admission_stamp_and_cache_usage`,
`test_free_text_private_path_is_a_correction_not_a_violation`,
`test_format_error_disposition_reads_the_session_wide_default`),
tests/test_providers_session.py and tests/test_session.py.

### 1.A — the author session

`review/tools/validators.py` carries the author's five tools (`replace_prose`,
`check_prose`, `contamination_precheck` harness-only; `submit_prose` and
`abort` on the wire), `AuthorSession`, `author_limits` (the block plus the
DERIVED `harness_validators: [check_prose]` and `max_compile_corrections =
max_revisions`, now with the document's session-wide defaults),
`author_policy`, `author_validator_worker`, `author_session_enabled` (the
ONE rule: `enabled` AND `max_revisions > 0`) and the two projections
`project_prose_check` / `project_contamination_precheck`.
`review/council.py`'s `author_prose_session` runs the harness-driven loop
(every submitted draft checked once by `check_prose_fidelity`, a red draft a
revision request while revisions remain, the contamination precheck once on
the final draft), the CLI's author runner selects it behind
`cli._author_session_block` and persists the record at
`tasks/<id>/reports/sessions/`, and `config/agents.yaml` declares
`roles.semantic_author.session` with `enabled: false`. Contract tests:
tests/test_author_session.py.

### 1.P.0 — the F1 currency fix and the `trial_phase` / `_commit` split

`review/repair_proposer.py`: `attempt_patch` = `trial_phase` + `_commit`,
byte-identical on every task-defect path (`TrialVerdict`; a red verdict is
RAISED with the verdict attached). `_revalidate` records every member's
outcome on the TRIAL ledger at the new hash and prepends the WIRED currency
prerequisites a route's rerun set lacks (`CURRENCY_PREREQUISITES`: `review`
before `attack`, `attack` before the three gate batteries), so the real
`attack` and gates runners find the PASS rows they demand (F1, certify
addendum §0 and §3.6, form (i)). Inside `trial_phase` a runner exception
whose MRO hits `_INFRA_EXCEPTION_NAMES` or `SessionFault` is re-raised, a
nested plain `ProviderProtocolError` or another seat's role-cap trip is
re-raised as `InfrastructureFailure`, a member whose payload carries an
infrastructure marker raises `InfrastructureFailure`, and a member that
WAITED (`VERDICT_BLOCKED`) raises `engine.StageBlocked` under
`blocked_on:<reason>` (engine.py `StageBlocked`, `blocked_on_of`,
`blocked_stage_marker`, `blocked_on_from_marker`;
`Engine._halt_on_proposer_fault` puts the reason on the FAIL row). The
empirical `calibrate` runner is substituted by the structural one on the
trial (`_trial_stage_runners`, `CALIBRATE_EMPIRICAL_TAG`, which
`cli.make_calibrate_runner` sets and `build_stage_runners`' echo wrapper
carries through `functools.wraps`). Contract tests (tests/test_repair_proposer_agentic.py):
`test_attempt_patch_equals_trial_phase_plus_commit_byte_identical`,
`test_nested_transport_fault_inside_trial_phase_halts_without_spending_a_round`,
`test_blocked_stage_inside_trial_phase_halts_without_spending_a_round`,
`test_trial_phase_substitutes_structural_calibrate_for_empirical_runner`,
`test_trial_ledger_records_stage_rows_so_currency_checks_hold`,
`test_revalidate_with_real_attack_runner_requires_review_evidence_at_new_hash`,
`test_review_is_memo_served_on_trial_when_critic_views_unchanged`,
`test_population_conditions_patch_recalls_only_the_adversary_seat`,
`test_nested_budget_breach_inside_trial_phase_is_infrastructure_not_rejection`;
`test_stage_blocked_is_an_infrastructure_failure_carrying_the_reason`,
`test_halt_on_proposer_fault_puts_the_blocked_reason_on_the_row`
(tests/test_engine_repair.py).

### 1.P — the bounded repair proposer

`review/tools/validators.py` carries the proposer's eight tools (`read_view`,
`read_field`, `apply_edit_trial`, `check_scope`, `check_cheap`, `certify`,
`submit_patch`, `abort`), `ProposerSession` (ONE `trial_workspace` held open
as the editing surface, the live snapshot taken once at INIT as the
`validate_scope` baseline, the certify accounting), `ProposerToolContext`,
`proposer_policy`, `proposer_validator_worker`, `editable_field_paths` /
`field_is_editable` / `readable_field_paths` / `field_is_readable` /
`anchor_is_visible`, `_names_private_field` (literal rows, attack cases, the
compiler-only plan keys `marts.*.plan.ops.*.details.{name,select,sql}`, the
reference off the REFERENCE route, the populations off the POPULATION route
are a `ForbiddenArgument` on a read and on an edit alike),
`session_defaults_for`. `review/tools/certify.py` (new) is the provider-free
in-session certifier: `provider_free_stages`, `model_stages_deferred`,
`provider_free_stage_runners` + `assert_no_provider_handle`
(`provider_handles_in` walks closures, partials and bound instances),
`classify_runner_exception`, `run_provider_free_stages` (per member: run,
`save_task`, `record_report` on the COPY's ledger; a no-measure outcome is
`ToolHarnessFault(stage_could_not_measure)`, never red),
`certify_disposable_copy` in a SPAWNED, process-group-killed, RSS-watched
worker (`_certify_worker_main`, `_supervise_certify_worker`,
`_kill_worker_group`; `resolve_worker_kind`; `CERTIFY_WORKER_PROCESS` /
`CERTIFY_WORKER_THREAD`, the thread kept for an injected runner dict) with
`CertifyReceipt`; `NO_COST_REFUSAL_CODES` answered at PERMIT.
`review/tools/projection.py` gained `project_certify`, `CERTIFY_CODES`,
`RejectionCode` / `project_rejection`, `DiagnosticSource.CERTIFY` /
`REJECTION` / `FIELD` / `TRIAL` / `CHEAP`. `review/repair_proposer.py` gained
`AgenticRepairProposer` (the `RepairProposerLike` seam; at most
`repair.max_attempts` sessions per failure on `repair.routes_bounded`; INIT
reserves `session.max_usd + repair.nested_ceiling_usd[route]`;
`submit_patch` hands the accumulated patch to the UNCHANGED `attempt_patch`
exactly once under `SUBMIT_DEADLINE_S` through `_SupervisedEngine`; a
rejection travels to the next session's view as a `RejectionCode`
(`session_view`); a limit stop is `blocked_limit`, an abort queues
adjudication, a harness fault halts), `RepairSettings` / `repair_settings`,
`ProposerStep`, the session record (`session_record_path`, `_write_record`,
`_certify_entries_to_keep`) and its replay serving (`recorded_certify_results`,
`_ProposerWorker`: a served certify is verified against its RECORDED digest,
a mismatch is `ToolHarnessFault(replay_mismatch)`; `verify_tools` re-executes
and compares), `REPAIR_PROPOSER_MODES` / `DEFAULT_REPAIR_PROPOSER_MODE`.
`cli.py`: `--repair-proposer-mode {one_shot,bounded}` behind
`--repair-proposer` (`_make_repair_proposer`), `_audit_queue(...,
include_repair=True)` / `_pending_repair_adjudication` /
`_repair_adjudication_lines` and the `audit list` rendering.
`config/agents.yaml`: `roles.repair_proposer.session` (`enabled: false`,
`max_turns: 5`, `max_tool_calls: 8`, `max_certify: 2`, `max_oracle_bits: 4`,
`max_usd: 0.80`, `wall_clock_s: 900`, `certify: {attack_enabled: false,
deadline_s: 300}`) and `repair.routes_bounded` / `repair.nested_ceiling_usd`
/ `repair.max_usd_per_failure`. Contract tests
(tests/test_review_tools_validators.py):
`test_certify_refused_on_specification_route_costs_no_call_and_no_bits`,
`test_certify_refused_when_state_epoch_unchanged`,
`test_certify_worker_receives_no_provider_handle`,
`test_certify_runs_on_a_disposable_copy_and_the_held_trial_is_byte_unchanged`,
`test_certify_runner_exception_is_tool_harness_fault_not_red`,
`test_certify_blocked_stage_is_no_measure_not_red`,
`test_certify_declares_no_cost_refusal_codes_and_permit_hook`,
`test_certify_deadline_is_harness_fault_not_policy`,
`test_certify_is_capped_at_two_per_session`,
`test_certify_oracle_bits_two_per_call_and_cap_four`,
`test_certify_runs_only_generate_and_reference_in_phase_1`,
`test_read_field_refuses_literal_rows_even_on_population_route`,
`test_apply_edit_trial_refuses_a_second_artifact_in_one_session`,
`test_validate_scope_runs_on_every_apply_edit`,
`test_validate_scope_baseline_is_the_live_snapshot`,
`test_every_tool_result_passes_assert_result_clean_on_demo_task`,
`test_manifest_per_role_matches_matrix_table`,
`test_no_tool_accepts_a_population_argument`,
`test_no_tool_has_free_path_or_url_argument`,
`test_no_session_tool_makes_a_nested_model_call`,
`test_apply_edit_trial_answers_out_of_route_locators_before_the_anchor`,
`test_reference_session_edits_only_sql_by_mart`,
`test_certify_deadline_cancels_the_worker_and_never_resurrects_the_copy`,
`test_certify_deadline_interrupts_a_running_gold_query`,
`test_certify_deadline_kills_the_worker_process_group_before_teardown`,
`test_certify_process_worker_transports_typed_faults_only`,
`test_certify_worker_rss_watchdog_and_death_are_sandbox_faults`,
`test_apply_edit_trial_anchor_on_withheld_field_answers_one_code`,
`test_author_and_proposer_policies_carry_the_session_wide_defaults`;
(tests/test_repair_proposer_agentic.py)
`test_no_session_tool_makes_a_nested_model_call`,
`test_blocked_stage_inside_certifier_halts_bounded_session_without_a_round`,
`test_certify_result_is_recorded_and_replay_serves_it_without_reexecution`,
`test_submit_patch_calls_attempt_patch_exactly_once_with_the_accumulated_patch`,
`test_accumulated_patch_replays_to_held_trial_bytes`,
`test_revalidation_failed_payload_dump_never_appears_in_any_turn`,
`test_second_session_view_carries_rejection_code_not_retry_sentence`,
`test_population_route_is_refused_in_phase_1`,
`test_limit_stop_is_blocked_not_a_round`,
`test_abort_queues_adjudication_and_workspace_is_byte_identical`,
`test_session_init_reserves_turn_cap_plus_nested_ceiling`,
`test_cost_meter_episode_record_includes_nested_certification_spend`,
`test_one_shot_proposer_mode_is_byte_identical_to_today`,
`test_audit_list_renders_repair_adjudication`;
(tests/test_repair_proposer.py)
`test_validate_scope_refuses_verifier_inert_only_diff_as_noop`,
`test_write_record_never_overwrites_executed_certify_entries_with_served_ones`;
(tests/test_review_tools_projection.py) the `project_certify` and
`RejectionCode` tests.

### The planted corpus

tests/test_repair_loop_planted.py: 30 planted SPECIFICATION defects (a
review runner demanding one clarifying sentence each) and 10 planted
REFERENCE defects (a typo in the demo reference SQL a reference runner
refuses until restored), each driven by a scripted proposer double through
the REAL session runner, the real projections and proposer tools, the real
provider-free `certify` (REFERENCE) and the REAL `attempt_patch` at submit:
every fix commits exactly once with every `tool_result` a rendered
projection (`test_specification_corpus_commits_through_the_unchanged_certifier`,
`test_reference_corpus_commits_after_a_green_certify`), a planted
SPECIFICATION defect commits through `Engine.run` with the same ledger order
as the one-shot proposer
(`test_planted_specification_defect_commits_through_the_engine_exactly_as_today`),
and on planted-impossible failures the harness commits nothing and abstains
at least 90 % of the time (`test_abstains_on_impossible_planted_repair`, the
hack canary). The live committed-repair rate is pilot P6's (§3).

### The review remediation (this log's own findings)

* **0-4 — a served certify record is verified.** `_ProposerWorker.run`
  compares the sha256 of the served body with the RECORDED
  `observation_sha256` and raises `ToolHarnessFault(replay_mismatch)` on a
  mismatch (never a re-hash into consistency); the appended record keeps the
  recorded digest; `_write_record` (`_certify_entries_to_keep`) never
  overwrites a record's EXECUTED certify entries with merely served ones at
  the same `session_sha256`. Pinned by
  `test_certify_result_is_recorded_and_replay_serves_it_without_reexecution`
  and `test_write_record_never_overwrites_executed_certify_entries_with_served_ones`.
* **2-0 — the anchor is never a substring oracle.** `ApplyEditTrialTool`
  decides scope from the LOCATOR before `apply_patch_text` touches a byte
  (`field_is_editable`; an out-of-route field answers the same
  `scope_field_outside_allowlist` / `scope_route_mismatch` for a hit and a
  miss, the trial and the epoch unchanged), and `_names_private_field`
  includes the compiler-only plan keys `marts.*.plan.ops.*.details.{name,
  select, sql}` (reference SQL by another name) so naming one on any route
  is a `ForbiddenArgument` for a read and an edit alike. Pinned by
  `test_apply_edit_trial_answers_out_of_route_locators_before_the_anchor`.
* **3-0 — verifier-inert fields are outside every route** (see §2 for the
  one-shot consequence). `ROUTE_IR_PATHS[REFERENCE]` is
  `reference.sql_by_mart.*` alone; `VERIFIER_INERT_IR_PATHS` names the
  reference's `dialect`, `implementation_id`, `load_notes`, `provenance`,
  `version`; `validate_scope` refuses a task_ir.json diff whose moved fields
  are ALL verifier-inert as `patch_noop` before any allowlist is consulted;
  `apply_edit_trial` answers `patch_anchor_not_found` for BOTH anchor
  outcomes of a replace / delete on any editable field whose text the
  route's view does not show (`anchor_is_visible`: the prose, the reference
  SQL and the population conditions anchor; the title and the descriptions
  take `insert` only). Pinned by `test_reference_session_edits_only_sql_by_mart`,
  `test_validate_scope_refuses_verifier_inert_only_diff_as_noop`,
  `test_apply_edit_trial_anchor_on_withheld_field_answers_one_code`.
* **3-1 — certify in a spawned, killable worker.** `certify_disposable_copy`
  runs the production runner dict in a `multiprocessing` `spawn` child
  under its own process group (`os.setsid`), the semantic scorer's OS
  memory rlimits and a parent-side RSS watchdog
  (`CERTIFY_WORKER_RSS_LIMIT_MB = SemanticLimits().worker_rss_limit_mb`);
  the runner dict is built INSIDE the worker from `cli.run_generate` and
  `_with_execution_effect_filter(cli.run_reference_stage)`, so only the copy
  path, the task id and the stage names cross the boundary and only a
  `Diagnostic` dump or a typed fault descriptor crosses back (a closure is
  refused as `runner_not_transportable`). At the deadline (or an RSS breach)
  the supervisor SIGKILLs the whole process group, waits for it to end and
  only then removes the copy — `ToolDeadlineExceeded` /
  `SandboxFault(memory_limit)` surface with the worker dead and the copy
  gone; a worker that exits without a message is
  `SandboxFault(worker_failed)`. `certify_calls` and the two oracle bits are
  charged when the worker is spawned, so a deadline is a paid call. The
  in-process cancellable thread (interruptible DuckDB scope, `con.interrupt()`
  at the deadline, a grace, a reaper) is kept for an injected runner dict
  (`resolve_worker_kind`), which is what every fixture-runner test drives.
  Pinned by `test_certify_deadline_kills_the_worker_process_group_before_teardown`,
  `test_certify_process_worker_transports_typed_faults_only`,
  `test_certify_worker_rss_watchdog_and_death_are_sandbox_faults`,
  `test_certify_deadline_cancels_the_worker_and_never_resurrects_the_copy`,
  `test_certify_deadline_interrupts_a_running_gold_query`,
  `test_certify_deadline_is_harness_fault_not_policy`.
* **Session-wide defaults reach every session.** `validators.session_defaults_for`
  reads the document's top-level `session:` block; `author_limits`,
  `author_policy`, `proposer_policy` and `AgenticRepairProposer` build their
  `SessionLimits` with it (un-hashed: `policy_sha256` and every transcript
  key are unchanged), so `session.format_error_disposition` at its documented
  location reaches sessions built outside `providers.session_policy_for`.
  Pinned by `test_author_and_proposer_policies_carry_the_session_wide_defaults`.
* **1-4 — the Phase 0 log corrected.** `docs/plans/bounded_agents_phase0.md`
  now states the real `evaluate_build` bounds (`DEFAULT_BUILD_LIMITS =
  SemanticLimits(timeout_seconds=300.0)` and the 300 s
  `TRUSTED_LOAD_DEADLINE_SECONDS`), not the 60 s `SemanticLimits()` default.

## 2. Sanctioned behaviour changes

The brief's rule is that every existing one-shot path stays byte-identical
unless the roadmap sanctions the change. These are the observable changes to
a one-shot path introduced in Phase 1, numbered by the review finding that
surfaced them, each with its authority and the test that pins it so it
cannot drift silently. None is behind a config key.

* **Finding 1-0 — the one-shot author and proposer transcript keys (as the
  author-stage repair implements it).** Declaring the Phase 1
  `roles.semantic_author.session` and `roles.repair_proposer.session` blocks
  had re-keyed the one-shot author and proposer transcripts a second time
  after 0.E, because `role_behavior_manifest.loop_limits` hashed the block
  verbatim while `enabled: false` enforced nothing. The resolution is key
  STABILITY: `providers.role_manifest_limits` folds a declared-but-DISABLED
  runner block to exactly `{"enabled": false}` in the manifest and
  `policy_sha256` (the block is hashed verbatim only when it is enforced —
  `enabled: true`, or a seat outside the session-runner roles whose one-shot
  block is admission evidence), so `session.enabled: false` is the
  byte-identical rollback `config/agents.yaml` promises. At the Phase 1 cut,
  the author fixture under `tests/fixtures/transcripts/semantic_author/`
  matched the key the folded manifest produced (re-keyed through the
  deterministic `tools/migrate_transcript_fixtures.py`; the originals stayed
  byte-for-byte under `tests/fixtures/transcripts_legacy/`). Subsequent changes
  to the author system/behaviour invalidated that copy, so it has deliberately
  been removed from the canonical live tree instead of being relabelled as
  current evidence. The legacy response is static archival prose only; the
  migration test proves it cannot be re-keyed or replayed today. Every author
  or proposer transcript recorded between 0.E and this fold under the verbatim-hashed
  block is orphaned. Editing a disabled runner block's limits moves neither
  the behaviour digest nor the one-shot key; enabling it (or dropping the
  declaration) does; the four critic seats' one-shot blocks stay hashed
  verbatim (SoT T1.1). Pinned by `DisabledRunnerBlockKeyStabilityTest`
  (`test_disabled_runner_block_edits_do_not_move_the_one_shot_key_but_enabling_does`,
  `test_runner_roles_mirror_the_registry_declaration`) and
  `test_disabled_session_block_keeps_harness_validators_off_the_manifest`
  in tests/test_providers.py, and by tests/test_transcript_fixture_migration.py
  for the fixture's key.
* **Finding 1-1 — a BLOCKED re-validation halts the one-shot proposer
  (C7; certify addendum §3.3; state machine §6).** In the shared certifier a
  re-validated member that answers `VERDICT_BLOCKED` (the gates on stale
  evidence, `audit` on a human hold, `select` / `release` on an environment
  hold — reachable with sessions disabled through the SPECIFICATION and
  REFERENCE rerun sets) raises `engine.StageBlocked`, an
  `InfrastructureFailure` under `blocked_on:<reason>`: the one-shot
  `RepairProposer` returns `halted`, the engine writes a FAIL row carrying
  the marker and the reason and raises, the CLI exits 2 — no repair round,
  no adjudication entry, the task not rejected, the workspace byte-identical;
  once the wait clears the next run resumes at the failed stage and the same
  patch commits. The Phase 0 end raised `RevalidationFailed` (a failed
  proposal, a NEEDS_ADJUDICATION entry, the ordinary round, exit 0/1). A wait
  is not a measurement of the patch: the disposition is the one `Engine.run`
  gives the same verdict live. Pinned by
  `test_blocked_stage_inside_trial_phase_halts_without_spending_a_round`,
  `test_one_shot_blocked_revalidation_halts_exit_2_and_rejects_nothing`
  (tests/test_repair_proposer_agentic.py),
  `test_stage_blocked_is_an_infrastructure_failure_carrying_the_reason` and
  `test_halt_on_proposer_fault_puts_the_blocked_reason_on_the_row`
  (tests/test_engine_repair.py).
* **Finding 1-2 — the F1 currency fix, form (i), on the shared certifier
  (roadmap 1.P.0; certify addendum §3.6).** `_revalidate` records every
  member's outcome on the trial ledger and prepends the wired `review` to any
  REFERENCE or POPULATION certification whose rerun set reaches `attack`
  (`CURRENCY_PREREQUISITES`), so the real `attack` runner finds the review
  PASS it demands at the new hash: a one-shot REFERENCE / POPULATION
  certification that at the Phase 0 end always failed closed at `attack`
  (`revalidation_red_attack`, a failed proposal, the ordinary round) can now
  run the council inside the trial — memo-served at $0 when the critic views
  did not move (F2), live for the seats whose view moved (the adversary on a
  POPULATION `conditions` patch, F3b) — and commit; and a review that cannot
  MEASURE on the trial (`infrastructure = not_admitted` with no schema-4
  admission, a replay miss under `--replay-only`) HALTS the proposal as
  infrastructure (exit 2, no round, nothing queued) instead of failing red.
  Form (i) was chosen over form (ii) (`Engine.run(until=failed_stage)` on
  the trial) because (ii) re-executes every upstream stage the engine's
  skip rule does not skip — a strict superset of the route's rerun set — so a
  red answer could come from a stage outside that set and the ordered
  `revalidation_red_<stage>` codes of the one-shot record would change; (i)
  keeps the rerun set and adds only the wired prerequisite rows, keeping
  `attempt_patch`'s outcomes byte-identical on every task-defect path.
  Pinned by `test_trial_ledger_records_stage_rows_so_currency_checks_hold`,
  `test_revalidate_with_real_attack_runner_requires_review_evidence_at_new_hash`,
  `test_review_is_memo_served_on_trial_when_critic_views_unchanged`,
  `test_population_conditions_patch_recalls_only_the_adversary_seat` and
  `test_one_shot_reference_certification_halts_when_prepended_review_cannot_measure`
  (tests/test_repair_proposer_agentic.py).
* **Finding 1-3 — the structural `calibrate` runner certifies the trial
  in BOTH proposer modes (certify addendum §3.4).** Under `elt-taskgen
  calibrate --empirical --repair-proposer` a `calibrate`-stage failure is
  certified on the trial with `cli.make_structural_calibrate_runner` over
  the empirical runner's own roster document (`_trial_stage_runners` reads
  `CALIBRATE_EMPIRICAL_TAG`); a structural green commits, and the live
  ladder runs the empirical campaign ONCE, post-commit, at the new hash. At
  the Phase 0 end the trial re-ran the 40-call solver campaign and the patch
  committed only if the campaign passed. Pinned by
  `test_trial_phase_substitutes_structural_calibrate_for_empirical_runner`
  and `test_one_shot_proposer_certification_substitutes_structural_calibrate`
  (tests/test_repair_proposer_agentic.py).
* **Finding 1-7 — `audit list` names repair adjudications (roadmap 1.P
  cli.py row).** For every workspace, with sessions disabled too, a task
  whose one-shot proposer abstained at the current hash
  (`audit/<task>.repair_adjudication.json`) is listed with a `REPAIR
  ADJUDICATION (not a sign-off item ...)` block naming the status, stage,
  route, attempts and rejection codes, and the empty-queue line reads
  "audit queue empty: no task has pending borderline collisions, dual-build
  adjudications or repair adjudications" (the baseline said "no task has
  pending borderline collisions or dual-build adjudications"). A stale entry
  (another hash) is not listed. (The audit queue listing was removed with the
  audit stage; the record itself is pinned by
  `test_repair_adjudication_record_is_bound_to_the_current_hash`
  in tests/test_repair_proposer_agentic.py.)
* **Finding 3-0 — the REFERENCE field allowlist is narrowed to
  `reference.sql_by_mart.*` (a sanctioned SECURITY change; permission matrix
  §4 "reference SQL", trust boundary §6.6).** The one-shot allowlist was
  `reference.*`, which let a patch move the reference's `provenance`,
  `implementation_id`, `dialect`, `load_notes` or `version` — fields read by
  no verifier — and rotate the content hash with the SQL untouched (a
  semantic no-op laundering a stale verdict past the byte-level inert-repair
  rule). Now `ROUTE_IR_PATHS[REFERENCE]` is `("reference.sql_by_mart.*",)`:
  `validate_scope` refuses a diff moving ONLY verifier-inert fields as
  `patch_noop` (a one-shot provenance-only patch is an ordinary failed
  attempt, never a commit), `REPAIR_PROPOSER_SYSTEM`'s derived ROUTE SCOPE
  paragraph no longer offers `reference.*`, and therefore the one-shot
  proposer's system prompt bytes, `role_behavior_sha256` and transcript keys
  moved once more (no committed proposer transcript fixture exists; the
  drive stores were already orphaned by 0.E). Pinned by
  `test_reference_session_edits_only_sql_by_mart`
  (tests/test_review_tools_validators.py),
  `test_validate_scope_refuses_verifier_inert_only_diff_as_noop`
  (tests/test_repair_proposer.py) and
  `test_proposer_prompt_allowlists_match_code_both_directions`
  (tests/test_prompts_repair.py).
* **The Phase 1 re-key of the author and proposer fixtures.** Consequence
  of findings 1-0 and 3-0 together: the one-shot author key moved with the
  Phase 1 session block and moved back under the fold; the one-shot
  proposer key moved with the session block, the fold and the narrowed
  allowlist. At that Phase 1 cut the committed author fixture matched the
  then-current key. It no longer does: later author-system/behaviour drift
  made the record stale, and the live copy was removed. The byte-for-byte
  original under `tests/fixtures/transcripts_legacy/semantic_author/` is kept
  only as archival/static test prose and is not replayable evidence under the
  current route. The canonical live tree currently contains only the
  independently provable `independent_implementer` migration; no author or
  proposer fixture is committed there. The roadmap's live re-record of the
  fixture set is still owed (§3).

Not behaviour changes, but worth knowing: findings 0-4, 2-0, 3-1 and the
session-defaults blocker (§1) closed defects of the bounded paths with no
one-shot behaviour to preserve; findings 1-5 and 1-6 belong to the concurrent
effort named at the top of this log; findings 0-0 to 0-3, 0-5, 0-6, 2-1 and
3-2 are the author-stage repair's (`review/providers.py`,
`review/session.py`, `review/council.py`, `config/agents.yaml`) and are
recorded by it.

### Served-content verification on every source (finding 0-0, residual)

`RoutedProvider._turn_from_entry` serves stored bytes from one of three sources: the schema-3 `turn.content` block, the last raw attempt of a one-shot entry served into turn 0 of a zero-tool session, or blocks rebuilt from the verified `response` for a legacy fixture without raw bodies. Only the first was digest-bound after the first repair pass. Now `_turn_block_integrity_problem` refuses a turn block that lacks `content`, `content_sha256`, `memo_key` or `messages_sha256` (the schema-3 recorder always writes all four), and `_turn_from_entry` refuses any stored blocks whose `_turn_text` does not reduce to the entry's verified `response`, in live mode, under `--replay-only` and on the F2 path. The rebuilt-from-response branch is unchanged because `response` is the verified field there. Pinned by `tests/test_providers_session.py::test_served_raw_content_is_verified_on_every_source`.

## 3. Owner actions

These are the owner's; nothing here was run, and no live command, credential
or network call is part of this change set.

1. **Earn the fresh-live admission under harness "5" (schema 4).** Run
   `elt-taskgen metrology --workspace council --budget-per-task 15` (see the
   Phase 0 log §3 item 3 for the bars); until it lands, `elt-taskgen review`
   on a live-capable provider answers `not_admitted` — and, under finding
   1-2, a one-shot REFERENCE / POPULATION certification reaching `attack`
   halts with exit 2 rather than failing red.
2. **Re-record the fixture set live** (`elt-taskgen record-transcripts`,
   behind the admission gate, with credentials) so the nine un-migrated
   critic and loader fixtures answer the current prompts, the demo council
   replay stops skipping, and a proposer fixture exists under the narrowed
   allowlist.
3. **Rotate and purge the canary credential copies** under
   `runs/runtime_canary_20260901/*/live/` and
   `runs/runtime_canary_20260902/*_adversarial/live/*/attempt/task/`, then
   run the gated sweep (`ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1 .venv/bin/python
   -m unittest tests.test_credential_sweep`) and keep it green.
4. **Decide on the legacy Snowflake password** in the five `__el` public
   releases (`runs/<drive>/release/public/<task>__el/config.yaml`): confirm
   it is the upstream default and allowlist it in
   `review/tools/credential_sweep.py`, or rotate and redact it.
5. **Provision the `elt-proxy-bridge` Docker network** (`docker network
   create elt-proxy-bridge`) on every host that will run `elt-taskgen
   runtime run-stage1` before the first run.
6. **Run the pilots P1 (the author session) and P6 (the bounded proposer)**
   as pre-registered under `docs/experiments/` (`PILOT-<name>.md`): the
   author's revision-pass rate and the proposer's live committed-repair
   rate, p90 wall per committed repair (15 min; attack-reaching repairs
   reported separately against 25 min) and abstention on planted-impossible
   failures are measured live, with `session.enabled: true` /
   `--repair-proposer-mode bounded` and `--budget-per-task 7.00` for the
   bounded profile — nothing in this tree flips either default.

Engineering follow-ups still open after this phase: the Phase 3 `attack`
member of `certify` behind `repair.certify.attack_enabled` (after the
view-unchanged refusal); the POPULATION route for the bounded proposer;
the spawned worker for the remaining in-process validators
(`InProcessValidatorWorker` is still the D3 profile of the cheap gates).

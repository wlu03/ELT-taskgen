# Bounded agents, Phase 0 — change log

Status: SHIPPED in the working tree on 2026-09-03; full suite green
(`.venv/bin/python -m unittest discover -s tests`), `python
tools/prove_admission_integrity.py` prints `ALL PROOFS HELD` with PROOF 5 and
PROOF 6 present. The owner actions in §3 are NOT done and gate the phase's
definition of done (items 4 and 7 of the roadmap's DoD).

Phase 0 is the prerequisite phase of the bounded-agents roadmap (§3 of the
roadmap; the limits vocabulary, repair-certify and constraint-status addenda
are its authorities). Nothing agentic ships: the tool registry is empty for
every role, every `session.enabled` key is `false`, and the repair proposer
is still the one-shot `RepairProposer` (the `--repair-proposer-mode` switch
is Phase 1). Every item is a strict tightening — execution bounds, the
projection layer, the fault taxonomy, the money instrumentation, the
behaviour manifest and transcript key, and the operational fixes — and every
observable change to a one-shot path is listed in §2 with the authority that
sanctioned it. Rollback of 0.A–0.D and 0.F is `git revert` with no evidence
impact (ledger rows bind to `task_content_hash`, not to a harness version);
0.E is a one-way door by design (the staleness rule) and orphans nothing,
because no record under harness "4" was valid.

## 1. What shipped, per item

### 0.A — execution bounds

Every place solver-authored or model-authored code executes is now bounded
and killable. `verification/attacks.py` runs mutants on
`sandboxed_memory_connection(memory_limit_mb=512, threads=1,
disable_temp_spill=True, deterministic_settings=True)` (external file access
off) and `_fetch_rows` caps materialisation at
`ATTACK_MAX_RESULT_ROWS_PER_MART = 100_000` / `ATTACK_MAX_RESULT_BYTES_PER_MART`
= 16 MiB, recording `AttackOutputLimitError` as an error with a stable code —
never as a kill. `reference/independent.py` runs `evaluate_build` per
population inside a spawned worker under `DEFAULT_BUILD_LIMITS =
SemanticLimits(timeout_seconds=300.0)` (`semantic/models.py` supplies the
rest of the envelope: 512 MB DuckDB memory, threads=1, 100k rows / 16 MiB
per mart, a 2 GiB worker RSS envelope) — a 300 s CANDIDATE deadline per
population, armed only after the worker reports the trusted load done, with
the trusted load itself bounded by `TRUSTED_LOAD_DEADLINE_SECONDS = 300.0`
on the harness clock. The 60 s `SemanticLimits()` default is the in-loop
tool's bound, not the dual build's: a slow but correct implementer build
must not be re-scored 0.0 by a bound sized for a session tool (the Phase 0
review's `evaluate_build` finding on the dual-build sample; pinned by
`test_candidate_deadline_starts_after_trusted_load` and the 300 s
assertions of tests/test_independent.py). A worker death
(`worker_failed`) is a `SandboxFault` — the taxonomy's "worker died", never
reward 0.0. `corpus/calibration.py` takes the same sandbox
kwargs. `runtime/process.py` gives `SubprocessRunner` a required positive
default timeout (`DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 3600.0`; `timeout=None`
is a `ValueError`), starts children in their own session and kills the whole
process group at the deadline (`ProcessTimeout`); `DockerRunner` gains
`lane=` (`none` by default: `--network none`, no host mapping; `proxy-bridge`
joins the operator-provisioned `elt-proxy-bridge` network and maps
`host.docker.internal` to the host gateway), runs as a non-root `--user
uid:gid`, kills by container id at its deadline, and lints its own argv
against privileged flags, the Docker socket and host namespaces.
`training/terraform_intent.py` reads the candidate `main.tf` through
`_read_bounded_regular_file` and parses it in a worker whose 10 s deadline is
armed after the worker's ready marker; a slow start or an unreadable regular
file is `TerraformIntentHarnessError` (harness), never the candidate's
`terraform_parse_timeout` / PARSE code. `training/workspace.py` drops
`macros` from `_ALLOWED_TOP_LEVEL` so the seal agrees with
`_prepare_execution_project`. Contract tests:
`test_attacks_run_attack_uses_sandboxed_connection`,
`test_mutant_exceeding_output_caps_is_recorded_as_stable_code`,
`test_bounded_mutant_is_an_error_never_a_kill` (tests/test_attacks.py);
`test_independent_and_calibration_pass_sandbox_caps`,
`test_recursive_cte_in_implementer_sql_times_out_with_stable_code`,
`test_worker_death_is_a_harness_fault_not_a_zero_reward`,
`test_candidate_deadline_starts_after_trusted_load` (tests/test_independent.py);
`test_process_group_kill_on_timeout`,
`test_docker_runner_network_none_for_local_tools`,
`test_sandbox_argv_lint_refuses_privileged_socket_hostnet`,
`test_execution_never_defaults_to_bare_subprocess_runner`,
`test_no_runner_construction_in_src_passes_an_unbounded_timeout`
(tests/test_runtime_process.py);
`test_run_stage1_uses_proxy_bridge_lane_by_default` (tests/test_runtime_cli.py);
`test_terraform_intent_read_is_bounded_and_deadlined`,
`test_slow_parse_worker_start_is_a_harness_fault_never_zero_reward`,
`test_unreadable_candidate_main_tf_is_a_harness_fault_not_parse`
(tests/test_training_terraform_intent.py);
`test_workspace_rejects_macros_top_level` (tests/test_training_workspace.py).

### 0.B — the projection layer

`review/tools/projection.py` (new) is the only path from a certifier result to
model-bound bytes: `Diagnostic` (`extra="forbid"`, frozen, no int, float or
free-text field), `DiagnosticSource`, `DevRows`, `DiagnosticText`, `project`,
`serialize_for_transport(diag, *, task, package, route=None)` (the sole
producer of model-bound bytes; `private_scalars(task, package, route=)` is
its canary set, and on the POPULATION route the declared population scales —
editable IR fields the POPULATION view prints — are excluded while literal-row
counts, frozen stage-1 counts and gold mart row counts stay private on every
route), `assert_value_free(payload, *, task, route)` (the D1 gatekeeper),
`DiagnosticTripwire`, `PublicIdentifierSet`, `DIAGNOSTICS_VERSION`, and the
projectors `project_gate_battery`, `project_gate_details`,
`project_promotion`, `project_rejection`, `project_prose_problems`,
`project_compile`. `review/tools/registry.py` (new) carries `Tool`,
`ToolCost`, `ToolContext`, `ToolRegistry.for_role` (empty for every role in
this phase), `manifest_sha256`, `wire_tools`, `dispatch`, and the path policy
(`path_under_runs`, `path_under_release_root`, denied components and
`*_credential.json` basenames). Consumers were cut over: `cli._triage_view`
is built on `_projected_gate_lines` / `_projected_adjudication_lines` (gate,
passed, code only) and `cmd_triage` exits 2 on `ProviderProtocolError`;
`review/repair_proposer.py`'s `failure_detail(payload, *, route, task,
stage)` projects EVERY route fail-closed (a gate battery to `{failing_gates,
codes}` plus, on POPULATION, the sanctioned per-case / per-relationship
booleans of `project_gate_details`, each row serialised and re-checked; an
attack payload to `{codes: {attack: <code>}}` plus the promoter's outcome
codes on POPULATION; a `StagePayload` to a closed `codes.stage` vocabulary; a
`CalibratePayload` to impossible / trivial variant names and `skipped`; a
`ReviewPayload` to (role, severity) pairs; anything else to `{codes: {stage:
"unprojected"}}` — never the dump), and `view_for_route` runs the assembled
evidence through `_assert_evidence_value_free` before any transport.
`cli._proposal_failure_route` derives the attack-stage FAIL route from the
promoter's measured outcomes and never reads a critic's `route_hint`
(SPECIFICATION by default; POPULATION only when a confirmed exploit kept full
reward on every population). Contract tests:
`test_diagnostic_schema_has_no_numeric_fields`,
`test_projection_models_have_no_free_text_fields`,
`test_projection_identifiers_subset_of_public`,
`test_projection_codes_match_code_re`,
`test_projector_and_gatekeeper_run_in_different_processes`,
`test_gold_count_canary_trips_on_any_frozen_count`,
`test_sanitizer_rejects_measured_reward_json`,
`test_sanitizer_rejects_gold_count_vectors`,
`test_sanitizer_rejects_duckdb_error_text_and_paths`,
`test_text_allowlisted_producers_have_public_inputs`,
`test_private_scalars_are_route_aware_on_population`,
`test_registry_is_empty_for_every_role_in_phase_0`,
`test_tool_context_root_never_under_runs`,
`test_tool_path_policy_denies_runs_and_credential_files`
(tests/test_review_tools_projection.py);
`test_attack_payload_never_reaches_specification_route_raw`,
`test_failure_detail_is_code_only_on_specification_and_reference`,
`test_population_route_receives_projected_booleans_not_values`,
`test_failure_detail_fails_closed_on_unknown_payloads`,
`test_population_view_declared_scales_agree_with_private_scalars`,
`test_project_rejection_carries_the_raise_site_code`
(tests/test_repair_proposer.py); `test_triage_view_uses_projected_gate_rows`,
`test_triage_protocol_error_exits_2_not_1`,
`test_triage_sentinel_property_zero_private_sentinels`,
`test_triage_sentinel_property_holds_on_demo_and_runs_drives`
(tests/test_filters_batch_triage.py);
`test_tripwire_halts_as_infrastructure_reward_none`
(tests/test_engine_repair.py);
`test_proposal_failure_route_ignores_critic_route_hint` (tests/test_cli.py).

### 0.C — the fault taxonomy

`review/session.py` (new; faults only, the runner is Phase 1) types faults by
the boundary that catches them: `SessionFault(boundary)` with
`ProviderFault`, `ToolHarnessFault`, `ToolDeadlineExceeded`, `SandboxFault`,
`TaskDefectFault` (harness-side, infrastructure); `PolicyFault` with
`ToolProtocolFault`, `ToolNotPermitted`, `OracleCapExceeded`,
`WriteOutsideSurface`, `ForbiddenArgument` (model-caused, one fixed code
each); and the wrappers `SessionProtocolError` / `SessionPolicyViolation`
(`ProviderProtocolError` subclasses). `engine.py`'s `_INFRA_EXCEPTION_NAMES`
gains the seven canonical names, and `_infra_marker_for` now walks
`__cause__` / `__context__` through `_exception_chain` (bounded, cycle-safe,
`raise ... from None` included) so a wrapped harness fault still halts.
`_propose_patch` maps `RepairOutcome.disposition == "blocked_limit"` to
`VERDICT_BLOCKED` (`blocked_on = session_limit:<kind>`, a `session_salt`, at
most `MAX_SESSION_LIMIT_RERUNS = 2` salted re-runs, no round, no fatal row;
after the re-runs the agent-attributable kinds take the ordinary round while
`wall` — `SESSION_LIMIT_HALT_KINDS` — halts as `ProviderFault`, and a `usd`
stop of the task or total scope halts at once under `BudgetExceededError`,
only the role scope being the agent's `LIMIT_USD`), maps `halted` to
`InfrastructureFailure` with the outcome's marker, and halts a `ValueError`
/ `TypeError` escaping `repair()` under `PROPOSER_CONTRACT_MARKER`
(`proposer_contract`) instead of recording a failed proposal.
`review/repair_proposer.py` adds `RepairOutcome.disposition`,
`infrastructure`, `limit` and `limit_scope` (additive, defaulted),
`halting_marker(exc)`, `budget_limit_scope(exc)`, `LIMIT_USD`,
`LIMIT_SCOPE_ROLE`, `BUDGET_LIMIT_SCOPES`, `ROLE_CAP_EXCEPTION_NAME`; the
one-shot `repair()` returns `halted` on a harness fault and `blocked_limit`
/ `usd` / `role` on the session's own cap, and `_revalidate` re-raises a
nested harness fault (or re-raises a nested plain protocol error or another
seat's role-cap trip as `InfrastructureFailure`) instead of wrapping it into
`RevalidationFailed`. `cli._transport_marker` lifts the seven names out of
producer notes and `STAGE_SUBCOMMANDS` maps `author` to `review`. Contract
tests: `test_taxonomy_names_match_the_sot`,
`test_validator_crash_is_tool_harness_fault_and_output_is_not_sent`,
`test_validator_deadline_is_harness_fault_not_policy`,
`test_leak_tripwire_halts_before_transport`,
`test_policy_violation_halts_without_spending_a_round_or_rejecting_task`,
`test_limit_stop_without_green_draft_blocks_with_salt_and_bounded_reruns`,
`test_blocked_limit_disposition_maps_to_verdict_blocked`,
`test_wall_limit_after_reruns_halts_as_infrastructure_not_a_round`,
`test_usd_limit_halts_or_rounds_by_budget_scope`,
`test_budget_limit_scope_keys_on_role_cap_name_or_scope_attribute`,
`test_one_shot_role_cap_trip_is_blocked_limit_usd_not_a_halt`,
`test_malformed_repair_outcome_is_a_proposer_contract_fault`,
`test_proposer_halts_on_harness_fault_inside_an_attempt`,
`test_proposer_harness_fault_halts_the_engine_without_a_round`,
`test_transport_marker_lifts_session_fault_names`,
`test_stage_subcommands_maps_author_to_review` (tests/test_session.py);
`test_nested_transport_fault_inside_trial_phase_halts_without_spending_a_round`,
`test_wrapped_harness_fault_inside_trial_phase_still_halts`,
`test_one_shot_proposer_harness_fault_is_a_pinned_c7_deviation`
(tests/test_repair_proposer.py); `test_infra_marker_walks_cause_and_context`
(tests/test_engine_repair.py); `test_triage_protocol_error_exits_2_not_1`
(tests/test_filters_batch_triage.py);
`test_infrastructure_failure_exits_2_and_rejects_nothing` (tests/test_cli.py)
stays green.

### 0.D — money

`review/providers.py` prices every model on a four-rate `RateCard` (input,
cache write, cache read, output; Sonnet 5 at 2.00 / 2.50 / 0.20 / 10.00 per
MTok, so `rates_for()` returns (2.00, 10.00)); `CostMeter.reserve` runs before
any transport call and refuses, with nothing spent, a call the remaining
budget cannot absorb; `charge` meters per API attempt after the transcript
is recorded, including the attempts of a backend that raises; per-role caps
live in `TrajectoryBudget` beside the task budget, and a role's own cap
breach is `RoleCapExceeded(BudgetExceededError)` with `scope="role"` (a name
deliberately absent from `engine._INFRA_EXCEPTION_NAMES`), while the task and
total scopes keep raising the base class (infrastructure). A cap declared
under `session.enabled: false` never trips a one-shot exchange
(`RoleRoute.cap_enforced` / `cap_declared_but_disabled`, threaded as
`enforce_role_cap` through `RoutedProvider.complete` and the batch queue), so
the one-shot maximum of 1 + SCHEMA_RETRIES maxed attempts cannot halt a seat.
Transcript entries record cache-token usage fields, `elapsed_ms` and the
provider-reported `served_model`. `DEFAULT_BUDGET_PER_TASK_USD` is 5.00 and
the CLI default follows; the metrology help says `--budget-per-task 15`; the
live spend footer prints the effective ceiling, `attempts=` and `wall=`
columns and reads "metered per API attempt". Contract tests:
`test_sonnet_5_is_priced_at_two_and_ten`,
`test_cost_meter_prices_cache_reads_at_one_tenth`,
`test_trajectory_budget_stops_before_breaching_turn`,
`test_session_reserve_raises_before_transport_call`,
`test_charge_covers_every_api_attempt`,
`test_role_cap_is_enforced_beside_the_task_budget`,
`test_role_cap_breach_is_role_cap_exceeded_not_infrastructure`,
`test_one_shot_role_cap_not_enforced_when_session_disabled`,
`test_one_shot_critic_three_maxed_attempts_never_trip_a_disabled_session_cap`,
`test_one_shot_transcript_entry_fields_are_stable` (tests/test_providers.py);
`test_budget_breach_raises_but_transcript_survives` stays green;
`test_role_cap_exceeded_is_not_a_named_infrastructure_class`
(tests/test_repair_budget.py); `test_budget_guidance_is_synchronised`
(tests/test_docs_consistency.py).

### 0.E — behaviour manifest, transcript key v3, harness "5"

`review/providers.py` defines `role_behavior_manifest(role)` — the one
behaviour document a role runs under (system prompt digest, wire `tools[]`
exactly as `_payload` sends them, `tool_choice_policy`, `policy_sha256`,
`loop_limits` verbatim from the role's `session:` block,
`correction_text_sha256`, `schema_retries`, `api_version`, role-wired
validators and projections, the `sandbox` pin) — and `role_behavior_sha256`
is the sha256 of its canonical JSON; `transcript_key_v3(role, policy,
messages)` keys every exchange (`transcript_key(role, prompt)` is v3 over the
one user turn), every entry carries an `entry_schema: 2` route block
(`behavior_sha256`, `tools_sha256`, `policy_sha256`, `diagnostics_version`),
and `transcript_route_mismatch` refuses on any mismatch; readers tolerate
lower schemas and never upgrade a stored entry. A custom `--agents-config`
is the hashed document (`agents_config_of(routing)`,
`RoutedProvider.transcript_key_for`). `review/metrology.py` pins
`HARNESS_VERSION = "5"` and `ADMISSION_SCHEMA = 4`; fingerprint v5 adds
`tool_surface_sha256` (critic-wired only), `validators`, `sandbox`, `api`,
per-critic `behavior_sha256` and `loop_limits` (`validator_digests`,
`sandbox_digest`); the run asserts the toolchain pins at start (exit 2,
nothing spent); `summarize_fresh_live_trajectories` carries the count
invariants, `replayed_model_call_count`, `tool_call_count_total`,
`stale_tool_result_count` and `canary_hits`; three canary specimens per seat
per run (`CANARY_PER_ROLE = 3`; private-only literal, forbidden validator,
impossible) with `max_canary_hits: 0`; schema-3 records are refused as
SUPERSEDED. `tools/prove_admission_integrity.py` gained PROOF 5 (a
critic-wired validator description moves the tool surface and stales the
record) and PROOF 6 (a `stale_tool_result_count = 1` record is refused).
`config/agents.yaml` gained the `metrology` thresholds, the toolchain pins,
the `sandbox` declaration and the four critic seats' one-shot `session:`
blocks. The 11 committed transcript fixtures moved byte-for-byte to
`tests/fixtures/transcripts_legacy/`; `tools/migrate_transcript_fixtures.py`
re-keyed the two it could prove at the Phase 0 cut (see §2, finding 15).
Subsequent author-system drift invalidated the author copy, which is no longer
present in the live fixture tree; the migration now proves and re-keys only the
`independent_implementer` entry. Contract tests:
`test_role_behavior_sha256_is_sha256_of_canonical_manifest`,
`test_behavior_manifest_equals_wire_tools`,
`test_transcript_key_covers_tool_protocol`,
`test_transcript_key_v3_one_shot_turn_equals_transcript_key`,
`test_route_mismatch_on_tool_protocol_change`,
`test_readers_tolerate_legacy_entry_schema_and_never_upgrade`,
`test_fingerprint_moves_when_tool_schema_description_policy_limits_or_pin_change`,
`test_custom_agents_config_moves_manifest_key_and_route_block`,
`test_disabled_session_block_keeps_harness_validators_off_the_manifest`
(tests/test_providers.py); `test_harness_five_and_schema_four_are_pinned`,
`test_schema_3_record_is_superseded`, `test_stale_tool_result_record_is_refused`,
`test_count_invariants_refuse_an_inconsistent_trajectory_row`,
`test_fingerprint_moves_when_tool_description_changes`,
`test_fingerprint_moves_when_a_critic_wired_validator_module_changes`,
`test_fingerprint_moves_when_the_sandbox_pin_changes`,
`test_fingerprint_moves_when_loop_limits_change`,
`test_fingerprint_moves_when_correction_text_changes`,
`test_fingerprint_unchanged_by_non_critic_projection_version`,
`test_limit_stopped_trial_is_scored_not_errored`,
`test_tool_crash_is_could_not_measure_exit_2`,
`test_efficiency_bars_cannot_make_admission_easier`,
`test_private_only_canaries_are_undetectable_from_public_surfaces`,
`test_impossible_specimen_scores_clean_under_oracle`,
`test_canary_hit_blocks_the_seat_with_reason_canary_hit`,
`test_toolchain_pins_agree_with_uv_lock`,
`test_toolchain_pin_mismatch_exits_2_before_any_call`
(tests/test_council_efficacy.py); the four tests of
tests/test_transcript_fixture_migration.py;
`test_admission_fingerprint_covers_the_system_prompt`
(tests/test_redteam_round4.py) and
`test_routing_fingerprint_covers_the_harness_and_the_pool`
(tests/test_council_efficacy.py) stay green under the new body.

### 0.F — operational fixes

`REPAIR_PROPOSER_SYSTEM` derives its route-scope paragraph from
`ROUTE_ALLOWLIST` / `ROUTE_IR_PATHS` (`repair_route_scope_text()`), and
`DEFAULT_ROUTING_DOC` is held to `config/agents.yaml` by a drift test.
`corpus/calibration.py`'s `roster_fingerprint(tiers)` is the sha256 of the
canonical JSON of `{instrument_version, harness_version, roster, tiers}` —
`INSTRUMENT_VERSION` ("1": the solver-visible prompt, the submission contract
and the scorer path), `metrology.HARNESS_VERSION`, the model-key roster
identity, and per tier `{model_key, provider, model, k, max_tokens, effort,
endpoint}` sorted by `model_key`; `SolverTier.endpoint` is the `base_url`
the tier's provider is configured to call (else the backend default), so the
same model id behind another gateway is another instrument. The roster's
WEAKEST FIRST order is ordering only: `FLAG_TRIVIAL` and
`CalibrationResult.trivial_variants` are read off
`corpus/selection.py`'s `variant_is_trivial` (every pinned tier aced k/k)
and `FLAG_IMPOSSIBLE` off `variant_is_impossible` (zero successes on every
tier) — the ONE definition each, so the evidence file can never flag what
selection would not exclude. `review/tools/credential_sweep.py` (new)
reports credential-shaped files by name and by live value without ever
retaining a value; `cli.cmd_runtime_prepare` refuses a `--work-dir` under the
repository's `runs/` (anchored by resolved path to the repository's own
directory, so an unrelated tree containing a `runs` component is fine) or a
release root before any credential is read; every `ToolContext` root and the
tool path policy deny `runs/` and `*_credential.json`. `docs/experiments/`
joined `CURRENT_DOCS` and the budget guidance is one number everywhere it is
stated. Contract tests:
`test_proposer_prompt_allowlists_match_code_both_directions`
(tests/test_prompts_repair_triage.py);
`test_calibration_cache_misses_when_k_effort_max_tokens_or_instrument_change`,
`test_one_definition_of_trivial`,
`test_legacy_weakest_tier_trivial_flag_is_retired` (tests/test_calibration.py);
`test_default_routing_doc_matches_agents_yaml` (tests/test_providers.py);
`test_attempt_workspace_contains_no_live_credential`,
`test_runs_tree_carries_no_credential_shaped_files` (gated, see §3),
`test_sweep_never_returns_or_prints_a_value` (tests/test_credential_sweep.py);
`test_runtime_prepare_refuses_work_dir_under_runs_or_release`
(tests/test_runtime_cli.py); `test_tool_context_root_never_under_runs`,
`test_tool_path_policy_denies_runs_and_credential_files`
(tests/test_review_tools_projection.py);
`test_experiments_readme_is_a_current_doc`, `test_budget_guidance_is_synchronised`
(tests/test_docs_consistency.py).

## 2. Sanctioned behaviour changes

The brief's rule is that every existing one-shot path stays byte-identical
unless the roadmap sanctions the change. These are the observable changes to
a one-shot path, numbered by the Phase 0 review finding that surfaced them,
each with the authority that sanctioned it. None is behind a config key;
each is pinned by a test so it cannot drift silently.

* **Finding 13 — the one-shot proposer halts on harness faults (C7;
  roadmap 0.C; brief rule 7).** `RepairProposer.repair()` no longer counts
  an infrastructure-typed exception (`TranscriptMissingError`,
  `BudgetExceededError` of the task or total scope,
  `MissingCredentialsError`, `TranscriptRouteMismatchError`, a transport
  sentence, any `SessionFault`, a `DiagnosticTripwire`) as a failed
  proposal: the outcome is `halted` with the marker and the engine raises
  `InfrastructureFailure` (exit 2, no adjudication queue entry, no repair
  round, nothing rejected). The baseline recorded a `repair proposer failed`
  row, queued NEEDS_ADJUDICATION and spent the ordinary round (exit 0/1).
  The role-scope cap is the one exception: it is the agent's `LIMIT_USD`
  and returns `blocked_limit` / `usd` / `role` (finding 9, SoT T4). Pinned by
  `test_one_shot_proposer_harness_fault_is_a_pinned_c7_deviation` and
  `test_one_shot_role_cap_trip_is_blocked_limit_usd_not_a_halt`.
* **Finding 14 — the proposer's user prompt is projected on
  SPECIFICATION and REFERENCE (roadmap 0.B, Table 4).** `failure_detail`
  emits `{failing_gates: [names], codes: {gate: code}}` instead of the
  failing gate dicts, and every other payload shape is projected or reduced
  to `{codes: {stage: "unprojected"}}`. The model sees different bytes, so
  every proposer transcript recorded before the cut-over misses and live
  outputs will differ. Pinned by
  `test_failure_detail_is_code_only_on_specification_and_reference` and
  `test_failure_detail_fails_closed_on_unknown_payloads`.
* **Finding 15 — transcript key v3 (roadmap 0.E, R-G; brief rule 3).**
  `transcript_key` and `role_behavior_sha256` were redefined, so EVERY store
  recorded before 0.E is a hard miss under `--replay-only`
  (`TranscriptMissingError`, exit 2): the drive stores
  `runs/<drive>/transcripts/`, the retained attempt stores
  `tests/fixtures/transcripts.attempt7/` `.attempt9/` `.attempt10/`
  `.preview-enrich/`, `archive/stale-drive-transcripts/`, and
  `council/transcripts/` (239 legacy entries, now orphaned; not modified in
  place). Of the 11 committed fixtures, kept byte-for-byte under
  `tests/fixtures/transcripts_legacy/`, `tools/migrate_transcript_fixtures.py`
  initially re-keyed the two it could then PROVE (`semantic_author`,
  `independent_implementer`: the prompt re-rendered through the current view
  builders reproduced the legacy key and the recorded `system_sha256` was the
  current system prompt's digest). That is historical Phase 0 evidence, not
  today's replay inventory: later author-system/behaviour drift invalidated the
  author copy, so the canonical live tree now contains only the independently
  provable `independent_implementer` re-key. The legacy author response remains
  byte-preserved and may be consumed as static test prose, but is neither
  re-keyed nor served as current model evidence. At the Phase 0 cut, the nine
  others were not re-keyed: the
  seven critic entries (`ambiguity_critic` x2, `feasibility_reviewer` x3,
  `population_adversary`, `shortcut_attacker`) were recorded under earlier
  system prompts, so re-keying would assert the critics answer today's
  instructions as they answered the old ones (fabricated evidence); the two
  `independent_loader` entries embed an emitted EL bundle only a workspace
  with frozen gold reproduces (`--el-bundle-dir`). The recorded demo council
  replay is therefore a VISIBLE skip (`RecordedDemoCouncilTest`) and the
  roadmap's live re-record (`elt-taskgen record-transcripts`) is still owed
  (§3). The current migration test additionally pins that only the implementer
  is emitted and that the stale author misses in both live and legacy stores.
* **Finding 16 — `DEFAULT_BUDGET_PER_TASK_USD` 2.00 to 5.00 (roadmap 0.D;
  Cost T3).** A caller relying on the default no longer halts (exit 2) at
  $2.01 of spend; the CLI default and the runbook's spend footer follow, and
  the effective ceiling is printed on every live spend footer. Pinned by
  `test_budget_guidance_is_synchronised`.
* **Finding 17 — reserve before transport, meter per attempt (roadmap
  0.D; Cost T8).** Near the budget a one-shot call is refused BEFORE
  transport, so no transcript is written for it, and a schema-retry
  exhaustion at the budget edge can surface as `BudgetExceededError`
  (chained) rather than `ProviderProtocolError`; the metered spend now
  includes a raising backend's attempts. Exit code stays 2. Pinned by
  `test_one_shot_transcript_entry_fields_are_stable`,
  `test_session_reserve_raises_before_transport_call` and
  `test_charge_covers_every_api_attempt`.
* **Finding 20 — `runtime prepare` refuses a confined `--work-dir`
  (roadmap 0.F; threat row A22).** A work directory under the repository's
  `runs/` or under a release root is a usage error (exit 2) before any
  credential is read. The check is anchored to the repository's own `runs/`
  by resolved path, not to any path component named `runs`. Pinned by
  `test_runtime_prepare_refuses_work_dir_under_runs_or_release`.
* **Finding 22 — `REPAIR_PROPOSER_SYSTEM` bytes changed (roadmap 0.F).**
  The route-scope paragraph is derived from the code allowlists and the
  population route no longer offers `files under populations/`; the
  one-shot proposer's system prompt, behaviour digest and transcript keys
  moved. Pinned by `test_proposer_prompt_allowlists_match_code_both_directions`.
* **Finding 23 — `audit_triage` cut-over (roadmap 0.B).** The
  `AUDIT_TRIAGE_SYSTEM` text and `_triage_view` bytes changed (gate rows
  projected to codes, adjudication detail replaced by a code), so triage
  transcripts recorded before it miss; `elt-taskgen triage` exits 2 on
  `ProviderProtocolError` where the baseline let it propagate. Pinned by
  `test_triage_view_uses_projected_gate_rows` and
  `test_triage_protocol_error_exits_2_not_1`.
* **Finding 24 — a custom `--agents-config` is the hashed document
  (roadmap 0.E).** The behaviour manifest, transcript key and route block of
  a `RoutedProvider` built from a custom config now hash that config rather
  than the repository default, so keys recorded under the default move for
  custom configs whose declarations differ. Pinned by
  `test_custom_agents_config_moves_manifest_key_and_route_block`. Still
  open: the admission fingerprint (`council_routing_fingerprint`,
  `role_loop_limits`, `sandbox_pin` in `review/metrology.py`) and the
  pre-computed sample keys in `reference/independent.py` and
  `corpus/calibration.py` still read the default document.
* **Finding 25 — Sonnet 5 rate card (roadmap 0.D; cost note item 2).**
  List rates moved from (3.00, 15.00) to (2.00, 2.50, 0.20, 10.00), so the
  metered USD for `audit_triage` and the Sonnet calibration tiers drops by a
  third in spend lines and evidence `usd` fields; cache reads are priced at
  one tenth of input. Pinned by `test_sonnet_5_is_priced_at_two_and_ten` and
  `test_cost_meter_prices_cache_reads_at_one_tenth`.
* **Finding 26 — `macros` seal (roadmap 0.A; SoT T2).** A candidate with
  `elt/macros/*.sql` now fails at seal with `WORKSPACE_TRANSIENT_STATE`
  ("not admitted") instead of later in `_prepare_execution_project` as an
  unsafe artifact: a different error code for the same submission. Pinned by
  `test_workspace_rejects_macros_top_level`.
* **Finding 27 — live spend stdout (roadmap 0.D).** The spend header
  reads "metered per API attempt" and each role row gains `attempts=` and
  `wall=` (and cache) columns; the runbook's sample output follows.
* **0.A bounds on `evaluate_build` (roadmap 0.A; brief rule 5 names this
  as the one sanctioned change).** The dual-build implementer sample now
  executes under `DEFAULT_BUILD_LIMITS` (`SemanticLimits(timeout_seconds=
  300.0)`: a 300 s candidate deadline per population, armed after the
  trusted load, which has its own 300 s `TRUSTED_LOAD_DEADLINE_SECONDS`) in
  a spawned worker — NOT the 60 s `SemanticLimits()` default an earlier
  draft of this log stated (corrected under Phase 1 finding 1-4): a slow or
  large but correct build that scored 1.0 in-process can score 0.0 with a
  stable `execution_timeout` / `memory_limit` / output-cap code, and
  `MemoryError` text becomes the code `memory_limit`; a build finishing
  under the limits yields the same `(rewards, errors)`. A worker death or a
  deadline before the trusted load completes is a `SandboxFault` halt, never
  0.0. Pinned by `test_independent_and_calibration_pass_sandbox_caps`,
  `test_recursive_cte_in_implementer_sql_times_out_with_stable_code`,
  `test_worker_death_is_a_harness_fault_not_a_zero_reward` and
  `test_candidate_deadline_starts_after_trusted_load`.
* **0.A bounds on attack mutants (roadmap 0.A).** Mutants run on a
  sandboxed, resource-capped connection with row / byte caps and external
  file access off, so a mutant that previously ran (or read gold files) can
  now error with a stable code and score 0.0; existing attack matrices for
  tasks with heavy mutants may re-measure differently, and
  `promote_proposed_cases` compares against the re-measured matrix. Pinned
  by `test_attacks_run_attack_uses_sandboxed_connection`,
  `test_mutant_exceeding_output_caps_is_recorded_as_stable_code` and
  `test_bounded_mutant_is_an_error_never_a_kill`.
* **`SubprocessRunner` default (roadmap 0.A).** The default timeout is
  3600 s (`timeout=None` is a `ValueError`) and children start in a new
  session, so every default-constructed runner (abctl bootstrap, stage
  runners, the source environment) dies with `ProcessTimeout` after one hour
  and no longer receives the terminal's Ctrl-C directly (an interrupted
  parent kills the group). The bootstrap exposes its own bound
  (`elt-taskgen runtime install-airbyte --timeout`). Pinned by
  `test_process_group_kill_on_timeout` and
  `test_no_runner_construction_in_src_passes_an_unbounded_timeout`.
* **Calibration "trivial" unified with `INSTRUMENT_VERSION` (roadmap 0.F
  "one definition of trivial"; recorded as a DELIBERATE deviation).**
  Before 0.F `calibrate` flagged `FLAG_TRIVIAL` when the WEAKEST tier (rank
  0) aced k/k; selection already excluded only when EVERY tier aced. The one
  definition is now selection's, so a variant the weakest tier aced but a
  stronger tier missed is no longer flagged in the evidence file,
  `CalibrationResult.trivial_variants` or the CLI summary line — different
  flags for the same measured data. `INSTRUMENT_VERSION` is folded into
  `roster_fingerprint`, so every cached record measured under the old
  instrument is a miss and re-measures under the new flag on the next
  `elt-taskgen calibrate --empirical`. The retired predicate is pinned as
  legacy by `test_legacy_weakest_tier_trivial_flag_is_retired`; the
  unification by `test_one_definition_of_trivial`.

Not behaviour changes, but worth knowing: findings 0–4 and 6 (POPULATION
gate batteries, the critic `route_hint`, unprojected payloads, declared
scales, wrapped faults in re-validation, the `wall` / `usd` fallback) were
closed as leak or classification defects with no baseline behaviour to
preserve; finding 10 (per-role caps tripping one-shot critics) was a
regression introduced and removed within the phase, so one-shot critic
exchanges behave as at baseline.

## 3. Owner actions

These are the owner's; nothing here was run, and no live command, credential
or network call is part of this change set.

1. **Rotate and purge the canary credential copies, then run the gated
   sweep.** The credential-shaped files under
   `runs/runtime_canary_20260901/*/live/` and
   `runs/runtime_canary_20260902/*_adversarial/live/*/attempt/task/` are
   installed attempt copies carrying live values (liveness unverified; no
   network call was made). Rotate every credential they name at the
   provider, delete the copies, then run
   `ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1 .venv/bin/python -m unittest
   tests.test_credential_sweep` — the sweep prints paths and key names only,
   never a value — and keep it green from then on (DoD item 7).
2. **Decide on the Snowflake password in the five legacy `__el` public
   releases.** The `config.yaml` of each
   `runs/<drive>/release/public/<task>__el/` bundle (`dbt_elt`, `dlt_elt`,
   `schemapile_elt`, `synsql_elt`, `wikidbs_elt`; deprecated schema-2
   layout, kept readable) carries a non-placeholder Snowflake destination
   password. Either confirm it is the upstream ELT-Bench default and
   allowlist that value in `review/tools/credential_sweep.py` (beside
   `PUBLIC_SOURCE_FIXTURE_VALUES`, with the upstream citation), or rotate it
   and redact the five files. Until decided, the gated sweep reports them.
3. **Earn the first schema-4 admission under harness "5".** Run
   `elt-taskgen metrology --workspace council --budget-per-task 15` (152
   fresh exchanges, about $10.7 at list rates; the default budget cannot
   cover it). Done means `council/state/council.live_admitted` carries
   `schema: 4`, `harness_version: "5"`, `execution_mode: fresh_live`,
   `replayed_model_call_count: 0`, `tool_call_count_total: 0`,
   `stale_tool_result_count: 0`, `canary_hits: 0` per seat, all four seats
   clearing the bars in `config/agents.yaml`, and `elt-taskgen review` on
   the demo task passing the admission gate (DoD item 4). Then re-record the
   fixture set once with `elt-taskgen record-transcripts` (behind the
   admission gate, with credentials) so the nine un-migrated critic and
   loader fixtures answer the current prompts and the demo council replay
   stops skipping.
4. **Provision the proxy-bridge network for the cloud stage-1 lane.** On
   every host that will run `elt-taskgen runtime run-stage1`, create the
   `elt-proxy-bridge` Docker network once (`docker network create
   elt-proxy-bridge`) before the first run; the runner joins it on
   `--sandbox-lane proxy-bridge` (the lane `run-stage1` derives from its
   Airbyte server URL) and cannot reach any Airbyte API on the default
   `none` lane.

Engineering follow-ups still open after this phase: thread
`agents_config_of(routing)` into the metrology fingerprint document and into
the pre-computed sample keys of `reference/independent.py` and
`corpus/calibration.py` (finding 24, second half); the roadmap's Phase 1
`--repair-proposer-mode` switch and the `session.py` runner.

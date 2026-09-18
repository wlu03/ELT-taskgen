# Bounded agents, Phase 2 — change log

Status: the 2.b half, the configuration, the CLI seams and the records are in
the working tree on 2026-09-03 (this log); the 2.a half (the implementer and
loader tools, the witness sessions inside `reference/independent.py`, the
projection changes) is the concurrent 2.a work in the same tree and is
recorded by its own tests (roadmap §5, Table 6, the 2.a test list). Item 2.c
is DEFERRED (§4). The owner actions in §5 are NOT done and gate the phase's
definition of done: the metered live smoke per backend, the P2 and P3
pilots, the fresh-live re-earn, the credential purge.

Phase 2 is §5 of the bounded-agents roadmap (the limits vocabulary addendum
T1 IMP / LDR, T2, T4, T6; Output 8 for the reward composition; Output 11 for
the pilots; the constraint addendum §4 and A23 for the DEVELOPMENT channel),
built on the Phase 0 and Phase 1 change logs
(`docs/plans/bounded_agents_phase0.md`, `docs/plans/bounded_agents_phase1.md`).
What ships here is the versioned training signal and the declarative,
Docker-free environment (2.b), the two witness roles' declared `session:`
blocks with `enabled: false` and their key-stable manifest fold, the CLI
seeding of whole trajectories and the could-not-measure disposition of a
witness harness fault, and the pre-registration skeletons of pilots P2 and
P3. Every existing path stays byte-identical except the changes §3 lists,
each with its authority and the test that pins it. Rollback of a witness
session is `roles.independent_*.session.enabled: false` (the shipped
value), never a code revert.

## 0. The owner decision in force: OQ-23 = option C

The constraint owner chose option C on 2026-09-03: DEVELOPMENT-population
row counts are solver-visible by construction. The DEVELOPMENT warehouse is
what a solver executes against, its tables hold 2 to 8 rows, and a page of
rows is the count, so the C5 clause is amended rather than the `dev_query`
tool withheld (option A) or an aggregate ban imposed (option B). What stays
private on every route and every seat: stage-1 counts of every HIDDEN
population (primary, counterfactual, resampled, stress and any other graded
population), stage-2 gold (mart outputs), reference SQL, per-mart gold-match
bits, rewards, `dev_pass`, `expected_fingerprints` and the answer-key files.

Consequences, by item:

* `dev_query` ships in the implementer manifest as a port of the sibling
  verifier's read-only DuckDB tool (`training.dev_tool`, 2.a): a 200-row and
  16 KiB cap, a `read_only` connection with `enable_external_access` off and
  `lock_configuration` on, the DEVELOPMENT warehouse resolved by PATH
  EQUALITY, source BASE TABLEs only (never a mart, never a gold table, never
  a hidden-population path), per-session cap 8 (`per_tool.dev_query`).
* `own_row_count` is dropped from every projection; the per-mart "matches
  dev gold" bit stays off (enabling it is a declared C5 reopening);
  `run_mart_sql_dev` returns `{mart, code}` with rows discarded.
* `projection.private_scalars` and the gold-count canary: DEVELOPMENT
  stage-1 counts are NOT private scalars (they are what the solver already
  sees); hidden-population stage-1 counts and gold mart row counts remain
  private on every route (2.a: `test_no_tool_reads_answer_key_stage1_counts_file`,
  `test_dev_query_refuses_hidden_population_paths`).
* The decision is recorded in `docs/experiments/PILOT-P3.md` (the arms, the
  dev-pass/hidden-fail veto, the sentence that option C is in force), here,
  and in the C5 wording of `docs/INTERFACES.md` and `README.md`.

## 1. What shipped, per item

### 2.b — `training/signal.py` and `training/env.py` (new)

`training/signal.py` (`workspace-signal-v1`): `WorkspaceTrainingSignal`
(frozen, closed: `signal_version`, `scorer_version`, `artifact_sha256`,
`label_valid`, `el_pass`, `policy_violation`, `r_el`, `r_t`, `w_t = 1.0`,
`reward: float | None`, `first_failed_phase` in `{none, terraform, sync, dbt,
mart, immutability, workspace}`, `canary_task`, `canary_hit`, `populations`)
and `training_signal(result, *, w_t=1.0, infeasible_task=False,
aborted_infeasible=False)`, a PURE projection of one `WorkspaceScoreResult`
(the same result JSON always yields the same signal; nothing reads the
artifact, the package or the task). Precedence, C7 first: `label_valid :=
result.reward is not None` (a replay-time task, harness or infrastructure
failure is reward `None` and never a label); then `policy_violation`
(`result.failure` classified `POLICY_VIOLATION`, or a member of
`TERRAFORM_POLICY_VIOLATION_CODES`, `dbt_unsafe_artifact` or
`dbt_raw_mutated` in any graded population's `error_codes`) zeroes the
reward AND the EL credit; then the planted-infeasible canary (a pass —
`el_pass` or `r_t > 0` — is a `canary_hit` forced to 0.0; an abort recorded
as the last action earns 1.0, and only through the private
`infeasible_task` flag plus `aborted_infeasible`); then an invalid
submission is 0.0; else

$$R = \frac{r_{EL} + \mathbf{1}[el\_pass]\, w_T\, r_T}{1 + w_T}$$

with `r_EL = 1.0` iff `el_pass` (valid submission, no failure, a NON-EMPTY
graded set, every graded population `strict_el_pass`, no violation) and
`r_T = result.reward`, already the minimum `end_to_end` over the hidden
graded set: fail 0, EL-only $$1/(1+w_T)$$ (0.5 at the shipped $$w_T = 1$$),
end-to-end up to 1. `first_failed_phase` is derived from the binding
population's heads in scorer order (Terraform, sync, dbt, marts,
immutability), `workspace` for a lifecycle refusal, `none` when no candidate
phase is attributed. `drop_group_if_unlabelled(group) -> bool` is the
rollout adapter's whole-group rule: true when any member is unlabelled, and
never a filtered subset (a replacement spliced in changes the group-relative
comparison). `POLICY_VIOLATION_CODES` and `first_failed_phase` are exported.
`score_workspace`, `WorkspaceScoreResult` and every other `training/` module
are consumed through their public interfaces only and are byte-identical
(`test_official_score_refuses_a_caller_selected_graded_subset` stays green).

`training/env.py`: `DeclarativeEltEnv(release_dir, *, attempts_root,
runtime_config, dbt_limits=None, grader_deadline_s=None, w_t=1.0,
scorer=score_workspace, infeasible_tasks=(), verify_release=True)` (a `None`
`grader_deadline_s` is SIZED to the candidate caps by `default_grader_deadline_s`
— see §6, finding 2-3) with
`reset(task_id) -> Observation` (the installed PUBLIC task: `task_id`,
`attempt_root`, `task_dir`, `elt_dir`, the documentation text and the public
file listing — never a release root, a private path, gold, a hidden
population or a count) and `step(artifact) -> StepResult` (writes the
`elt/`-confined artifact through the public `WorkspaceArtifactFile` path
rule, seals with `seal_workspace` after the policy is gone, scores through
the public `score_workspace` on the complete hidden graded set and projects
with `training_signal`; `done` is always true; a second `step` before
`reset` is refused). A refused artifact (a path escape, an empty artifact)
is a classified `WorkspaceScoreResult` built from the public models — a
measured 0.0 — and the grader never runs. THE OUTER SUPERVISOR DEADLINE for
the in-process grader phases lives here (`supervised_score`, roadmap 2.b;
Output 13 OQ-29): a deadline is `ToolDeadlineExceeded`, a raising grader is
`ToolHarnessFault` (class name only), a non-result is a harness fault too;
each yields a `StepResult` with `harness_fault` set, `result` None and an
`unlabelled_signal` (label `None`, NEVER 0.0), so a stalling artifact can
convert a certain zero only into a dropped group. `close()` removes exactly
the episode tree `reset` created; sealed artifacts of earlier episodes are
retained (pilot evidence). No `train` CLI verb exists.

Contract tests (tests/test_training_signal.py, tests/test_training_env.py):
`test_training_signal_el_only_lies_strictly_between_fail_and_e2e`,
`test_training_signal_policy_violation_zeroes_el_credit`,
`test_training_signal_none_label_sets_label_valid_false_and_no_reward`,
`test_training_signal_reward_is_pure_function_of_result_json`,
`test_training_signal_workspace_violation_is_read_from_result_failure`,
`test_rollout_adapter_drops_entire_group_when_any_member_is_unlabelled`,
`test_declarative_env_step_seals_and_scores_without_tools`,
`test_grader_deadline_yields_none_never_zero`, plus
`test_planted_infeasible_canary_rewards_abort_and_forces_a_pass_to_zero`,
`test_first_failed_phase_follows_the_binding_population_in_scorer_order`,
`test_refused_artifact_is_a_measured_zero_before_any_grader_runs`.

### The configuration: `roles.independent_implementer.session` and `roles.independent_loader.session`

`config/agents.yaml` (mirrored in `providers.DEFAULT_ROUTING_DOC`) declares
the two witness blocks of SoT T1 / roadmap Table 2, both `enabled: false`:
the implementer's `max_turns: 4`, `max_tool_calls: 6`, `per_tool:
{dev_query: 8, dry_run_sql: 4, run_mart_sql_dev: 2}`, `max_usd: 0.50`,
`wall_clock_s: 600`, `hard_caps: {turns: 8, tool_calls: 14, usd: 0.50,
wall_clock_s: 1200}`; the loader's `max_turns: 2`, `max_tool_calls: 2`,
`max_usd: 0.20`, `wall_clock_s: 600`, `hard_caps: {turns: 2, tool_calls: 2,
usd: 0.20, wall_clock_s: 600}`. Both roles join
`providers.SESSION_RUNNER_ROLES` (mirrored by `registry._DECLARED_ROLES`,
2.a). The fold: a declared-but-disabled block of the two WITNESS roles
(`providers.WITNESS_RUNNER_ROLES`) folds to exactly `{}` in the manifest
(`role_manifest_limits`), not to the author's and proposer's `{"enabled":
false}` of Phase 1 — their one-shot exchanges are the gates' recorded replay
evidence (the committed `tests/fixtures/transcripts/independent_implementer/`
fixture and every drive store), all keyed under no declaration, and the fold
is what keeps every one of those keys unmoved while the SoT block is
declared. Editing a disabled witness block moves nothing; enabling it moves
the digest and the keys (a session is a different behaviour and its
trajectories are seeded whole). Pinned by
`test_witness_blocks_follow_the_sot_and_fold_to_empty_while_disabled` and
`test_disabled_witness_block_keeps_the_committed_implementer_fixture_key`
(tests/test_providers.py), by the unchanged
tests/test_transcript_fixture_migration.py, and the SoT R0.1 refusal of a
default above its cap holds for a disabled block too.

### The CLI seams

`cli.cmd_record_transcripts` seeds WHOLE TRAJECTORIES for a witness whose
session is enabled in the agents document the routing was loaded from: no
one-shot exchange is recorded for it (a tool session could never look one
up); in the second pass, against the frozen reference, the same entry point
the gates stage replays (`run_independent_build` /
`run_independent_load_build`, every turn recorded through
`provider.run_session`) is run, and nothing is written as build evidence
(that is the gates stage's, at gate time). With the shipped `enabled: false`
the seed is the one one-shot exchange, byte-identical. Pinned by
`test_record_transcripts_seeds_whole_trajectories`.
`cli._ensure_independent_build` catches `review.session.SessionFault` beside
the transport errors and returns the note `independent build not performed:
<ClassName>: …` with nothing recorded, so `cli._transport_marker` lifts the
class name and the gates stage halts as could-not-measure (exit 2, no repair
round, nothing rejected; C7) instead of going red; the loader's
`_ensure_el_evidence` already recorded every producer exception by class
name. Pinned by `test_session_fault_in_build_is_could_not_measure_not_red_gate`.

### The records

`docs/experiments/PILOT-P2.md` and `docs/experiments/PILOT-P3.md` are the
pre-registration skeletons of Output 11 §11.1.5 in the directory README's
nine-item order (hypothesis, arms with caps and the F1 override for P2's arm
C, cohort digest placeholder, `K` and seed, primary metric and test, adopt
thresholds and vetoes, exclusion rules, budget cap, the routing fingerprint
per arm with arm A's digests read from the code); P3 states that OQ-23
option C is in force. `docs/INTERFACES.md` gains the `training/signal.py`
and `training/env.py` entries, the witness-session and `dev_query`
paragraphs and the C5 wording; `README.md` names the witness session keys
and the C5 wording. `docs/plans/cloud_free_elt_agent_rlvr.md` joins
`VOCABULARY_DOCS` in tests/test_docs_consistency.py (its three
vendor-runtime-shaped phrases were reworded as explicit denials), and this
log joins `CURRENT_DOCS`.

### 2.a — recorded by its own tests

The implementer and loader tools (`dry_run_sql`, `run_mart_sql_dev`,
`dev_query`, `list_schemas`, `submit_sql_by_mart`, `abort`;
`replace_load_plan` with `check_load_plan` auto-run, `submit_load_plan`,
`abort`), `registry._DECLARED_ROLES` gaining both roles, the projection
changes of §0, and the witness sessions inside `reference/independent.py`
(`provider.run_session` per sample with `MAX_SAMPLES` as the number of
sessions, `_assert_cross_family` before the session and on the served model
of every turn, the certifier `parse_sql_by_mart` then `evaluate_build`
unchanged, samples gaining `turns`, `tool_calls[{name, args_sha256,
output_sha256, sanitizer_version}]`, `build_harness_version`, the manifest
digest and the defaulted limits) are the concurrent 2.a work; their contract
is the 2.a test list of roadmap §5 and the two tests the constraint addendum
A23 requires.

## 2. Sanctioned behaviour changes

The brief's rule is that every existing one-shot path stays byte-identical
unless the roadmap sanctions the change. These are the observable changes
introduced by the half of Phase 2 this log records.

* **A witness harness fault is could-not-measure, not a stage crash and
  not a red gate (C7; roadmap Table 6 `cli.py` row).** Before, a
  `SessionFault` raised by the build worker's supervisor (a worker death,
  the trusted load's deadline) propagated out of the gates stage runner and
  the engine classified it as infrastructure through the MRO — the same
  disposition (halt, exit 2, no round) but as an opaque stage error with
  nothing persisted. Now `_ensure_independent_build` records nothing, the
  note names the class and survives in `reports/independent_build_notes.json`,
  and the gates runner's FAIL outcome carries the marker. Pinned by
  `test_session_fault_in_build_is_could_not_measure_not_red_gate`.
* **No one-shot transcript key moved.** Declaring the two witness blocks
  would have re-keyed the implementer's and loader's one-shot exchanges under
  the Phase 1 fold; the witness fold to `{}` keeps `role_behavior_sha256`,
  every transcript key, the committed fixture and the drive stores exactly
  where they were. Pinned by
  `test_disabled_witness_block_keeps_the_committed_implementer_fixture_key`.
* **`record-transcripts` prints one more line per enabled witness** (which
  seed is taken and why). Pinned by
  `test_record_transcripts_seeds_whole_trajectories` (the shipped-config leg)
  and the unchanged `RecordTranscriptsAdmissionGateTest`.
* **The seeding try is SPLIT, so the two failure paths print two distinct
  WARNINGs (finding 1-0).** Whole-trajectory seeding needs the frozen-reference
  read (`load_gold` / `begin_task_evidence`) to be a *separate* failure point
  from the variant emit / loader seed (`emit_variant` / `complete`), so an
  enabled witness can tell which stage could not be seeded. The consequence,
  observable even while both blocks are DISABLED, is that a `load_gold` /
  `begin_task_evidence` failure now prints
  `WARNING: <roles> seeding FAILED before the frozen reference could be read
  (<Cls>: <exc>); …` instead of the pre-Phase-2 single WARNING (which named
  only `independent_loader` and appended `— el-independent-load will be RED and
  the EXTRACT_LOAD variant refused variant-locally`). Control flow and the
  exit code (0) are unchanged; only the WARNING text on that error path moved.
  This supersedes the earlier "nothing else changes while both blocks are
  disabled" wording for that one error path. Both WARNING shapes are pinned by
  `test_record_transcripts_warnings_have_two_distinct_shapes`.

## 3. Deferred: item 2.c

Output 15 of the report defers 2.c — the dbt profile hardening behind
`training.dbt_profile_hardening: off` (`enable_external_access: false`,
extension autoload off, `memory_limit`, `threads: 1`,
`max_temp_directory_size: '0B'`, then `lock_configuration`), the
`run_dbt_dev_probe` composition with a fresh `state_dir` outside `task/`, the
frozen `_expected_raw_state` per (task, population), `verify_action_trace`,
`write_candidate_file` refusing `macros/`, the `dev_check` oracle budget, the
compiled-SQL policy check and `run_results.json` retention — to a Tier A or
Tier B host that does not exist. Nothing of it is built here; its eight
tests (`test_dbt_profile_locks_duckdb_config` through
`test_compiled_sql_policy_and_run_results_retained`) are owed with it, and
2.c is done only when a host attests tier A or B, `make workspace-acceptance`
is green there with hardening on, and an agent-solved development task
replays on hidden populations. No `train` CLI verb ships before it.

## 4. Engineering follow-ups

* `registry._DECLARED_ROLES` must name both witness roles (2.a) — the pin
  `test_runner_roles_mirror_the_registry_declaration` holds
  `providers.SESSION_RUNNER_ROLES` equal to it.
* `docs/plans/tinker_elt_rl_environment.md` (roadmap §5 Docs row) is to be
  updated for the declarative adapter: `DeclarativeEltEnv.step` returns the
  one terminal `StepResult`, its `signal.reward` is the `Env.step()` reward,
  and the `EnvGroupBuilder` applies `drop_group_if_unlabelled` — discard and
  rerun the whole group with fresh environments, never a spliced replacement.
* The trainer adapters of Output 8 §8.2 (TRL, verl, prime-rl, SkyRL, rLLM,
  Tinker) read the precomputed signal only; none is written here.

## 5. Owner actions

These are the owner's; nothing here was run, and no live command, credential
or network call is part of this change set.

1. **Metered live smoke per backend before P2 / P3.** One
   `run_independent_build` and one `run_independent_load_build` per source
   backend (postgres, mongodb, rest, s3, files) with both witness sessions
   enabled in a pilot-only agents document, the resolved openai_compat model
   id priced in `providers.openai_compat.pricing`, the meter printed, and
   every recorded turn checked for zero `dev_pass` bits, zero hidden counts,
   zero sanitizer tripwires and a cross-family model on every turn.
2. **Run the pilots P2 and P3** as pre-registered
   (`docs/experiments/PILOT-P2.md`, `docs/experiments/PILOT-P3.md`): fill the
   `TBD` cohort digest, seed and arm-B/C fingerprints at the start, commit
   the `pilot_stats` module under `tools/` with its tests and run the $0
   dress rehearsal on the offline doubles first; adopt by §6 of each file;
   retain the sealed P2 and P3 artifacts (at least 20 locally accepted and
   10 rejected per arm) for the Phase 5 parity sample.
3. **Earn the fresh-live admission under harness "5" (schema 4)** — the
   Phase 0 log §3 item 3 and the Phase 1 log §3 item 1 — and re-record the
   fixture set live (`elt-taskgen record-transcripts`, behind the admission
   gate, with credentials); with a witness session enabled that command seeds
   whole trajectories from the frozen reference (two passes, as for the
   loader today).
4. **Rotate and purge the canary credential copies** under
   `runs/runtime_canary_20260901/*/live/` and
   `runs/runtime_canary_20260902/*_adversarial/live/*/attempt/task/`, run the
   gated sweep (`ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1 .venv/bin/python -m
   unittest tests.test_credential_sweep`) and keep it green; decide on the
   legacy Snowflake password in the five `__el` public releases before any
   of their bundles is shown to a loader session (Phase 1 log §3 item 4).
5. **Schedule the `scorer_version` change of the DuckDB re-pin** (the 1.4.x
   LTS end of life, Output 13 OQ-16): `signal_version` and `scorer_version`
   are on every signal now, so a re-pin is a versioned change of both
   strings, never a silent one.

## 6. Review-finding remediation (2026-09-03)

The Phase 2 implementation review raised eleven findings and one test-suite
failure; each was fixed under the concurrent-effort fence (no edit to any
existing `training/**` or `export/**` file, `scorer.py` and `workspace.py`
included). Where a finding's *primary* fix lands in a fenced file, the
compensating fix was made in a file this effort owns and the residual is
recorded here as owed to the concurrent effort.

### dev_query hardening (findings 0-0, 0-1, 3-0, 3-1, 3-2)

`training/dev_tool.py` `_validate_query` now validates on the sqlglot AST, not
raw text: every `exp.Table` must resolve to a DEVELOPMENT source BASE TABLE of
`task.tables` (or a CTE defined in the same query), a table-valued function in
`FROM` (`duckdb_databases()`, `range()`, `read_csv(...)`, and their
quoted/comment-split spellings), a catalog/db-qualified or system-schema
relation (`information_schema.*`, `pg_catalog.*`), and every catalog/settings/
secret function (`duckdb_*`, `pragma_*`, `current_setting`, `version()` →
`exp.CurrentVersion`, `current_database`, `current_schema[s]`, `which_secret`,
…) are refused with the measured code `external_access` — closing the
hex()/base64()/replace() path-laundering the raw-regex layers could not.
`_open_dev_connection` now mirrors `duckdb_sandbox.sandboxed_memory_connection`
(external access off, extensions off, `secret_directory`/`home_directory`/
`extension_directory` emptied, no temp spill) before it locks the config, as
defence in depth. `run_dev_query` runs the query in a spawned, killable worker
(like `dry_run_sql`/`run_mart_sql_dev`), so a runaway query leaves no locked
connection on the shared warehouse file and its deadline is the measured
`execution_timeout` code (the child budget sits a second under the harness
tool wall); non-finite floats are coerced to NULL — a non-finite float NESTED
inside a LIST / STRUCT / MAP cell is nulled in the worker (`_finite_nested`)
before `to_scalar`'s `canonical_json` can raise it out as a `worker_fault`, and
a cell no coercion can carry is the measured `invalid_query` — an over-long
cell is truncated, and a non-identifier or non-public output column is the
correctable `invalid_query`, never a harness fault or a leak tripwire. Owner
decision on the re-check's open point: a path-, secret- or executor-text-shaped
literal the model echoes into a cell (`SELECT '/Users/…' AS customer_name`) is
refused in `DevQueryTool.run` as `invalid_query` BEFORE
`serialize_for_transport` (`projection.dev_rows_leak_shape`), so a witness
cannot void its own session as a `LEAK_TRIPWIRE` at will; the projector's
detectors are unchanged behind the refusal. A relation outside `task.tables`
is now `invalid_query` rather than the DuckDB-execution `query_failed`;
`test_dev_query_never_returns_marts_or_gold_tables` is re-pinned to the
stronger code (it still proves marts/gold are never returned). New
regressions: `test_dev_query_refuses_catalog_and_pragma_functions`,
`test_dev_query_path_launder_variants_are_refused_not_delivered`,
`test_dev_query_benign_queries_return_codes_not_faults` (now including the
nested `['nan'::DOUBLE]`, `{'x': 'inf'::DOUBLE}` and `MAP {'k': 'nan'::DOUBLE}`
cells),
`test_dev_query_echoed_path_or_secret_literal_is_invalid_query_not_a_tripwire`,
`test_dev_query_runaway_does_not_poison_a_later_session`.

### The runner never leaks a raw exception, and the scored clock is candidate-only (findings 2-1, 2-4)

`review/session.py` `_execute` wraps any exception the tool/gatekeeper did not
type as a `SessionFault`/`PolicyFault` into a `ToolHarnessFault` before it
leaves the runner, and `RoutedProvider.run_session` (`review/providers.py`)
wraps both an unclassified escape from `run_bounded_session` and a post-session
store-write `OSError` (a `ProviderFault`), so the engine never receives a raw
exception to keyword-route on its text. `tool_wall_ms` (the scored
`LIMIT_WORKING` clock) is now charged from the MEASURED duration of the
answering/refusing worker attempt only (`_dispatch` times each attempt on
`self.clock`): a retried/dead attempt, the D1 sanitizer and a harness-fault
dispatch no longer inflate it. Regressions:
`test_unclassified_worker_exception_leaves_the_runner_as_a_harness_fault`,
`test_tool_wall_ms_charges_only_the_answering_attempt` (a FakeClock and a
policy-role session: the OOM-kill re-dispatch at the finding's own numbers —
535 s + a dead 4 s attempt + a 2 s respawn + a 3 s answer charges 538 s and
the session continues where the old sum of 544 s stopped it as a scored
LIMIT_WORKING — the transient `runtime_unavailable` retry, the
`ToolDeadlineExceeded` stall and the second `SandboxFault` halting as
HARNESS_FAULT with `tool_wall_ms` unchanged, a patched `assert_value_free`
that advances the clock being excluded, and a genuinely long answering
attempt still binding).
Residual: the deeper "measured worker time reported via `WorkerResult`"
refinement (which would also exclude the tool's own `_run_in_spawned_worker`
spawn latency) is deferred with POL — `LIMIT_WORKING` is disabled for every
role active in Phase 2 (all are in `COUNCIL_ROLES_WITHOUT_WORKING_LIMIT`), so
the harm is latent, and the WorkerResult approach conflicts with the
fake-clock test harness the runner is exercised under.

### The SQL PERMIT pre-filter and load-plan paths (findings 3-3, 3-4)

`_forbidden_argument` scans the forbidden-keyword regex over a copy with
single-quoted string LITERALS and comments blanked (`_sql_keyword_scan_text`),
so `WHERE x = 'set'` and `-- load rows` are no longer terminal violations while
a genuine `SET`/`LOAD`/`COPY` statement still is and a private-tree token in a
literal is still denied. `_path_argument_problem` refuses a NUL byte or a
backslash, and `validators._check_load_plan_static` wraps `Path(...).resolve()`
in `try/except (ValueError, OSError)` returning `('path_escape', table)`, so a
NUL-laden load-plan path is `path_escape`, never the `ValueError: embedded null
byte` harness fault. Regressions:
`test_forbidden_sql_ignores_literals_and_comments`,
`test_path_argument_rejects_nul_byte_and_backslash`,
`test_check_load_plan_nul_byte_path_is_path_escape_not_a_harness_fault`.

### The declarative env classifies harness I/O and policy-shaped artifacts (findings 2-0, 2-2)

`training/env.py` `_normalize_artifact` refuses the shapes that used to crash
the writer — the bare `elt` directory key, a key that is a proper ancestor of
another (`elt/models` beside `elt/models/a.sql`), a case-folded duplicate
(`elt/Main.tf` vs `elt/main.tf`), and a non-bytes value — as a classified
`StepResult` (`WORKSPACE_PATH_ESCAPE`/`WORKSPACE_CASE_COLLISION`/
`SUBMISSION_INVALID`), never an `IsADirectoryError`/`FileExistsError`/
`TypeError`. The re-check's residuals are closed the same way: a NUL byte or
any C0 control character (or DEL) in a key (formerly `ValueError: embedded
null byte` escaping `step` with the episode already marked stepped) and a
path component over `NAME_MAX` (255 UTF-8 bytes; formerly `ENAMETOOLONG`
caught as the harness-fault `artifact_write_io` — a dropped group a policy
could farm from its own file name) are measured `WORKSPACE_PATH_ESCAPE`
before any write; every path is NFC-normalized before the case-folded
comparison, an NFC/NFD collision is `WORKSPACE_CASE_COLLISION` on every host
(APFS collapses the pair into one file, ext4 writes two), and the NFC form
is what is written so the sealed manifest is host-independent. `_write_artifact`
narrows its error handling: a `ValueError` or an `OSError` whose errno is
policy-attributable (`ENAMETOOLONG`, `EINVAL`) is the measured
`WORKSPACE_PATH_ESCAPE`, while `ENOSPC`/`EACCES`/`EIO`/`EMFILE` stay the
`SandboxFault` `artifact_write_io` harness fault (reward `None`). `step`
splits its try so that harness fault, and a seal-time `WorkspaceLifecycleError`
that is EITHER `OSError`-caused (a transient re-hash of the PUBLIC tree the
policy never touches) OR carries a code in `_HARNESS_ONLY_WORKSPACE_CODES`
(`_verify_attempt` swallows the `OSError` of a failed `resolve(strict=True)`
and fails `PATH_ESCAPE` with no cause — re-check probe C), become an
UNLABELLED harness `StepResult`; and a workspace code the grader returns at
REPLAY time — `_HARNESS_ONLY_REPLAY_CODES`: the structural set
(`WORKSPACE_PUBLIC_MUTATED`, path escape, symlink, non-regular file) plus
`SUBMISSION_INVALID`, `WORKSPACE_DIGEST_MISMATCH` and `TASK_ID_MISMATCH`,
which after the env's own accepted seal can only be the harness's sealed
directory or an I/O fault re-reading it (probe D: `os.scandir` `EMFILE` inside
`load_sealed_workspace`) — is reclassified from a 0.0 policy label to a harness
fault. Regressions:
`test_policy_shaped_artifacts_are_classified_never_crash_the_env`,
`test_nul_byte_key_is_a_measured_path_escape_never_a_crash`,
`test_over_long_path_component_is_a_measured_label_not_a_harness_fault`,
`test_normalization_collision_is_a_case_collision_on_every_host`,
`test_artifact_write_io_fault_is_a_harness_fault_not_a_zero`,
`test_transient_io_fault_at_seal_is_a_harness_fault_not_a_zero`,
`test_seal_time_resolve_io_fault_is_a_harness_fault_not_a_zero`,
`test_replay_time_io_fault_inside_the_real_grader_is_a_harness_fault` (a real
`OSError` at each of the four replay sites through the real `score_workspace`
with the orchestration doubles, the `load_sealed_workspace` scandir site
included), `test_grader_returned_public_mutation_is_a_harness_fault_not_a_zero`.
Residual (owner hand-off, §7): the finding's *primary* fix in the fenced
`training/workspace.py` is still owed so that consumers OTHER than the
declarative env (`cli.py`'s `seal_workspace`/`score_workspace` callers) stop
receiving a 0.0 for the same I/O faults.

### The grader deadline is sized to the candidate caps (finding 2-3)

`DeclarativeEltEnv`'s default deadline is now `default_grader_deadline_s`,
sized as `graded_populations x (3 x command_timeout + trusted_budget) + slack`
(floored at the historical 1800 s), so a candidate that hits all of its own
pre-declared dbt caps still lands a measured `dbt_timeout` 0.0 inside the
window instead of a dropped group. Regression:
`test_grader_deadline_is_sized_above_worst_case_candidate_caps`.
The cascade half is closed in `env.py` alone (`scorer.py` untouched): with no
scorer injected, the REAL public `score_workspace` runs in an ENV-OWNED CHILD
PROCESS (`score_in_child`, `GraderChildSpec`; `start_new_session=True`, its
own session and process group). The child bootstraps through `python -c`,
reloads the package with the public `load_workspace_package(release_dir,
task_id, verify=…)` and calls the public `score_workspace(package,
sealed_dir_path, attempts_root=…, runtime_config=…, dbt_limits=…,
expected_seal_sha256=seal_sha256)` — the path form the scorer already
authenticates by the harness-retained seal digest — writing
`result.model_dump_json()` atomically to a file beneath the episode's
`supervisor/` directory. At the deadline the supervisor `os.killpg`s the
child's process group, then (best effort, from a `ps` listing gathered before
the kill) every descendant the grader detached into its own session the way
`dbt_runner` spawns dbt, and WAITS, so no grader thread and no dbt subprocess
survives a trip to contend with the group's re-sample; the step is
`ToolDeadlineExceeded`, label `None`. A child that raised reports the
exception CLASS only (`ToolHarnessFault`, `harness_exception`), a non-result
is `non_result`, a child that died without reporting is `SandboxFault`
`grader_child_died`; nothing the child prints is read into a label (stderr is
retained beside the spec for the owner). The in-process thread supervisor
(`supervised_score`) remains only for an injected scorer double (a test's
orchestration doubles wired through the public entry point), which a thread
cannot kill — the reason the real grader is a process. `grader_entry`
(`module:function`, default `GRADER_ENTRY` = the public grader) is the test
seam that lets a child run an offline double. Regressions:
`test_default_grader_runs_the_public_scorer_in_an_env_owned_child`,
`test_grader_child_faults_are_reported_by_class_never_by_text`,
`test_grader_deadline_kills_the_env_owned_child_process_group` (a child that
spawns one grandchild in its own group and one detached session, trips the
deadline; both pids and the child are dead afterwards and no `-grader` thread
is alive). Residual: a per-POPULATION deadline is still not implementable
without editing `scorer.py` (it refuses a caller-selected subset), and the
re-check's optional refinements — deriving the worst-case candidate time in
the sizing pin from the runner's command tuple, and surfacing an
already-recorded `dbt_timeout` as the measured label when a trip lands after
it — are deferred with the 2.c host.

### The group-drop rule now has an in-repo caller (finding 2-5)

`training/adapter.py` ships `EnvGroupBuilder`, the caller of
`drop_group_if_unlabelled` the plan named: it samples `group_size` rollouts of
one task, drops and re-samples the WHOLE group with fresh state when any member
is unlabelled, and hands the trainer only fully-labelled groups. Reward is read
through the new `WorkspaceTrainingSignal.require_label`, which raises
`UnlabelledSignalError` rather than let a `None` reward be read as a number.
Regressions (`tests/test_training_adapter.py`):
`test_env_group_builder_never_hands_a_dropped_group_member_to_the_trainer`,
`test_env_group_builder_raises_when_a_group_never_labels`,
`test_require_label_refuses_to_read_a_none_reward_as_a_number`.

### Non-UTF-8 text artifacts (finding 2-2, residual)

`DeclarativeEltEnv._normalize_artifact` encodes text values to UTF-8 before writing. A value containing a lone surrogate raised `UnicodeEncodeError` out of `step`, an unclassified crash with no `StepResult`. It is the policy's own bytes, so it is now a measured `SUBMISSION_INVALID` (label valid, reward 0.0, grader not run), never a harness fault and never a dropped group. Pinned by `tests/test_training_env.py::test_non_utf8_text_value_is_a_measured_submission_invalid_never_a_crash`.

## 7. Owner hand-off to the training/export effort

The following change belongs to the concurrent effort that owns
`src/elt_taskgen/training/workspace.py` (the concurrent-effort fence of the
Phase 2 brief), and is recorded here rather than made:

* **Map every non-`ELOOP` `OSError` in `training/workspace.py` to
  `HARNESS_INTERNAL`, raised `from` the `OSError`,** at these sites:
  `_bounded_static_entries` (today `WORKSPACE_PUBLIC_MUTATED`),
  `_bounded_file_digest` (`WORKSPACE_PUBLIC_MUTATED`),
  `_bounded_candidate_entries` (`WORKSPACE_NON_REGULAR_FILE`),
  `_read_bounded_regular_file` (`WORKSPACE_NON_REGULAR_FILE`),
  `_verify_attempt` (the swallowed `resolve(strict=True)` failure that is
  reported as a deliberate `_fail(WORKSPACE_PATH_ESCAPE)` with no cause) and
  `load_sealed_workspace` (both the `resolve` and the `os.scandir` sites,
  today `SUBMISSION_INVALID`). Keep the `ELOOP` -> `WORKSPACE_SYMLINK`
  mapping, which is the candidate's doing.
* **Reason.** Those codes are label-eligible policy classes in
  `WORKSPACE_ERROR_CLASS_BY_CODE`, so a transient host fault (`EMFILE`,
  `EIO`, an NFS hiccup) re-hashing the PUBLIC tree or re-reading the sealed
  directory becomes a 0.0 policy violation for every consumer of
  `seal_workspace`/`score_workspace` OTHER than the declarative env —
  `cli.py`'s workspace commands included. The declarative env compensates
  in `env.py` (the `__cause__` guard, `_HARNESS_ONLY_WORKSPACE_CODES` at
  seal, `_HARNESS_ONLY_REPLAY_CODES` at replay; §6 finding 2-0), but that
  compensation cannot reach the other callers, and a `HARNESS_INTERNAL`
  raised from the `OSError` would let `scorer.py`'s existing
  `_failure_result` path yield reward `None` for it with no scorer change.
  `WORKSPACE_SCORER_VERSION` is unaffected: the change moves a harness fault
  out of the label-eligible classes, it does not change any measured label.
* **Tests to carry with it.** The four replay-site cases of
  `test_replay_time_io_fault_inside_the_real_grader_is_a_harness_fault` and
  the seal-site case of
  `test_seal_time_resolve_io_fault_is_a_harness_fault_not_a_zero` (both in
  `tests/test_training_env.py`) already trip a real `OSError` at each site
  through the real `score_workspace`; once `workspace.py` raises
  `HARNESS_INTERNAL` there, the env's `_HARNESS_ONLY_REPLAY_CODES` widening
  becomes redundant for those sites and may be narrowed back to the
  structural set.

# Phase 4 of the bounded-agents roadmap: the harness-6 metrology protocol

Status: historical phase log, shipped 2026-09-04. At this phase boundary every
critic seat was still `enabled: false`; the later admitted default flip enabled
the population and shortcut sessions while ambiguity and feasibility remain
one-shot. `config/agents.yaml` is the current authority. What moved in this
phase was the protocol a seat would be measured under, plus the specimen pool.
Revised 2026-09-04 by the adversarial-review repair pass — §4b is what that pass
changed, and §1 item 5 records the then-shipped `--workers` behaviour.

Read the earlier logs first: `bounded_agents_phase0.md` (projection layer, fault
taxonomy, metering, fingerprint v5), `bounded_agents_phase1.md` (the bounded
runner, the author session, the F1 fix, the agentic proposer),
`bounded_agents_phase2.md` (the witness sessions under OQ-23 option C, the
training signal and the declarative environment) and `bounded_agents_phase3.md`
(the POPULATION route, the default-off critic validators, the correction
channel).

## 1. What shipped, per item

### Item 1 — the metrology protocol (`review/metrology.py`)

`HARNESS_VERSION` moved `"5"` to `"6"`; `ADMISSION_SCHEMA` stays 4. A schema-4
record earned under `"5"` is now SUPERSEDED, exactly as a schema-2 record is
(`test_harness_6_supersedes_every_harness_5_record`). `_COST_NOTE` is
`"~$25 per run at list rates with caching; use --budget-per-task 60"`, and the
CLI derives both its metrology help text and the `--budget-per-task` help from
that one string, so the operator-facing figures cannot drift
(`test_budget_guidance_is_synchronised`).

Per-trial isolation: `TrialContext` and `trial_workspace(trial, *, root)` give
every trial a nonce-only temporary workspace holding the public projection only,
asserted free of private material at entry and torn down unconditionally at
exit. The path carries no seed and no trial index, so a seat cannot read its own
position out of its scratch directory. `run_metrology` calls `begin_trial(ctx)`
and `end_trial()` on any provider that exposes them (duck-typed, so
`council.py`'s `Provider` protocol is untouched).

Evidence: one trajectory per (trial, seat), `SpecimenResult.trajectory_by_role`,
and `RoleMetrics` carrying the efficiency and integrity counters — `nudge_count`,
`validator_run_count`, canary hits by kind, private probes, the policy-violation
upper bound, the stuck and limit-stopped ratios — beside the measurement fields
`pass_k`, `icc`, `n_eff` and the `position_dependence` probe. `n_eff` is
`trials / (1 + (K - 1) * rho)`: replicates of one specimen are not independent
trials and the report says so in the number. The integrity counters BLOCK; the
efficiency ratios are advisory and, per the house rule, can only ever make
admission harder.

### Item 2 — the fixture families (`review/metrology_fixtures/`, new package)

The pool no longer derives from one fixture. `FAMILY_NAMES` is `("demo",
"clinic_visits", "stock_ledger")`: the demo task wrapped, plus two new star
schemas built through the same generator path with different domains, table
counts, key shapes and mart plans. Each family carries per-role `InjectorAnchor`
lists naming the public identifiers the existing injectors target, its own
specimen builders, and its own canary GUID. `select_trials` stratifies the draw
over the families with a per-family holdout, and `pool_sha256` covers every
family, so the pool digest moves when a family does.

This is what makes the re-earn measure something: on a single-fixture pool a
seat that has seen the demo task cannot be distinguished from a seat that reads
carefully.

### Item 3 — the provider trial seam (`review/providers.py`)

`begin_trial` / `end_trial`; a `ToolExecutor` per trial with no cache and a fresh
validator run each time, so nothing crosses a trial boundary.
`RoutedProvider.complete` dispatches to `run_bounded_session` only when the
role's session is enabled AND a trial or task context is active, returning the
final normalized findings text; with every seat disabled the one-shot path is
byte-identical. Trajectories are content-addressed at
`<ws>/transcripts/trajectories/<role>/<trajectory_sha256>.json`, and raw
validator output lives in a `tool_raw/` tier OUTSIDE the transcript store, so a
raw byte can never reach a transcript. A `SessionPolicyViolation` under a trial
is a scored seat outcome, never exit 2.

### Item 4 — the council entry (`review/council.py`)

`run_council`, `_parse_findings`, `screen_findings` and the FATAL leak
short-circuit are byte-identical, pinned on the function source.
`findings_from_session(task, role, result, session, id_suffix)` is added as the
ONLY path from a critic `SessionResult` to `screen_findings`: it parses, voids
uncompilable proposals through the Phase 3 projection, then screens. This closes
the Phase 3 hand-off, where the void existed as a function with no production
caller.

### Item 5 — the CLI (`cli.py`)

`_validated_review_manifest` accepts one session row per critic role with the
count identity `model_call_count = tool_call_count + refused_count +
nudge_count + correction_count + terminal_count + limit_stop_count`, with
`attempt_count` aliased for one release so a one-shot row still validates.
`_cmd_metrology_measured` prints the efficiency and integrity lines, the canary
flags, ICC and `n_eff`. `--workers N` runs the trials through N
`RoutedProvider`s over ONE routing, transcript store and cost meter
(`providers.RoutedProviderPool`, OQ-20), with the evidence sorted by trial
index before hashing, pinned by a determinism test that the manifest digest is
identical at 1 and 8 workers. **The trial loop is SERIAL**: `run_metrology`
dispatches `for index, trial in enumerate(schedule)` and nothing in the tree
runs two trials at once, which the pool's own docstring says
("the loop in this tree dispatches trials serially, so the pool is the seam
concurrency plugs into"). `--workers N` is therefore that seam and a
per-worker isolation of the provider state, **not a speed-up**: an earlier
revision of this log claimed it "dispatches trials in parallel", and the
metrology redesign's "8-way about 15 to 25 min" wall figure against a serial
1.5 to 3 h run rests on concurrency that has not been written. Before the
loop can be made concurrent, `RoutedProviderPool._current` (one shared slot
set in `begin_trial` and cleared in `end_trial`) has to become thread-local
and the meter has to be taken under `RoutedProviderPool.lock`.
`--diagnostic-order-check` and `admission audit --revoked` are added.

### Item 6 — doubles and proofs

`tests/test_metrology_doubles.py` carries the agentic doubles: a diligent oracle
is admitted; the replay, echo, flooding, boilerplate, stuck, budget-burner,
policy-probe and private-sniffing doubles are each blocked or measured as the
design says; a leak probe trips before transport with exit 2 and nothing written
or revoked; a crashing executor exits 2.
`tools/prove_admission_integrity.py` gained schema-4 markers with a non-zero
validator-run count, PROOF 5 mutating a critic-wired validator projection
description, and PROOF 6 extended to stale results. It prints ALL PROOFS HELD.

## 2. Sanctioned behaviour changes

| Change | Why it is sanctioned | Pinned by |
|---|---|---|
| `HARNESS_VERSION` `"5"` to `"6"` | C3: the protocol changed (trajectory evidence, families, integrity bars, manifest relaxation). Version strings only go up. | `test_harness_6_supersedes_every_harness_5_record` |
| `pool_sha256` moves | The pool now spans three families | the pool tests |
| `_COST_NOTE`, the metrology help and `--budget-per-task` help | One derived string; the run costs about $25 at list with caching | `test_budget_guidance_is_synchronised` |
| The review manifest accepts a session row | OQ-19, needed before any seat can be flipped; one-shot rows still validate through the alias | `test_review_manifest_accepts_one_trajectory_per_role_with_count_invariants`, `test_review_manifest_attempt_count_alias_holds_one_release` |
| `audit --revoked`, `--diagnostic-order-check`, `--workers` | Additive operator surface | the CLI tests |

With every seat disabled, the four critic wire manifests, their behaviour
digests and their one-shot transcript keys are unchanged from the Phase 3 end
state. `council_routing_fingerprint` differs only in `harness_version`,
`pool_sha256` and the observable-state digest — the components the protocol
change legitimately moves.

## 3. What stays off, and why

Both validated seats (`population_adversary`, `shortcut_attacker`) ship
`enabled: false`. The pilots that justify a flip (P4 for the adversary's
pre-flight, P5 per critic seat) are live runs and belong to the owner. The
"no tools, no files" sentence in the critic prompts is removed only when a seat's
session is enabled, derived from the block, so the disabled prompt bytes are
byte-identical.

`validators.binaries` carrying duckdb and sqlglot is a consequence of a seat
being ENABLED: the binaries enter the fingerprint with the validated seat, not
before. That is why the fingerprint does not move today.

## 4. Owner actions

1. **The first fresh-live re-earn, under harness "6"**: `elt-taskgen metrology
   --workspace council --budget-per-task 60`. Because "5" was never earned, this
   is ONE paid run rather than two. It must land `canary_hits = 0`,
   `private_probe_count = 0`, a policy-violation upper bound at most 0.10 per
   seat, and the three admission bars, on the three-family pool.
2. **The seat flip**, after P4 and P5 adopt: set
   `roles.population_adversary.session.enabled: true` (and the attacker's) and
   re-earn. A seat that clears one-shot stays one-shot.
3. **The P5 pilot** (`docs/experiments/PILOT-P5.md`): four fresh-live metrology
   runs per arm at shared seeds, about $226 at tier C.
4. The Phase 0 to 3 owner actions still stand: the credential purge under
   `runs/`, the legacy Snowflake password decision, the live fixture re-record,
   the proxy-bridge network, and the P1, P2, P3, P4 and P6 pilots.

## 4b. The 2026-09-04 repair pass (adversarial review findings)

Four adversarial lenses were run over Phase 4 and Phase 5. Every finding they
raised against Phase 4 is fixed below; the Phase 5 half is in
`bounded_agents_phase5.md` §3b. Each entry names the defect, not the patch.

| # | What was wrong | What now holds | Pinned by |
|---|---|---|---|
| p4-0-0 / p4-1-0 / p4-2-3 | The blocking `max_policy_violation_ub` bar was DEAD CODE for exactly the two seats this phase ships. `_has_tool_channel` read `enabled and max_tool_calls > 0`, and a `harness_validated` seat never declares a model-initiated tool call — that is what the mode MEANS — so `_policy_violation_ub` short-circuited to 0.0 and 35 policy violations of 35 scored trials read 0.00 against a bar of 0.10. POLICY_VIOLATION is fully reachable at `max_tool_calls: 0`: any `tool_use` naming a tool the seat does not hold is `ToolNotPermitted`, and `_submit` applies the forbidden-argument rules to the submitted PAYLOAD. | The channel is "an ENABLED session". The 0.0 short-circuit survives only for a seat that runs no session at all, where the count is zero by construction. The owner's one-key seat flip is all that is needed to arm the bar. | `test_policy_violation_bar_blocks_under_the_shipped_zero_tool_block` |
| p4-1-1 | The three statistical bars were pinned in ONE direction. `admission_status` re-derived the verdict against the current config so tightening retires an admission — the converse was unguarded: the record's own recorded `thresholds` were never read back, and the `metrology:` bars are not in `council_routing_fingerprint`, so one config line retroactively admitted a blocked record with NO digest moving. The read-back also always used the repository default while the run read its bars from `--agents-config`. | `loosened_blocking_bars(recorded, current)` refuses when any current blocking bar is LOOSER than the one the run was measured under (`[bar_loosened]`), and the read-back bar comes from the same agents document the fingerprint and the loop limits do. A record carrying no recorded bar is refused, not trusted. | `test_a_loosened_bar_cannot_resurrect_a_record_its_own_bar_refused`; `tools/prove_admission_integrity.py` PROOF 4 |
| p4-2-0 | `certify`'s `on_stage(name)` was a LATCH: announced before a runner and never cleared, so every piece of harness bookkeeping between and after runners — `save_task`, `record_report`, `_record_memo_served_review`, the final green projection, `engine.close()`, the worker's IPC send — sat inside a window attributed to a completed, TOUCHED stage. A certify that ran GREEN through every stage was reported and BILLED as the model's own resource exhaustion, and the passing result discarded. | The announcement is a scoped WINDOW: `on_stage(NO_STAGE)` the moment a runner returns. `_deadline_receipt` already answers None — the harness fault — for a stage that is not running. | `test_a_deadline_outside_a_stage_runner_is_a_harness_fault` |
| p4-2-1 | `_forbidden_argument` answers the FIRST problem in the model's own JSON key order, so any earlier path- or SQL-typed argument with a NON-outranking problem shadowed the anti-evasion rules: `{"path": "/abs", "field": "populations.-1.conditions.2"}` became a free `invalid_arguments` correction with no violation and no security event, taking the absolute-path argument down with it. `_submit` never consulted the outranking set at all. | `_outranking_argument_problem` reads EVERY argument for the population and locator rules and runs before the schema at PERMIT and before the payload schema at SUBMIT. | `test_an_outranking_rule_is_not_shadowed_by_another_argument_problem` |
| p4-2-2 | `private_probe_tokens` had NO production caller: the blocking `private_probe_count <= 0` bar read the 0 default on every real run, and `duplicate_tool_calls` was dead the same way (silently deflating the advisory wasted-call ratio). Only the test double produced the counters. | `TrialToolExecutor` refuses an identifier-typed argument naming a private surface or ANOTHER trial's nonce (`private_probe`, terminal on the runner path, an outcome on the `ToolExecutor.execute` path), counts it, and both counters reach the SoT T8 evidence row the bars re-derive from. `TrialContext` carries `foreign_nonces`. | `test_private_probe_in_a_tool_argument_is_refused_and_counted`, `test_a_foreign_trial_nonce_in_a_tool_argument_is_a_private_probe` |
| p4-0-1 | Every hack canary was built from the DEMO fixture, so after the three-family diversification the blocking `canary_hits == 0` bar covered one family of three and P(canary | a clinic_visits or stock_ledger trial) was exactly ZERO. The family is trivially observable from a seat's own view, so the canary was CONDITIONALLY IDENTIFIABLE. | `canary_specimens()` builds every kind for every family (72 specimens), `_check_canary_holdout` requires `CANARY_POOL_HOLDOUT + 1` per (family, seat, kind), the private-only token folds the family in, and the impossible decoy notes are family-neutral. | `test_the_canary_pool_is_not_conditional_on_the_fixture_family`, the updated `test_private_only_canaries_are_undetectable_from_public_surfaces` |
| p4-0-2 | The per-trial workspace's `public/views/` held one file per CONSULTED seat, and `Trial.roles` is all four critics on a clean specimen and the single target seat on a tampered or canary one — a perfect discriminator of clean-vs-tampered AND of the targeted seat, in the tool root a session runs under. | `views/` holds all four `CRITIC_ROLES` on every trial. Every view is derivable from the public task, so the constant set costs nothing. | `test_trial_workspace_view_set_is_constant_across_trials` |
| p4-0-3 / p4-2-4 | One `TrialToolExecutor` is shared by every seat of a trial, and the trajectory record and the `tool_raw/` tier read `executor.runs` whole — so seat B's per-(trial, seat) evidence carried seat A's unsanitized validator bytes as `unmatched-<i>.bin`, monotonically in seat order, and a digest collision could put one seat's observation into the other's record. | Both builders see a per-session WINDOW of the shared log, filtered by `ValidatorRun.role`. | `test_trajectory_and_raw_tier_hold_only_this_seats_runs` |
| p4-1-2 | The per-trial isolation attestation was a hardcoded constant, so `admission_status`'s isolation check was circular: it could only read back what the harness wrote, and a provider with a cross-trial cache still admitted. | The three attested booleans are DERIVED from counters the loop kept (distinct nonces, teardown assertions that held, per-trial executors observed cache-free), the counters are recorded beside them, and a provider that exposes no trial seam records `cross_trial_cache: true` — fail closed. `admission_status` re-checks the observations. | `test_isolation_attestation_is_measured_not_asserted`, the updated `test_scratch_notes_do_not_survive_a_trial` and `test_run_metrology_calls_begin_and_end_trial_around_every_trial` |
| p4-1-3 | This log claimed `--workers N` "dispatches trials in parallel". Nothing in the tree runs a trial concurrently. | §1 item 5 above is restated to the shipped behaviour, and names what has to change before the loop can be made concurrent. | `test_docs_only_name_real_subcommands` (the log is in `CURRENT_DOCS`) |
| p4-2-5 | `RoutedProviderPool.exchange_evidence` answers a freshly SORTED union on every read, and the loop sliced it by a count taken before the trial — so an unindexed row (a `complete` outside a trial) made the loop score the wrong trajectory and swallow the trial's real terminal, only at `--workers > 1`. | `_rows_for_trial` selects by trial IDENTITY (nonce, then index), falling back to the positional tail only for a double whose rows carry neither key. | `test_trial_rows_are_taken_by_identity_not_by_a_positional_slice` |
| p4-2-6 | A `PolicyFault` raised by a HARNESS-run validator (`auto_validators`, `_submit`'s declared validators) or a `permit` hook escaped `run_bounded_session` BARE — no `SessionPolicyViolation`, no `session_result` — so under a trial it could not become the seat outcome C7/SoT T4 require (exit 2 instead), and in the review path it reached the engine as an unclassified `RuntimeError` to be keyword-routed on its text. | `_Session.run` carries a backstop that classifies every escaping `PolicyFault` by its own vocabulary (`OracleCapExceeded` -> `LIMIT_ORACLE`, everything else -> an attached `SessionPolicyViolation` with its security event) and always attaches the partial result. | `test_a_policy_fault_from_a_harness_run_validator_is_classified` |

Two pinned literals moved with the canary change and are re-pinned here
deliberately, in this log, as the pinning test demands: `PHASE4_FINGERPRINT`
`fb7f630df8fd…` -> `fb9acc5c1a3f…` and `PHASE0_BLOCKS_FINGERPRINT_HARNESS_6`
`64d4aeff5092…` -> `28140492a6b2…`, both through `pool_sha256` and
`view_sha256` alone (the family-wide canary pool). No admission record exists
under the previous value — the owner's harness-6 re-earn has not been run —
so the move costs nothing. `docs/INTERFACES.md` carries the new value.

## 5. Engineering follow-ups

- `metrology.build_prose` hard-codes an overview sentence naming three
  operational sources, which is false for a family with a different table count;
  the family-aware variant is a small change to `_OVERVIEW_VARIANTS`.
- The diligent-reader test over the whole pool needs a family-aware reader; the
  fixtures package exposes one, and the demo `OracleProvider` still reads only
  demo phrases.
- `projection.DIAGNOSTICS_VERSION` stays `"2"` although the certify vocabulary
  gained a member in the Phase 3 residual work; the version is recorded and never
  hashed, and a bump would re-key the two migrated fixtures for nothing. Fold a
  bump into the re-earn if the single-source-of-truth reading demands it.

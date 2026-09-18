# PILOT-P4 — population-adversary harness validation pre-registration

Status: pre-registration SKELETON, committed on 2026-09-03 before any paid
call (roadmap Phase 3 entry criterion: "`PILOT-P4.md` committed with the B0
versus B1 contrast and the bit budget"; Output 11 §11.1.5). Every value
marked `TBD` is filled in, in this file, at the pilot's start and before the
first live trial. This file contains no measurements and does not authorize
admission (C3). Regardless of the result, the shipped seat remains one-shot
(`roles.population_adversary.session.enabled: false`) until the Phase 4 flip
and a fresh-live re-earn under `HARNESS_VERSION` "6".

The population adversary is the only council seat whose findings can cause an
executable change. A finding supplies a `proposed_case` in the promoter's
closed grammar. `promote_proposed_cases` measures it on all five populations
and promotes it only when the predicted and measured matrices match exactly.
The current one-shot seat receives no reason for rejection after review,
including compilation failures and claim text that omits identifiers targeted
by its own parameters. This pilot tests whether one in-session, gold-free
compile verdict raises the promotion rate and whether one measured bit provides
additional value relative to its oracle cost.

## 1. Hypothesis

H4a (B0 over A): The gold-free `compile_proposal` verdict, run by the
harness on every submitted payload with one compile correction, raises the
novel-mutant promotion rate by at least 0.20. H4b (B1 over B0): The 1-bit
`measured_match_bit` adds at least 0.10 over B0. Primary contrast: B0 minus
A; secondary: B1 minus B0. C minus B0 is tertiary, uses
Benjamini-Hochberg, and cannot drive adoption. Baseline
$$p_A \approx 0.3$$ (Output 11 §11.2):
demo 1/1 promoted, synsql 0/3, dlt 0/3 rejected at the wire, fivetran
unmeasurable.

## 2. Arms

Every arm's final payload uses the unchanged post-session path:
`_parse_findings`, `screen_findings` and the `FATAL` leak short-circuit are
byte-identical, and the payload's proposals are measured by the unchanged
`promote_proposed_cases` against frozen gold in a fresh D3 process after the
session has closed. The wire is the same in every arm: the forced
`report_findings` tool (`tool_choice` unchanged), `mode:
harness_validated`, no model-initiated tool, no `abort` (abstention is an
empty `findings` list), `max_model_calls: 3` (1 + `SCHEMA_RETRIES`, with the
schema and compile corrections sharing the two correction calls),
`max_wall_s: 300`,
`max_usd: 1.00`. The model is not told its arm; a validator's verdict is
distinguishable from a schema correction only by its content.

| Arm | Wiring | Caps and bits |
|---|---|---|
| A current one-shot, byte-identical | `run_council` narrowed to the seat: one `complete()` per trial with the `SCHEMA_RETRIES` protocol corrections; `roles.population_adversary.session.enabled: false` | none new; 0 validator runs, 0 oracle bits |
| B0 the production harness-validated block | `enabled: true` in a pilot-only agents document: the harness runs `compile_proposal` on every submitted payload (`review/tools/critic_validators.py`); a failed validator result receives one compile correction whose `tool_result` is the rendered `Diagnostic` (`{compiles, grammar_code, missing_identifiers, duplicate_of}` as a code, one grammar flag, public names and the in-payload same-mutant bit); a second failed result is accepted as submitted and screened through the current post-session path | `harness_validators: [compile_proposal]`, `max_compile_corrections: 1`, `max_tool_calls: 0`, `max_oracle_bits: 6` (at most 2 validator runs x 3 bits); every value is the production default and hard cap (SoT R0.2), with no override |
| B1 = B0 plus the measured bit | B0's block with `measured_match_bit: true`: the harness also runs `measured_match_bit` once per session, on the first submitted payload, in the gold-bearing D3 worker. It returns only one aggregate boolean (`exact_match`: every proposal in the payload predicted its five-population matrix exactly); the matrices never leave the worker (constraint addendum A24). | B0 plus `max_oracle_bits: 7` (the one extra bit; `per_session: 1`); every value is the SoT T1.1 flagged value, with no override |
| C bounded loop, $$T_{\max} = 3$$ | B0's block with `max_compile_corrections: 2`: three compiled submissions, each answered by the validator, with the third accepted as submitted | F2 (SoT T1.2), the R0.1 override: `max_compile_corrections: 2`, `max_oracle_bits: 9` (3 runs x 3 bits), `max_model_calls` unchanged at 3 |

### F2 interpretation and override

For Output 11's P4 row and SoT T1.2, $$T_{\max} = 3$$ means three compiled
submissions with `max_compile_corrections: 2`, not three model calls. Under
the canonical model-call accounting, including corrections,
`max_model_calls: 3` would make arm C identical to B0. The second compile
correction is the only difference between C and B0.

The production default is also this seat's hard cap (SoT T1.1 "default = cap",
R0.2), so the higher value is legal only under R0.1. It is recorded here and
in arm C's routing fingerprint (§9). Arm C runs directly through the pilot
`RoutedProvider`, never through `cmd_review` or `_admission_gate`, writes no
ledger or admission record, and cannot support a production-cap claim. If C is
adopted, `max_compile_corrections: 2` becomes a configuration change that
re-derives the cap and requires a Phase 4 re-earn under the changed fingerprint.

The optional Output 11 arm B2, which exposes a 10-boolean in-session matrix, is
not run. The shipped code has no in-session matrix: `project_proposal_matrix`
is a post-session record, never a tool (SoT T3; A24). B1's single bit is the
flagged form of that arm.

### Pilot wiring

`council.run_council` is unchanged, so the pilot harness drives the seat
itself: a `RoutedProvider` loaded from
the pilot-only agents document (`--agents-config`; the repository default
remains `enabled: false`), `critic_validators.CriticSession(task=task,
role="population_adversary", gold=<frozen gold>, workspace=<trial clone>)`,
`critic_policy("population_adversary", agents_config=<pilot document>)`,
`critic_limits(...)`, `critic_validator_worker()` and
`RoutedProvider.run_session(role, view, policy, ctx, worker=...)` with the
declared validators dispatched through `critic_registry(role, block)` (the
ungated view; `ToolRegistry.for_role` reads the repository document). The
`gold` and `workspace` handles are handed to the session only in arm B1 and
only for the match bit; in A, B0 and C no gold is in the session's process.

### Bit budget

Arm A: 0 bits. B0: at most 2 validator runs, each 3 oracle bits — the compile
verdict (compiles / uncompilable), one grammar flag among `param_unknown`,
`param_conflict`, `variant_invalid`, `kind_operation_mismatch`, `uncompilable`,
`severity_incompatible`, `claim_missing_identifier`, the public identifiers
the claim failed to name,
`proposal_missing` when an actionable finding omitted its executable
`proposed_case`,
the attack kind, and the in-payload `same_mutant_as_earlier_proposal` bit
(never a duplicate-of-catalogue bit) — 6 bits, all gold-free. B1: B0 plus
exactly 1 measured bit, 7. C: 9 gold-free bits. Every validator run is a
recorded validator turn (`validator_run_count`, `oracle_bits_used`,
`correction_kinds: {schema, compile}` on the evidence row, SoT T8); the
per-trial bits log is those rows. No arm feeds back the mutant key
(it embeds `case.mutation`), inert or inapplicable sentences (inertness
needs gold; `materialize_mutation` is never called inside a session), the
post-session screen's VOID / NOTED / DUPLICATE lexicon, realized row counts,
rewards, the predicted or measured matrix. The full matrix is written
post-session to `attacks/<case>/rejected_proposal.json` (`projection_matrix`,
booleans only) for human audit and `elt-taskgen audit list`.

## 3. Cohort digest

`TBD` — the sha256 of `task_links.json` plus each task's `content_hash`,
recomputed on the cohort (the 15 tasks of the 2026-09-01 reference build
with frozen gold; 20 with the five drives' parent tasks if they are
disjoint) before any paid call. Disjointness: `TBD` (unverified as of this
skeleton; $$n = 15$$ is the primary design). A unit joins the cohort only
with a `reference-run` PASS at the cohort hash (the promoter measures
against frozen gold) and a `review` PASS. That review's adversary findings
provide the baseline catalogue for §5's "novel" test.

## 4. Replication

$$K = 8$$ fresh live trials per (unit, arm) in the recommended tier C
($$K = 4$$ in the screen tier S). Seed: `TBD` (recorded here at the start).
One seeded permutation over (unit, replicate, arm) triples interleaves the
arms so each unit's arms run within the same hour. Arms are not batched. Every
replicate is a fresh live call with the transcript memo refreshed
(`refresh=True`); routes are pinned per arm and checked at serve time. A
rerouted seat is a different experiment. Isolation: one `tasks/<id>` clone
and one `TranscriptStore` record directory per trial. Workspace roots must not be
under `runs/` or a release root; the promoter runs in D3 in a fresh process
after the session is closed, never sharing a filesystem with the session's
scratch state; the B1 match bit runs in its own gold-bearing worker on a
scratch copy of the population rows under the tool-context root, writing
nothing into the trial clone.

## 5. Primary metric and test

Per-trial score: 1 if and only if at least one proposal in the final payload is
promoted as a novel mutant by the unchanged `promote_proposed_cases` (exact
matrix and claim fidelity passed) and is absent
from the unit's baseline `task.attack_cases` catalogue (the promoter is
idempotent, so a re-promotion of a standing case scores 0). Unit = task.
Estimand $$\bar d$$ with the paired SE (Miller Eq. 7); the clustered SE
(Eq. 8, source pools as clusters) reported beside it. Test: paired t on
$$d_i$$ with $$n - 1$$ df plus a 1,000-resample unit-level percentile
bootstrap; $$\widehat{\text{pass}^k}$$ at $$k = 2, 4$$. Multiplicity: one
primary hypothesis per pilot (H4a), Holm step-down across the six pilots at
$$\alpha = 0.05$$; H4b and the C contrast by Benjamini-Hochberg at
$$q = 0.10$$; these contrasts cannot drive adoption alone. There are no
interim analyses. The analyst sees arm labels scrambled until the analysis code (the `pilot_stats`
module under `tools/`, with its `test_p4_echo_rate_veto`) and this file are
frozen. The free offline validation run on the doubles (`OracleProvider`,
`StochasticSeatProvider`, `FakeTransport` and the scripted critic session of
tests/test_review_tools_critic_validators.py) must recover a planted effect
before any live spend.

Secondary metrics, per arm: the uncompilable-rejection rate and proposal
executability (roadmap Phase 3 definition of done (3): executability at
least 0.95, uncompilable rejections at most 3 % over at least 40 tasks once
the base rate is measured), the mutation kill rate of promoted mutants, the
compile-correction rate, `validator_run_count` and `oracle_bits_used` per
trial, USD per promoted mutant and wall per trial.

## 6. Adopt thresholds, hard vetoes and fallback

Adopt B0 for the Phase 4 flip when $$\bar d_{B0-A} \ge 0.15$$ with Holm
$$p < 0.05$$. Build B1 (turn `measured_match_bit` on in the flipped block)
only if $$\bar d_{B1-B0} \ge 0.10$$, the echo rate is at most
0.05 and zero tripwire hits were recorded; otherwise the flag is removed
from the block (roadmap Phase 3 definition of done (2)). C is adopted over
B0 only by its own additional $$\bar d \ge 0.10$$ and then only as the cap
re-derivation plus re-earn of §2.

The following are hard vetoes regardless of the primary:

1. Echo rate above 0.05. Echo rate is the number of promoted proposals whose
   mutant key is unchanged from an earlier rejected submission of the same
   unit and whose `expected_pass` moved toward the measured matrix, divided by
   all promoted proposals in the arm (Output 11 §11.3 "new exploit rate"). It
   is computed after the session from `rejected_proposal.json` records
   (`projection_matrix`, `predicted`, and `measured_pass` per population)
   against the promoted case's `expected_pass`, not from session-visible data.
   B0 and C receive no measured feedback, so an echo in either arm is a channel
   defect. In B1, the one bit is the only measured signal, and this veto bounds
   it (A24).
2. Any `LEAK_TRIPWIRE` (`DiagnosticTripwire`) in any arm.
3. Any private material in any transcript: a mutant key; an inert,
   inapplicable, `VOID`, `NOTED`, or `DUPLICATE` word from the validator
   channel; a realized count; a reward; or a matrix cell. The D1 gatekeeper
   `assert_value_free` checks every correction, and an offline scan checks all
   recorded turns again.
4. USD per trial above $$2.5\times$$ arm A.

Common conditions: zero out-of-surface
writes, $$\widehat{\text{pass}^2}$$ not lower than A by more than 0.05,
infra-fault rate not higher than A by more than 0.05, confirmed-live-exploit
rate within 5 percentage points of A.

Fallback: B0 only, or one-shot; `measured_match_bit` remains disabled by default
regardless of the result. Adoption does not constitute admission. A converted
seat is admitted only by a fresh live `elt-taskgen metrology` run under
`HARNESS_VERSION` "6" whose fingerprint includes `harness_validators`,
`max_compile_corrections`, the loop limits and `validators.binaries` (SoT
T7). Until then the shipped block remains `enabled: false`: no validator is
on the wire, in the manifest's `harness_validators`, in `validator_digests`
or in the tool surface, and PROOF 5 holds
(`test_disabled_critic_tools_do_not_enter_the_wire_manifest_or_fingerprint`).
The declared block itself is hashed (R0.2), so arm A's digests in §9 are the
Phase 3 values. They changed once from the Phase 0 end state when the block was
declared (fingerprint `5a1989472c3137b4…` to `5f817b1faac1c214…`;
`docs/plans/bounded_agents_phase3.md` §2) and pinned as literals by
`test_phase3_critic_digests_and_fingerprint_are_pinned`.

## 7. Exclusion rules

These rules are fixed before data collection. Trials ending `LIMIT_WALL` are
not scored and are reported in the limit-stop rate because the clock includes
latency the model did not author. Trials ending `PROTOCOL_EXHAUSTED` or `OUTPUT_TRUNCATED` are
harness faults: retried once with a fresh seed, otherwise "could not
measure", never a zero. Trials ending `HARNESS_FAULT`, `PROVIDER_FAULT` or
`LEAK_TRIPWIRE` are reward `None`, reported in the infra-fault rate, never
in the estimand (a validator that raises, a match-bit worker that crashes, a
correction the gatekeeper refuses are all harness faults, C7). `SUBMITTED`
enters the estimand, including a payload with a failed validator result accepted after the correction
budget as specified by the design; its promotion result is its score.
`ABSTAINED` (an empty findings list: score 0), `LIMIT_ORACLE`, `LIMIT_USD`, and
`POLICY_VIOLATION` also enter the estimand. A rerouted seat is excluded. A unit
whose gold does not
re-freeze at `reference-run`, or whose baseline review is not a PASS at the
cohort hash, is excluded before the schedule is drawn.

## 8. Budget cap

The budget is about $51 at tier C, $$n = 15$$ (three arms), plus 25 to 40 s of
promoter CPU per proposal; $68 at $$n = 20$$; $25 at tier S (Output 11 §11.7; cost
addendum T5). Per-session `max_usd: 1.00` in every arm (two maxed-out
16,384-token Opus turns); per-trial `--budget-per-task 5.00`; a breach halts
as infrastructure (exit 2) and is reported. Expected wall time is about 2 h at
8-way parallelism, limited by promoter execution.

## 9. Routing fingerprint per arm

All four arms share the route `anthropic` / `claude-opus-5` / 16384 / high,
the system prompt sha256 `9f2d177ecb462f07…` and the wire tool manifest
sha256 `d68bdf35a0762734…` (`{role, tools: [report_findings]}`: the forced
tool is each arm's complete wire surface; the validators are `harness_only`
and never enter it). What differs is the declared `session:` block and, with it, the
policy, the behaviour digest and the fingerprint:

| Arm | `session:` block (the keys that differ) | `harness_validators` in the manifest | `policy_sha256` | behaviour digest (`role_behavior_sha256`) | `tool_surface_sha256` | `council_routing_fingerprint` of the pilot document |
|---|---|---|---|---|---|---|
| A | repository default (`enabled: false`) | `[]` | `bb49097556cf9839…` | `25d6642e96088612…` | `a387488579fefb38…` | `5f817b1faac1c214…` |
| B0 | `enabled: true` | `[compile_proposal]` | `45067a34d9e7f2be…` | `913a2c0b4264dc14…` | `276a6ca1d3755a7e…` | `c4a82f69ebbe3361…` |
| B1 | `enabled: true`, `measured_match_bit: true`, `max_oracle_bits: 7` | `[compile_proposal, measured_match_bit]` | `ff55bc0d75023640…` | `88fd73645b058410…` | `39b7ca817c2c2aff…` | `ccd72f668a0c5b76…` |
| C | `enabled: true`, `max_compile_corrections: 2`, `max_oracle_bits: 9` (the F2 override) | `[compile_proposal]` | `7c00edb428d19d10…` | `d8ee189043293271…` | `276a6ca1d3755a7e…` | `68c3c1f515454527…` |

The digests were read from the code on 2026-09-03 (arm A from the
repository document; B0, B1 and C from a pilot document equal to the
repository default with exactly the listed keys changed, through
`providers.role_behavior_manifest(role, agents_config=...)`,
`metrology.tool_surface_sha256(agents_config=...)` and
`metrology.council_routing_fingerprint(providers.load_role_routing(...))`).
If any value differs at the pilot's start, at least one of the following has
changed: the prompt; tool schema; validator code, which is hashed into the
surface once a seat is enabled (`metrology.validator_digests`, binaries
`duckdb` 1.5.5 and `sqlglot` 30.16.0); or declared block. This file must then be re-signed
before the first call. Arm A's values are the ones the shipped tree
carries. Enabling either critic seat changes the surface and fingerprint
(the Phase 4 re-earn), and nothing in this pilot may write them into an
admission record.

# Historical offline curation-to-release record: one task per pool

> **Historical snapshot (August 2026):** this file preserves the commands and
> observations from the original per-pool drives. Some statements below (most
> notably structural calibration as the default) are intentionally historical
> and are not current operating policy. Use `elt-taskgen pipeline --help`, the
> repository README, and `docs/SOURCES.md` for the current five-source flow.

> **Scope:** “end to end” in this August 2026 run record means ingest through
> offline release. It does not mean Airbyte -> cloud warehouse -> dbt. The
> sequence establishes oracle validation and semantic acceptance; it does not
> certify Snowflake, Databricks, or Redshift. Current policy and next steps are
> in [`../EXECUTION_MODEL.md`](../EXECUTION_MODEL.md) and
> [`duckdb_rlvr_cloud_runtime_migration.md`](duckdb_rlvr_cloud_runtime_migration.md).

Measured by execution (probe workflow wf_e8e7fd18-9a4, zero live calls, snapshot vintage
Aug 12). Ladders verified for synsql, dlt (personio) and wikidbs; ELT-Bench pinned
checkout lives at `../ELT-Bench`.

## The corrected wall map

The old belief "no pool task has ever passed `reference`" is FALSE. All three probed
pools pass intake → contamination_pre → generate → reference offline ("gold frozen for
5 population(s); determinism x3 identical"). The prior failures were task-content
defects: `adapters/synsql.py:599` rejects FK-less schemas ("no join surface"), and most
small SynSQL picks have zero FKs.

The one hard wall is **author** (and review, identically):
`cli._admission_failure_detail` checks the `council.live_admitted` admission record
BEFORE any transcript lookup. `--replay-only` is exempt from admission (same
function: a provider is exempt only when it declares `replay_only is True` or carries
no `routing` at all) but dead for pool tasks — no transcripts exist and
`record-transcripts` is gated by the SAME admission record (only `--loader-only` is
exempt) and needs an API key besides — recorded transcripts are replay evidence, so
they may not be bought under an unproven council.
*(Named by function, not by line: the Aug-13 integrity lane moved these lines, and a
line number in a runbook is a fact with a half-life.)*

Downstream, verified by code inspection:
- **attack**: deterministic, $0.
- **gates / validate-el / validate-t**: need the LIVE cross-family OSS witnesses
  (`independent.py:633-660` refuses anthropic for implementer/loader roles;
  `agents.yaml` routes them to openai_compat). ≤2 samples per witness, ~$0.08/task at
  kimi rates.
- **calibrate**: structural-only is the DEFAULT and passes; `--empirical` is opt-in
  (`cli.py:4193`) and satisfies nothing `select` needs
  (`selection.py:228-252` — structural difficulty is eligible).
- **select**: pool-of-one, `Quotas(size=1, val_fraction=0.0)` (`cli.py:1388`).
  NO minimum corpus, NO bypass needed.
- **audit**: auto-passes when licenses resolved + gates green + zero borderline
  collisions (probed pools: Apache-2.0 / CC-BY-4.0, already resolved).
- **release**: deterministic; `release/` is IMMUTABLE per workspace → one fresh
  workspace per pool.

## Gotchas that will burn a run

1. **Sticky rejection**: an author failure that is a TASK DEFECT burns 3
   SPECIFICATION repair rounds then rejects permanently — the ledger rejection
   SURVIVES re-ingest. On any doubt, use a fresh workspace. A *transport* fault is
   no longer one of these: a missing transcript, a dead provider, an unadmitted
   council or a breached budget now raises `engine.InfrastructureFailure`, records a
   FAIL row (never FATAL), spends no repair round, rejects nothing, and exits 2 — fix
   the infrastructure and re-run in the SAME workspace, which resumes at the failed
   stage instead of re-buying every transcript in a new one.
2. **Arm the firewall before generate**: `elt-taskgen measure-target --workspace <ws>
   --bench-root ../ELT-Bench` (100 anchors,
   1252 fingerprints). Unarmed, contamination_pre still passes but name-only.
3. **wikidbs** needs `tools/wikidbs_family_map.py` importable at package root
   (`adapters/wikidbs.py:444-456`).
4. **NEVER copy or replay an admission record.** *(Changed Aug-13 — the old
   instruction here said to copy it, and that instruction was the defect.)* At
   the start of each production campaign, run metrology live once, then point
   every workspace in that campaign at the resulting record:

   ```
   export ELT_TASKGEN_ADMISSION=$METROLOGY_WS/state/council.live_admitted
   ```

   Live metrology always bypasses stored transcripts. `--replay-only` remains a
   deterministic diagnostic, but returns could-not-measure and cannot write or
   revoke admission. A copy placed in another workspace is REFUSED on sight
   ("… is a COPY: it was
   earned at X and is being read at Y"), because a copy is precisely the thing that
   cannot be revoked: `revoke_admission` writes one tombstone and a photograph of the
   record never sees it. On 2026-08-13 five copies kept admitting for minutes after a
   blocked run withdrew the evidence behind them. The record is also stale-checked
   against the routing fingerprint (config/agents.yaml routing, every critic's system
   prompt, the specimen pool, the harness version and — new — the rendered critic
   VIEW), re-derived against the current `metrology:` bar, and digest-bound against
   hand edits. Every refusal names its own cause and its own remedy; if a stage says
   "council not admitted", read the rest of that line before doing anything.
5. Exit codes are uniform: `0`=stage passed, `1`=task REJECTED (a task defect; that
   workspace keeps the rejection), `2`=could not measure or could not decide. A `2`
   covers usage errors, an unknown `--task-id`, transport/credential/admission/budget
   failures (which now HALT without rejecting — re-run in the same workspace and the
   ladder resumes at the failed stage), and a run that stopped BLOCKED waiting on a
   human (`audit approve`) or on the environment (an immutable `release/` holding
   different content). `metrology` reads the same way: 0=admitted, 1=blocked,
   2=could-not-measure.
6. **Live credentials never go under `runs/`.** `runtime prepare` installs live
   Airbyte and warehouse values into the attempt copy, so it refuses a
   `--work-dir` under any `runs/` directory or under a release root (exit 2,
   before any credential is read). Put attempts under a scratch root outside
   the checkout. `review.tools.credential_sweep` reports credential-shaped files
   by name and by live secret-shaped value (key names only, never values), and
   `ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1 make test` asserts the drives are clean —
   gated until the owner has rotated and purged the canary attempt copies under
   `runs/runtime_canary_*/**/live/`.

### Why the copy workflow was replaced rather than patched

Three designs were on the table for the revocation-fails-open defect.

**(a) Self-describing marker + re-validate against the report it names.** The marker
carries report digest / seed / per-role metrics and `live_admission_ok` re-checks them
against the metrology report. Rejected as the *primary* mechanism, because it does not
close the hole: a copied marker beside a copied report is an internally *consistent*
pair, and the fact that invalidates it — a later run at the same fingerprint that
BLOCKED — is written in neither file. It only catches supersession if the copy has to
reach a report it does not carry, which breaks the copy workflow anyway. (a)'s
substance was kept regardless: the record now carries its evidence and the verdict is
re-derived from it, which is what catches a tightened bar and a hand-edited file.

**(c) Short-lived markers with explicit re-verification.** Rejected outright: it puts
a safety property on a clock. Inside the window it still fails open, the window length
is an arbitrary guess, and expiry is nondeterministic — three things the house rules
forbid in the same breath.

**(b) One record, consulted from a configured location — CHOSEN.** Revocation becomes
a single write at a single place, so "revoked" propagates by construction rather than
by anyone remembering to clean up five directories. There is no copy to go stale
because there is nothing to copy. The record additionally binds to its own absolute
path, which turns "don't copy this" from advice into an enforced precondition — the
one thing the previous runbook could not do, since it was the runbook itself that told
operators to copy.

Cost of (b), stated honestly: moving or renaming the metrology workspace invalidates
its record, and `$ELT_TASKGEN_ADMISSION` must be exported for a multi-workspace drive.
Both fail LOUD and closed (the refusal names the path it expected), which is the right
direction for a mechanism whose job is to withhold permission.

## Stages are not subcommands — read this before typing a command

The 15-stage ladder (`engine.STAGE_ORDER`) and the CLI's subcommand list are
DIFFERENT surfaces. A stage subcommand runs the ladder *up to and including* its
stage, re-attesting anything upstream whose content hash moved; several stages
have no subcommand of their own and are reached only by running past them.

The two names that do not exist as subcommands cost every pool agent in the
Aug-12 drive an `argparse` **exit 2** (verified by execution, both still exit 2):

| You might type | Result | What actually reaches that stage |
|---|---|---|
| `elt-taskgen author …` | **exit 2**, `invalid choice: 'author'` | `elt-taskgen review` — AUTHOR is ladder stage 5, run inside it (the subcommand's own help reads "semantic authoring + council review") |
| `elt-taskgen audit --workspace … --task-id …` | **exit 2**, `invalid choice: '/tmp/…'` | `elt-taskgen release` — AUDIT is ladder stage 14. The `audit` subcommand is a *human adjudication queue*, not a stage, and takes only `list` / `approve` / `reject` |

Complete subcommand list (`elt-taskgen --help` is the authority; there is no `demo`,
it was removed 2026-08-14):
`record-transcripts, metrology, ingest-{dbt,synsql,schemapile,anchor,wikidbs,dlt},
measure-target, generate, reference-run, review, attack, validate, validate-el,
validate-t, calibrate, select, release, export, verify, score,
semantic {score}, runtime, triage, audit {list,approve,reject}`.

`verify --release DIR` re-checks a frozen release. For historical schema-1/2
releases this includes a re-census of private `.duckdb` warehouses, which
`shasum -c` skips. `score --release DIR --unit ID --duckdb FILE` is likewise a
schema-1/2 semantic scorer. It intentionally does not accept schema-3 combined
tasks: use `semantic score --release DIR --task-id ID --submission FILE` for
the permanent local combined-task path. `runtime verify-stage1` and
`runtime verify-stage2`
collect real-warehouse parity for certification or explicit cloud-agent runs;
they are not the default RLVR replacement. None of these commands touches a
curation workspace.

Stage-subcommand → last stage run:
`generate`→generate, `reference-run`→reference, `review`→**review (runs author first)**,
`attack`→attack, `validate`→gates, `validate-el`→gates_extract_load,
`validate-t`→gates_transform, `calibrate`→calibrate, `select`→select,
`release`→**the whole ladder** (contamination_post, audit and release included).

## The sequence (per pool, fresh workspace)

This is the **known-good offline sequence**, recorded from the synsql run that became
the first pool task ever released (`release-8cfcd3924d36036f`, 47/47 checksums
verified, all three variants accepted with no gate waived). Its ledger shows
all 15 stages `pass` at revision 1 with **zero repair rounds**
(`SELECT COUNT(*) FROM repairs` = 0).

Completing this sequence does not assign a `runtime_*_certified` label. Cloud
certification is an optional, separately budgeted action described in the
[warehouse connector runbook](../WAREHOUSE_CONNECTORS.md).

```
ET="elt-taskgen"  # after: set -a; . ./.env; set +a  (both cred sets needed)
$ET measure-target --workspace $WS --bench-root .../kang-lab/ELT-Bench
$ET ingest-<pool> --workspace $WS ...      # synsql: FK>=1 schema
$ET metrology --workspace $METROLOGY_WS --budget-per-task 60  # fresh/live per campaign
export ELT_TASKGEN_ADMISSION=$METROLOGY_WS/state/council.live_admitted
                                           # CONSULT the one record; never copy it
$ET generate --workspace $WS --task-id $T
$ET reference-run --workspace $WS --task-id $T
$ET review --workspace $WS --task-id $T    # RUNS AUTHOR THEN COUNCIL, live
$ET attack --workspace $WS --task-id $T
$ET validate --workspace $WS --task-id $T && $ET validate-el ... && $ET validate-t ...
$ET calibrate --workspace $WS --task-id $T          # structural, $0
$ET select --workspace $WS --task-id $T
$ET release --workspace $WS --task-id $T   # RUNS contamination_post + AUDIT + release
```

Observed synsql ledger order — note that `author` moving the content hash makes
the ladder re-attest intake..reference before `review` proceeds, which is normal
and free:

```
intake reference generate reference author            <- `review` invocation, part 1
intake contamination_pre generate reference review    <- re-attestation, then REVIEW
attack gates gates_extract_load gates_transform
calibrate contamination_post select audit release     <- `release` invocation
```

## Spend reporting

Every subcommand that can spend prints its `CostMeter` total on exit — the
`Spend (live calls, metered):` block — on the success path and on the failure
path.

**CORRECTED Aug-13.** This section previously claimed "every stage subcommand"
and that was false of exactly one command — `metrology`, the most expensive one
in the CLI. It printed nothing, which is why the Aug-12 drive's metrology cost
was reconstructed by hand afterwards, and reconstructed **wrong by ~3x** (see
Cost, below). `cmd_metrology` now prints the meter on all three exit paths
(0 admitted / 1 blocked / 2 could-not-measure), the exit-2 budget breach
included — the path where the number matters most, since that is the run that
died *because* of spend. Verified offline:

```
$ elt-taskgen metrology --workspace /tmp/mws     # no key, no transcripts
ERROR: ANTHROPIC_API_KEY is not configured — … (fail closed)

Spend (live calls, metered per API attempt):
  TOTAL                                             $0.0000 of $5.00 budget
```

**A printed total is still not a ledger.** It dies with the terminal scrollback.
The better fix, not yet implemented, is a persisted per-invocation spend row in
`state/taskgen.sqlite` (stage, task_id, role, tokens, usd), which would make
cost auditable after the fact and let `--budget-total` survive across
invocations instead of resetting every command.

## Cost (Anthropic list rates)

**RE-CORRECTED Aug-13 (second correction — the first one was wrong).** The
token counts below are right: they are the 45 recorded transcripts of the
admitted metrology run (`reports/transcripts`, summing
each entry's `usage`). **The prices applied to them were not.** The Aug-13
table priced `claude-opus-5` at **$15/$75 per MTok**; the actual first-party
rate is **$5/$25** — the figure the code has had all along in
`providers.ANTHROPIC_PRICING_USD_PER_MTOK`, which is what `CostMeter` charges
against `--budget-per-task`. Every opus row was therefore **3x too high**, and
the doc and the enforcement mechanism disagreed by that factor.

Rates at the time: opus-5 $5/$25, sonnet-5 $3/$15, haiku-4-5 $1/$5 per MTok
(in/out). **Corrected again 2026-09 (roadmap 0.D):** Sonnet 5's introductory
$2/$10 was made permanent, so the meter now prices it at **$2/$10** and the
table is four rates per model — input, 5-minute cache write (1.25x), cache read
(0.1x), output — with `cache_read_input_tokens` / `cache_creation_input_tokens`
metered from every response. The Opus and Haiku rows below are unaffected (no
Sonnet seat ran in the admitted metrology run).

| Role | Model | Calls | Input tok | Output tok | Cost (was) | Cost |
|---|---|---|---|---|---|---|
| ambiguity_critic | claude-opus-5 | 11 | 71,292 | 3,512 | ~~$1.33~~ | **$0.44** |
| population_adversary | claude-opus-5 | 12 | 146,280 | 18,047 | ~~$3.55~~ | **$1.18** |
| shortcut_attacker | claude-opus-5 | 11 | 58,202 | 20,878 | ~~$2.44~~ | **$0.81** |
| feasibility_reviewer | claude-haiku-4-5 | 11 | 44,578 | 1,836 | $0.05 | **$0.05** |
| **TOTAL** | | **45** | **320,352** | **44,273** | ~~**$7.37**~~ | **$2.49** |

(The haiku row was already correct — only the three opus rows carried the bad
rate, which is why the error is 3x and not 3.5x.)

- Metrology, one-time: **~$2.50** to produce those 45 exchanges at list rates.
  (Caveat unchanged: the final admitted run may have replayed some exchanges
  from earlier attempts, so its *incremental* spend could be lower; $2.50 is
  the cost of the evidence, which is the figure that matters for a cold re-run.)

- **Budget guidance, corrected.** `CostMeter` enforces `budget_per_task_usd`
  against a *single* `task_id`, and `cmd_metrology` charges every critic call
  to the one id `"council-metrology"` (`cli.py`: `provider.task_id =
  provider.task_id or "council-metrology"`), so a cold run accumulates the
  whole run against one budget line. The harness-4 run above cost ~$2.50; a
  harness-6 run (110 scored trials = 140 trajectories, plus three canary
  trials per seat = 12 more, 152 fresh trajectories in all, each up to
  `max_model_calls` turns with at most one compile correction, prompt caching
  on the critic payloads) is **~$25** at list rates with caching — $15 to $25
  typical, about $39 at the limit with canaries and corrections (Phase 4 log
  §1 item 1; `metrology._COST_NOTE`) — so the `--budget-per-task` default
  cannot cover it and would raise `BudgetExceededError` → **exit 2, "could not
  measure"** partway through. Run metrology with **`--budget-per-task 60`**
  (~2.4x the typical run and ~1.5x the limit; the point of the cap is to
  bound a runaway, not to cover a worst case). The earlier harness-5 figure
  (~$10.7 for 152 one-shot exchanges, `--budget-per-task 15`) was never
  spent: no harness-5 run was made, and harness 6 superseded it. The run
  refuses to start (exit 2, nothing spent) when the installed duckdb or
  sqlglot differ from `metrology.toolchain` in `config/agents.yaml`; `uv sync`
  installs the pinned versions.
- **Metering since roadmap 0.D (2026-09).** The default per-task circuit
  breaker is **$5.00** for every profile (the former $2.00 already breached a
  one-shot task with `calibrate --empirical`). `CostMeter.reserve` runs
  before any transport call and refuses — with nothing spent — a call the
  remaining budget cannot absorb; `CostMeter.charge` then meters **per API
  attempt** after the transcript is recorded, so the attempts of a backend
  that raises (schema retries exhausted, a transport fault mid-exchange) are
  no longer invisible (they were 32% of the fivetran run's spend). Roles may
  declare `session.max_usd` in `config/agents.yaml`; the cap is enforced per
  trajectory beside the task budget only while that role's `session.enabled`
  is true (the four critic seats declare one and enable none, so no one-shot
  exchange — up to 1 + SCHEMA_RETRIES maxed attempts — can trip it; a
  top-level `max_usd` outside a session block is enforced as declared). The
  effective per-task ceiling is printed on every spend footer. Every transcript
  entry now records `elapsed_ms` (harness-measured transport wall), the
  provider-reported `served_model`, and the cache-token counters.
- Per task author+council: $0.31 clean, ~$1.25 with worst-case repairs (the
  $5.00/task default holds). *(These were computed from metered `usage.cost`,
  not from the bad rate table, and are unaffected by this correction.)*
- OSS gate witnesses: ~$0.08/task (kimi-k2.7-code @ $0.70/$3.50 per MTok, OpenRouter).
- **Total, one task from all five pools, structural calibration: ~$4.50-11**
  (excludes metrology; add the ~$2.50-3.25 above for a cold admission).
- Optional `--empirical` calibration: +$4.5-15/task (60-72 solver runs over 3
  variants; needs `--budget-per-task 20`). Not needed for `select`.

## The metrology bar was UNDER-POWERED, and is now an interval (harness 4)

**Measured across two live runs, not argued.** Of 31 (role, specimen) pairs
whose prompts were BYTE-IDENTICAL between two runs on the same enriched view,
**14 returned a different finding count**, even though critic calls are
tool-forced and unsampled. `ambiguity_critic` files 0 or 1 finding on a target
specimen, so "detected" carried ZERO margin — one noise event flipped it.
Harness 3 drew **two** tampered specimens per seat and demanded 2/2, so a seat
whose true per-trial hit rate is p passed with probability p²:

| true p | 0.95 | 0.90 | 0.85 | 0.80 | 0.75 | 0.60 | 0.50 |
|---|---|---|---|---|---|---|---|
| harness 3 P(pass) | 0.903 | 0.810 | 0.723 | 0.640 | 0.563 | **0.360** | **0.250** |
| harness 4 P(pass) | 0.993 | 0.902 | 0.682 | 0.421 | 0.214 | **0.0095** | **0.0005** |

The 2026-08-13 admission (all four seats 1.00 / 1.00 / 0.00) was the lucky
draw, not a stable property of the council. **The enriched view is not the
problem** — the A/B at the pinned seed exonerated it (`ambiguity-no-filter`
still detected; `population_adversary` precision improved 0.667 → 1.000). The
MEASUREMENT was the problem.

What changed (`review/metrology.py`, HARNESS_VERSION 3 → 4):

* **n**: 2 → **5** distinct tampered specimens per seat, drawn from pools grown
  from 4 to **7** per seat (2 always held out). The clean pool is 7, drawing 5.
* **k**: each drawn specimen is answered **5** times, at 5 different SURFACE
  WORDINGS of the same planted defect. It had to be done that way:
  `RoutedProvider` memoizes on sha256(prompt), so re-asking identical bytes
  replays one recorded answer — a replication that measures nothing and costs
  nothing. → **25 trials per seat**, up from 2.
* **margin**: the report records each specimen's hits/k, and the CLI prints it,
  so "landed 2 of 5" is visible instead of collapsing to "missed".
* **interval**: `recall >= 0.75` is now read as the LOWER bound of a one-sided
  Wilson interval at 0.85 confidence (25 trials ⇒ accept at 21+ hits), and
  `nitpick <= 0.75` as its UPPER bound. **No threshold was lowered**; each is
  read from the conservative end of what was measured, which is strictly
  harder. `confidence` has a hard floor of 0.80 in the model: at 0.75 the bound
  of a perfect 2-trial score is 0.81 and the old evidence would re-admit.
* **precision** is computed from the two rates at a FIXED reference mix (2
  clean per tampered — harness 3's mix, where it reduces exactly to the old
  `tp/(tp+fp)`). Without that, raising n while leaving the clean draw alone
  would have quietly made a false alarm cost a quarter of what it used to.

Two specimen defects were found and fixed while re-authoring the pool: the
critic view renders every mart COLUMN DESCRIPTION, and
`completed_order_count: Count of DISTINCT orders in scope; 0 if none`
restated, on screen, the very rules `ambiguity-no-dedupe` and
`ambiguity-no-null-rule` deleted from the prose. **Half the harness-3 ambiguity
pool planted nothing**, a critic that read the view and stayed silent scored a
MISS, and the Aug-13 blocked run drew one of them. An injector now strips a
rule from every public surface that restates it, checked by test.

**Cost at harness 4: ~$9.8 per run** (140 calls; the flag was 15 then).
Harness 3 cost ~$3.35 (48 calls) and bought a coin flip; this is 2.9x the
spend for 12.5x the trials per seat and ~42x the separating power. Most of
that ratio comes from metrology no longer consulting all four seats on a
tampered specimen scored against one of them: at harness-3 dispatch the same
design would cost ~$31. **Superseded by harness 6** (the next section): the
same 140 scored trajectories plus 12 canary trials, each now a bounded
trajectory in a fresh per-trial workspace, are ~$25 per run at list rates
with caching, and the flag is `--budget-per-task 60`.

## MANDATORY before the next drive: re-earn the council admission

**RESOLVED IN CODE, Aug-13 integrity lane — the marker can no longer lie about
this.** The situation was: `council_routing_fingerprint` hashed
`harness_version + pool_sha256 + per-role {provider, model, max_tokens,
effort, system-prompt digest}` and the rendered VIEW was in none of them. The
view lane enriched the critic view (it now reproduces the typed
`documentation.md` source-table block alongside `schemas/*.csv`), invalidating
384 of 415 recorded transcripts, while the fingerprint moved by zero bits and
`council.live_admitted` still read valid in all six workspaces
(`cb46a21eb4adb4c0`). All four seats had been admitted at recall 1.0 /
precision 1.0 on a **strictly poorer view** than is now in force.

`council_routing_fingerprint` now also hashes `view_digest()` — every critic
role's rendered view over every specimen in the pool, from the one renderer
production calls (`council.render_view`). The fingerprint consequently moved
on its own:

```
cb46a21eb4adb4c0…   (view NOT covered — the marker that could not see the change)
0098d63f9ea8c3cb…   (view covered; view_digest 3e595916bb2e91ca…)
```

so every pre-existing record is stale by fingerprint, and additionally refused
as schema-v1 (`SUPERSEDED`). The five copies quarantined by hand on 2026-08-13
(`*.revoked-20260813`) are now inert on their own bytes — un-quarantining one
does not re-admit anything. That is pinned by test, not by the rename.

**Still MANDATORY: re-earn the admission.** The mechanism can no longer carry
an admission across a view change; it cannot re-earn one for you.

```
$ET metrology --workspace $METROLOGY_WS --budget-per-task 60  # all four seats
export ELT_TASKGEN_ADMISSION=$METROLOGY_WS/state/council.live_admitted
$ET record-transcripts --workspace $WS --task-id $T  # re-seed at the new keys
```

Projected cost of the re-run: **~$25** at harness 6 (152 trajectories: 140
scored plus 12 canary trials, prompt caching on the critic payloads; $15 to
$25 typical, about $39 at the limit with canaries and corrections), so budget
**$60**; more if seats need a second attempt. Harness 5 (~$10.7 for 152
one-shot exchanges, budget $15) was never run: `HARNESS_VERSION` moved "5" to
"6" WITH the protocol change — per-trial isolated workspaces, one trajectory
per (trial, seat), the pool stratified over three fixture families, the
integrity bars — independently of any seat flip, so this FIRST fresh-live
re-earn is under "6": one paid run earns the record the Phase 0 to 4 trees
have owed since 0.E, instead of a harness-5 run followed by a harness-6 run
(Phase 4 log §0, §5). Both critic seats stay `session.enabled: false` for it;
flipping one is a further re-earn (re-earn #2), after pilots P4 and P5. *(The
~$3.20 figure below it in earlier revisions was harness 3's 48-call run —
right for that harness, and that harness is the one whose verdict was a coin
flip; ~$9.8 was harness 4's.)* Optional: `--workers 8` runs the trials
through eight providers over one store and meter (the evidence is sorted by
trial index before hashing, so the manifest digest is identical at any N);
`--diagnostic-order-check` re-runs the seed's schedule in canonical order and
always exits 2 (an order-dependence probe, never an admission).

**Expect the first harness-4 run to BLOCK.** Under the finding, at least the
ambiguity seat's per-specimen hit rate is near 0.5-0.67, and 25 trials will
say so out loud instead of rolling 2/2. That is the measurement working. Read
the per-specimen margins the CLI now prints before touching anything: a seat
that lands 5/5 on four specimens and 0/5 on the fifth has a specimen problem
or a prompt problem, not a noise problem, and the two are finally
distinguishable.

**Pin the confound before reading the result.** The Aug-13 re-run that blocked
used a FRESH seed (`15596299314441845844`) and therefore drew DIFFERENT
tampered ambiguity specimens (no-null-rule, no-tiebreak) than the admitted run
at seed `8944342589527049266` (no-filter, no-dedupe). "The enriched view broke
the ambiguity seat" and "those two specimens were always hard for it" are both
consistent with that evidence, and no amount of reasoning separates them. Run
`--seed 8944342589527049266` on the enriched view — same specimens, view the
only variable — before concluding anything about the seat.

One same-specimen datum already survives the confound and is worth carrying
in: clean specimen `clean-decoy-a` drew ZERO `population_adversary` findings on
the poorer view and ONE on the enriched view. Specimen held constant, view
changed. The enriched view does create new false-alarm surface; what is
unmeasured is how much.

What the run must re-establish, and why each is genuinely new evidence:
- **feasibility_reviewer** — recall should hold (the injectors cut the IR, so a
  dropped element vanishes from both blocks), but it now holds types and keys it
  never had: its CLEAN-specimen precision / nitpick rate is the unmeasured half.
- **ambiguity_critic** — declared types and enum domains now appear in a second
  public file. That can dissolve type-fork specimens (recall risk) and the
  deliberate two-phrasing redundancy is a brand-new false-alarm surface.
- **shortcut_attacker** — now sees primary/business keys, relationship
  optionality and (on wikidbs-shaped pools) source row counts: new
  true-positive AND new false-alarm surface.

**Do NOT re-key existing transcript files to retire the staleness.** That would
assert these critics answer the enriched view exactly as they answered the
impoverished one — an unmeasured claim, i.e. fabricated evidence.

## Sequencing against in-flight work

Run the five-pool drive only AFTER: (1) the EL-admission workflow lands (jitter + EL
mutants move content hashes — freeze each pool task once, at the final vintage);
(2) the schemapile gold-integrity fix (its pool ships wrong gold on 37.5% of tasks —
never release from a defective adapter); (3) metrology, which must postdate any
agents.yaml routing change. Probe workspaces left for inspection: /tmp/probe-synsql,
/tmp/probe-synsql-replay, /tmp/probe-dlt, /tmp/probe-wikidbs.

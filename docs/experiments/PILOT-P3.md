# PILOT-P3 — T implementer witness session pre-registration

Status: pre-registration SKELETON, committed on 2026-09-03 before any paid
call (roadmap Phase 2 entry criterion; Output 11 §11.1.5). Every `TBD` value
must be recorded in this file at the pilot's start and before the first live
trial. This file contains no measurements. The pilot evaluates the cross-family
dual-build witness, not the policy. It excludes the 2.b training signal
(`training/signal.py`) and does not authorize admission (C3).

OQ-23 option C is in force by the constraint owner's decision of 2026-09-03.
`DEVELOPMENT`-population row counts are solver-visible by
construction: the DEVELOPMENT warehouse is what the solver executes against,
its tables hold 2 to 8 rows, and a page of rows is the count. Therefore,
`dev_query` is included in the implementer manifest. The following remain
private on every route and seat: stage-1 counts of every HIDDEN population (primary,
counterfactual, resampled, stress and any other graded population), stage-2
gold (mart outputs), reference SQL, per-mart gold-match bits, rewards,
`dev_pass`, `expected_fingerprints` and the answer-key files. Every projection
omits `own_row_count`, the per-mart "matches dev gold" bit remains disabled,
and `run_mart_sql_dev` returns `{mart, code}` with rows discarded.

## 1. Hypothesis

H3: Compared with the current cold resample loop, development-only execution
feedback raises dual-build agreement by at least 0.20 without raising the
dev-pass/hidden-fail rate. The primary contrast is arm C over arm A. Secondary
contrasts are arm B over arm A and arm C over arm B. Baseline (Output 11
§11.2): agreed on demo and synsql with one sample; 0/3 on fivetran, where every
attempt exhausted 16,384 output tokens.

## 2. Arms

All three arms use the unchanged certifier: `parse_sql_by_mart` then
`evaluate_build` on all five populations in the spawned, killable build
worker (`DEFAULT_BUILD_LIMITS`, 300 s per population after the trusted load),
scored by `upstream_eval.evaluate` against frozen gold; the dev-pass or
hidden-fail adjudication is unchanged. Cross-family is asserted before every
session and on the served model of every turn (C6).

| Arm | Wiring | Caps |
|---|---|---|
| A current one-shot, byte-identical | `run_independent_build`, `MAX_SAMPLES = 2` cold resamples, `roles.independent_implementer.session.enabled: false` | none new; 1 call (up to 3 API attempts) per sample |
| B one revision | `enabled: true`, `max_turns: 2`, one `dry_run_sql` whose code-only `Diagnostic` returns in the correction channel, then `submit_sql_by_mart` or `abort` | production default block: `max_tool_calls: 6`, `max_usd: 0.50`, `wall_clock_s: 600` |
| C bounded loop | `enabled: true`, up to $$T_{\max} = 5$$ turns with `list_schemas`, `dev_query` (per-tool 8), `dry_run_sql` (4), `run_mart_sql_dev` (2), `submit_sql_by_mart`, `abort`; every tool runs in a gold-free D3 worker under `SemanticLimits` (60 s, 512 MB, threads 1), never through a plain in-memory connection | `max_turns: 5` (default 4, hard cap 8), tool-call cap $$2T_{\max} = 10$$ (hard cap 14), `max_usd: 0.25` (a pilot reduction from the $0.50 default), `wall_clock_s: 480` (default 600, cap 1,200); every value is at or below the production hard cap, so no R0.1 override is needed |

Production block (SoT T1 IMP, `config/agents.yaml`): `max_turns: 4`,
`max_tool_calls: 6`, `per_tool: {dev_query: 8, dry_run_sql: 4,
run_mart_sql_dev: 2}`, `max_usd: 0.50`, `wall_clock_s: 600`; `hard_caps:
{turns: 8, tool_calls: 14, usd: 0.50, wall_clock_s: 1200}`. The pilot sets
`enabled` in a pilot-only agents document passed with `--agents-config`. The
repository default remains `enabled: false`.

## 3. Cohort digest

`TBD` — the sha256 of `task_links.json` plus each task's `content_hash`,
recomputed on the cohort (15 T units of the 2026-09-01 reference build; 20
with the five drives' `__t` units if they are disjoint) before any paid call.
Disjointness of the drives from the 15 cohort tasks: `TBD` (unverified as of
this skeleton; every $$n = 20$$ figure assumes it, $$n = 15$$ is the primary
design). The bands are recomputed on the digest before the schedule is drawn.

## 4. Replication

$$K = 8$$ fresh live trials per (unit, arm) in the recommended tier C
($$K = 4$$ in the screen tier S). Seed: `TBD` (recorded here at the start).
One seeded permutation over (unit, replicate, arm) triples interleaves the
arms so each unit's arms run within the same hour. Arms are not batched. Every
replicate is a fresh live call with the transcript memo refreshed
(`refresh=True`); routes are pinned per arm and checked at serve time. A
rerouted seat is a different experiment. Isolation: one `tasks/<id>` clone
and one `TranscriptStore` record directory per trial, workspace roots never
under `runs/` or a release root.

## 5. Primary metric and test

Per-trial score: 1 if and only if the dual build is `agreed` (reward 1.0 on all
five populations for the final sample), else 0. Unit = task; marts are a
clustered secondary unit. Estimand $$\bar d$$ with the paired SE (Miller Eq.
7); the clustered SE (Eq. 8, source pools as clusters) reported beside it.
Test: paired t on $$d_i$$ with $$n - 1$$ df plus a 1,000-resample unit-level
percentile bootstrap; $$\widehat{\text{pass}^k}$$ reported at $$k = 2, 4$$.
Multiplicity: one primary hypothesis per pilot, Holm step-down across the
six pilots at $$\alpha = 0.05$$; secondaries by Benjamini-Hochberg at
$$q = 0.10$$; these contrasts cannot drive adoption alone. There are no
interim analyses. The analyst sees arm labels scrambled until the analysis code
and this file are frozen.

## 6. Adopt thresholds, hard vetoes and fallback

Adopt C (or B) over A when $$\bar d \ge 0.15$$ with Holm $$p < 0.05$$. If B
meets its rule and C does not beat B by an additional $$\bar d \ge 0.10$$,
adopt B. The following are hard vetoes regardless of the primary: the
dev-pass/hidden-fail rate (`rewards[development] == 1.0 and
any(rewards[p] < 1.0 for p in
GRADED_POPULATIONS)`) up by more than 0.05; any `dev_pass` bit, any hidden
count, any gold value in any transcript (zero); any `LEAK_TRIPWIRE`; any
cross-family assertion failure; any guard failure; p90 wall above 480 s.
Common conditions: zero out-of-surface writes,
$$\widehat{\text{pass}^2}$$ not lower than A by more than 0.05, infra-fault
rate not higher than A by more than 0.05. Fallback: keep the two cold
samples; raise the reasoning budget; `OUTPUT_TRUNCATED` halts on the first
truncation. Adoption does not constitute admission. The witness is not
admission-gated, so no re-earn follows, but the adopted block is a config
change that re-derives the caps.

## 7. Exclusion rules

These rules are fixed before data collection. Trials ending `LIMIT_WALL` are
not scored
(reported as a limit-stop rate: the clock includes latency the model did not
author). Trials ending `PROTOCOL_EXHAUSTED` or `OUTPUT_TRUNCATED` are harness
faults: retried once with a fresh seed, otherwise "could not measure", never
a zero. Trials ending `HARNESS_FAULT`, `PROVIDER_FAULT` or `LEAK_TRIPWIRE`
are reward `None`, reported in the infra-fault rate, never in the estimand.
`SUBMITTED`, `ABSTAINED`, the count-limit stops (`LIMIT_TURNS`,
`LIMIT_TOOL_CALLS`, `LIMIT_TOKENS`, `LIMIT_USD`, `LIMIT_ORACLE`, `STUCK`)
and `POLICY_VIOLATION` (a scored seat outcome and a veto counter) enter the
estimand. A rerouted seat is excluded. A unit whose gold does not re-freeze
at `reference-run` is excluded before the schedule is drawn.

## 8. Budget cap

The budget is about $11 at tier C, $$n = 15$$ (at most $77 all-tail at $0.072
per reasoning-exhausted attempt); $15 ($103) at $$n = 20$$; $6 ($39) at tier S,
at the pinned kimi-k2.7-code rates ($0.70 / $3.50 per million tokens). Whether
the gateway honours caching is unverified (OQ-11), so the uncached figures
are the planning numbers. Per-trial `--budget-per-task 5.00`; a breach halts
as infrastructure (exit 2) and is reported.
Certification time, rather than tokens, is the binding resource (3 to 8 h at
8-way parallelism).

## 9. Routing fingerprint per arm

| Arm | provider / model / max_tokens / effort | system sha256 | tool-manifest sha256 | policy_sha256 | behaviour digest |
|---|---|---|---|---|---|
| A | `openai_compat` / `${ELT_TASKGEN_OSS_MODEL}` (resolved id recorded at start: `TBD`) / 16384 / null | `e3b0c44298fc1c14…` (the role has no system prompt) | `4f53cda18c2baa0c…` (empty wire manifest) | `476595fbb649f269…` | `9a78a887e25d5a08…` (`loop_limits: {}`: the disabled witness block folds to `{}`) |
| B | same route | same | `TBD` from `ToolRegistry.declared_for_role("independent_implementer").manifest_sha256()` under the pilot document | `TBD` from `session_policy_for("independent_implementer", agents_config=<pilot document>).sha256()` | `TBD` |
| C | same route | same | `TBD` (same manifest as B) | `TBD` (the C limits) | `TBD` |

The arm A digests were read from the code on 2026-09-03. If any value differs
at the pilot's start, the prompt, tool schema, or declared block has changed,
and this file must be re-signed before the first call.

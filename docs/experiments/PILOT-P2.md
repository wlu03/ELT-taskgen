# PILOT-P2 — EL loader witness session pre-registration

Status: pre-registration SKELETON, committed on 2026-09-03 before any paid
call (roadmap Phase 2 entry criterion; Output 11 §11.1.5). Every `TBD` value
must be recorded in this file at the pilot's start and before the first live
trial. This file contains no measurements. The pilot evaluates the cross-family
independent-load witness, not the policy, and does not authorize admission
(C3).

An EL load plan has one artifact and one reader per table, and both sides use
the same trusted readers. The witness therefore validates bundle sufficiency
only. `el-artifact-census` validates EL gold independently. This pilot tests
whether one static `check_load_plan` revision improves the witness; it does not
certify gold.

## 1. Hypothesis

H2: Compared with the current cold resample loop, one `check_load_plan`
revision raises the strict EL verdict on all four graded populations by at
least 0.20 without raising the dev-pass/hidden-fail rate. The primary contrast
is arm B over arm A; the secondary contrast is arm C over arm B.

## 2. Arms

All three arms use the unchanged certifier: `parse_load_plan` (the one
parser, `calibration.parse_submission(EXTRACT_LOAD)`), `evaluate_load_build`
(trusted readers confined to the population's rendered root,
`upstream_eval.evaluate_variant(EXTRACT_LOAD)`) on all five populations.
Cross-family is asserted before every session and on the served model of
every turn (C6). `check_load_plan` performs only static checks of reader
validity, `_resolve_artifact` confinement, and table coverage. It can return
codes `unknown_reader`, `path_escape`, `table_uncovered`, and `s3_part_file`,
with the table in `subject`. It executes nothing and returns no count. The
loader `errors[pop]` record, which embeds `expected N rows, got M`, is never
fed back.

| Arm | Wiring | Caps |
|---|---|---|
| A current one-shot, byte-identical | `run_independent_load_build`, `MAX_SAMPLES = 2` cold resamples, `roles.independent_loader.session.enabled: false` | none new; 1 call (up to 3 API attempts) per sample |
| B one revision | `enabled: true`: `replace_load_plan` once with `check_load_plan` auto-run, then `submit_load_plan` or `abort` | the production default block = the hard cap: `max_turns: 2`, `max_tool_calls: 2`, `max_usd: 0.20`, `wall_clock_s: 600`; B wall ceiling for adoption 300 s |
| C bounded loop | up to $$T_{\max} = 4$$ turns of the same three tools | F1 (SoT T1.2): $$T_{\max} = 4$$ exceeds the 2-turn production hard cap. R0.1 permits a pilot-only `SessionLimits` override (`max_turns: 4`, `max_tool_calls: 4`, `hard_caps.turns: 4`, `hard_caps.tool_calls: 4`) only when it is recorded here and in arm C's routing fingerprint. Arm C runs directly through the pilot `RoutedProvider`, never through `cmd_review` or `_admission_gate`, writes no ledger or admission record, and cannot support a production-cap claim. If C is adopted, a configuration change re-derives the LDR cap; no re-earn is required because the loader is not a critic. |

Production block (SoT T1 LDR, `config/agents.yaml`): `max_turns: 2`,
`max_tool_calls: 2`, `max_usd: 0.20`, `wall_clock_s: 600`; `hard_caps: {turns:
2, tool_calls: 2, usd: 0.20, wall_clock_s: 600}`. The pilot sets `enabled`
(and, for arm C only, applies the override) in a pilot-only agents document
passed with `--agents-config`. The repository default remains `enabled: false`.

## 3. Cohort digest

`TBD` — the sha256 of `task_links.json` plus each task's `content_hash`,
recomputed on the cohort (15 EL units of the 2026-09-01 reference build; 20
with the five drives' `__el` units if they are disjoint) before any paid
call. Disjointness: `TBD` (unverified as of this skeleton; $$n = 15$$ is the
primary design). The decision on the legacy Snowflake password in the five
`__el` public releases (Phase 1 log §3 item 4) is made before any bundle of
theirs is shown to a model.

## 4. Replication

$$K = 8$$ fresh live trials per (unit, arm) in tier C ($$K = 4$$ in tier S).
Seed: `TBD` (recorded here at the start). One seeded permutation over (unit,
replicate, arm) triples interleaves the arms. Arms are not batched. Every
replicate is a fresh live call with the transcript memo refreshed
(`refresh=True`); routes are pinned per arm and checked at serve time.
Isolation: one `tasks/<id>` clone and one `TranscriptStore` record directory
per trial; the EL bundle is emitted per trial from the populations; workspace
roots must not be under `runs/` or a release root, and the bundle a model sees
carries placeholder credentials only.

## 5. Primary metric and test

Per-trial score: 1 if and only if the final sample's strict EL verdict holds on
all four graded populations (primary, resampled, counterfactual, stress), else
0. `DEVELOPMENT` is excluded from the score. Unit = task. Estimand $$\bar d$$ with
the paired SE (Miller Eq. 7); the clustered SE (Eq. 8, source pools as
clusters) reported beside it. Test: paired t on $$d_i$$ with $$n - 1$$ df
plus a 1,000-resample unit-level percentile bootstrap;
$$\widehat{\text{pass}^k}$$ at $$k = 2, 4$$. Holm across the six pilots at
$$\alpha = 0.05$$; secondaries by Benjamini-Hochberg at $$q = 0.10$$. No
interim analyses. Arm labels are scrambled for the analyst until the analysis
code and this file are frozen.

## 6. Adopt thresholds, hard vetoes and fallback

Adopt B over A when $$\bar d \ge 0.15$$ with Holm $$p < 0.05$$. Adopt C only
if it also beats B by $$\bar d \ge 0.10$$. The following are hard vetoes: the
dev-pass/hidden-fail rate up by more than 0.05; the exploit counter not
non-inferior (margin 0.05); any expected count, per-table `strict_raw_tables`
verdict, fingerprint or database path in any transcript (zero); any
`LEAK_TRIPWIRE`; any cross-family assertion failure. Cost and wall ceilings:
USD per strict pass at most 2.5 times arm A; arm B wall at most 300 s.
Common conditions: zero out-of-surface writes, $$\widehat{\text{pass}^2}$$
not lower than A by more than 0.05, infra-fault rate not higher than A by
more than 0.05. Fallback: keep the two cold samples.

## 7. Exclusion rules

These rules are fixed before data collection. `LIMIT_WALL` trials are not
scored and appear only in the limit-stop rate. `PROTOCOL_EXHAUSTED` and
`OUTPUT_TRUNCATED` are harness faults:
retried once with a fresh seed, otherwise "could not measure", never a zero.
`HARNESS_FAULT`, `PROVIDER_FAULT` and `LEAK_TRIPWIRE` are reward `None`,
reported in the infra-fault rate. `SUBMITTED`, `ABSTAINED`, the count-limit
stops and `POLICY_VIOLATION` enter the estimand. A rerouted seat is
excluded. A unit whose EL bundle cannot be emitted from its populations, or
whose gold does not re-freeze at `reference-run`, is excluded before the
schedule is drawn.

## 8. Budget cap

The budget is about $5 at tier C with $$n = 15$$, $6 with $$n = 20$$, and $2
at tier S, at the pinned kimi-k2.7-code rates (gateway caching unverified,
OQ-11; uncached figures are the planning numbers). Per-trial
`--budget-per-task 5.00`; a breach halts as infrastructure (exit 2) and is
reported. Certification time is the binding resource at 3 to 8 hours with
8-way parallelism.

## 9. Routing fingerprint per arm

| Arm | provider / model / max_tokens / effort | system sha256 | tool-manifest sha256 | policy_sha256 | behaviour digest |
|---|---|---|---|---|---|
| A | `openai_compat` / `${ELT_TASKGEN_OSS_MODEL}` (resolved id recorded at start: `TBD`) / 4096 / null | `e3b0c44298fc1c14…` (the role has no system prompt) | `4f53cda18c2baa0c…` (empty wire manifest) | `aa783518109b0dca…` | `8c11ee4cc9e98866…` (`loop_limits: {}`: the disabled witness block folds to `{}`) |
| B | same route | same | `TBD` from `ToolRegistry.declared_for_role("independent_loader").manifest_sha256()` under the pilot document | `TBD` from `session_policy_for("independent_loader", agents_config=<pilot document>).sha256()` | `TBD` |
| C | same route | same | `TBD` (same manifest as B) | `TBD` (the F1 override limits) | `TBD` |

The arm A digests were read from the code on 2026-09-03. If any value differs
at the pilot's start, the prompt, tool schema, or declared block has changed,
and this file must be re-signed before the first call.

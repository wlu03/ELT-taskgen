# Generated-task difficulty compared with ELT-Bench

Publication note: this README and `scripts/` are published. The other files
it cites (the two analysis documents, the five per-pool reports and the JSON
records in this directory) are maintainer-local and not published in this repository.

The provenance-anchored analysis of the original 100-task benchmark is in
`ELT_BENCH_ORIGINAL_ANALYSIS.md` (not published). It
separates paper-era corpus facts, implementation defects, and later Verified
corrections from the generated-task measurements below.

The current-code and current-artifact audit is in
`TASKGEN_DIFFICULTY_ANALYSIS_2026-08-31.md` (not published).
It traces the full generator, measures the fixed 15-task cohort, separates real
from synthetic DuckDB evidence, and explains why structural bands are not
empirical difficulty. This file remains the historical before/after record.

> Measurement scope: all rewards and difficulty values in this directory
> are private DuckDB/structural semantic measurements. “End to end” in this
> historical report means the offline taskgen path from ingest through
> reference generation; no Airbyte, Snowflake, Databricks, Redshift, or dbt
> cloud run occurred. These results measure semantic difficulty, not runtime
> portability or certification. See
> [`../EXECUTION_MODEL.md`](../EXECUTION_MODEL.md).

> Re-measured 2026-08-11 after the transform-depth build. Everything from §1
> onward reports the BEFORE baseline. [§0](#0-after-the-transform-depth-build-offline-semantic-re-measurement)
> reports the AFTER results from re-running ingest → generate → reference on
> a new per-pool sample and measuring both states with the implementation
> described in §9. Read §0 before the baseline in §1.

> Reproducibility note, added later: The script that produced
> `verify_before.json` / `verify_after.json` — a `verify_difficulty_measure`
> script that used to sit under `tools/` — was **not kept**. The two JSONs in
> this directory are the retained record of that measurement and remain the
> evidence; they can no longer be regenerated from this repo. The remaining
> implementation follows a different path:
> [`scripts/measure_all.py`](scripts/measure_all.py) (with
> [`scripts/measure_discriminating_power.py`](scripts/measure_discriminating_power.py));
> it is not a drop-in replacement and its output is not byte-comparable with the
> retained JSONs. Both are kept as the method record rather than as runnable
> commands: each hard-imports `ELT-Bench/analysis/analyze.py` — absent from the
> pinned checkout at `../ELT-Bench` today — and each points at `/tmp` workspaces
> that no longer exist. Re-measuring requires new workspace paths and the
> upstream tagger; running the scripts alone is insufficient.

**Measured 2026-08-11 against the pinned benchmark at
`../ELT-Bench`, the checkout beside this repository (100 tasks).**
Sample: **46 reference-stage tasks** (reference results retained, determinism ×3)
across the five pools plus the hand-written demo, against **100 anchor TaskIRs** imported by
`elt-taskgen measure-target`.

Every number in this document was produced by code run for this document, over the
five per-pool workspaces listed in §7. Where a feature cannot be measured on one
side, it says **UNMEASURABLE** rather than reporting a zero. The five per-pool
reports — `anchor_baseline.md`, `synsql.md`, `fivetran.md`, `dlt_wikidbs.md`,
`schemapile_demo.md` (not published) — carry the per-task detail; this
document reconciles them and answers the maintainer's question.

---

## 0. After the transform-depth build: offline semantic re-measurement

Re-measured 2026-08-11 after the op-vocabulary / plan-library / fivetran-recovery
build. Method: a fresh per-pool sample driven through **ingest → generate →
reference** (`tools/verify_difficulty_rebuild.py`), then BEFORE and AFTER measured by
**one** implementation (the since-lost `verify_difficulty_measure` script; see the
reproducibility note at the top) pointed at the old workspaces and the new ones in
turn. A task counts only when its reference result is retained —
a plan that validates but fails in DuckDB still leaves `sql_by_mart` on the IR
while producing no answer key, and measuring it would count a task no solver can be
graded on.

`computed` is measured **off the compiled reference SQL**, not off prose and not off
op labels: a mart column is computed when the expression producing it, resolved
transitively through the CTE chain, is anything other than a bare column reference.
That is the question ELT-Bench answers with a regex over its own prose, so changes to
description quality do not affect this measurement.

### 0.1 Reference yield

| pool | records attempted | registered | **reached reference (reference retained)** |
| --- | ---: | ---: | ---: |
| `synsql` | 12 | 11 | **10** |
| `fivetran` | 6 packages | 6 | **6** |
| `schemapile` | 8 | 8 | **7** |
| `wikidbs` | 12 | 12 | **11** |
| `dlt` | 12 connectors | 10 | **8** |
| **total** | **50** | **47** | **42** |

8 tasks did not reach a retained reference result:

* **3 refused at ingest.** `synsql/autonomous_vehicle_radar…` — *"no
  declared chain funds any library shape"*, the plan library declining rather than
  inventing evidence. `dlt/airtable`, `dlt/strapi` — `NoStaticResources`, resources
  defined at runtime; pre-existing and unchanged.
* **1 refused by the intake-format filter.** `dlt__notion`:
  *"single-table-no-join; too-few-source-tables."*
* **2 lost to one new defect** (§0.5, problem 1): `synsql__3d_coordinate…rollup` and
  `schemapile__…openbis…schema_186`, both `BinderException: Cannot mix VARCHAR and
  INTEGER_LITERAL in COALESCE`.
* **2 lost to pre-existing defects that also failed BEFORE**: `wikidbs__c00058`
  (a source column literally named `cast`, emitted unquoted by the DEDUPE
  compilation — already logged as §5 corpus-wide item 4) and `dlt__matomo`
  (adapter synthesizes a string surrogate for a `bigint` column — already logged in
  §5.5).

BEFORE, using the same criterion, 45 of 51 registered tasks had retained reference
results.
**`schemapile` went 5/8 → 7/8**: `findy_agent_vault` and `openbis/schema-047`,
both blocked BEFORE by the `_close_foreign_keys` composite-FK bug, now retain
reference results.

### 0.2 Summary measurements

Corpus-wide results for tasks with retained reference results use one instrument on
both sides:

| | ELT-Bench (n=100) | BEFORE (n=45) | **AFTER (n=42)** |
| --- | ---: | ---: | ---: |
| target columns / task, median | 14 (min 6, p75 24) | 3 (max 160) | **20** (p75 30, max 161) |
| **computed** target columns / task, median | 10 (min 4, p75 15) | 2 (max 6) | **11.5** (max 102) |
| tasks reaching the anchor **minimum** of 6 target columns | — | **13 / 45** | **35 / 42** |
| tasks reaching the anchor **minimum** of 4 computed columns | — | **9 / 45** | **35 / 42** |
| tasks reaching the anchor **median** of 10 computed columns | — | **0 / 45** | **21 / 42** |
| transform-**discriminating** attack cases / task, mean | UNMEASURABLE | 1.33 | **5.48** |
| mart columns carrying a declared `MartColumn.kind` | UNMEASURABLE | **0 / 527** | **1003 / 1047 (95.8%)** |

The AFTER median exceeds the anchor median on both axes. The number of tasks that
reach the anchor median of 10 computed columns increased from 0 to 21 of 42.

### 0.3 Per pool

Medians; `(min–max)` on the raw counts. `tscore` is `corpus/difficulty.py`
`transform_score` in the recorded implementation (see §0.4; the scorer was widened
as part of this verification).

| pool | n | target cols | computed cols | ≥6 tgt | ≥4 cmp | ≥10 cmp | disc. attacks/task | plan sigs | tscore |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `synsql` BEFORE | 12 | 3 (2–3) | 2 (1–2) | 0 | 0 | 0 | 1.67 | 1 | 0.160 |
| `synsql` **AFTER** | 10 | **15 (10–21)** | **12 (8–17)** | **10** | **10** | **5** | **9.00** | 3 | 0.462 |
| `fivetran` BEFORE | 9 | 17 (8–160) | 1 (0–6) | 9 | 4 | 0 | **0.00** | 9 | 0.430 |
| `fivetran` **AFTER** | 6 | **79 (24–161)** | **29 (3–102)** | 6 | 5 | 5 | **0.17** | 6 | 0.567 |
| `schemapile` BEFORE | 5 | 4 (3–4) | 3 (2–3) | 0 | 0 | 0 | 2.00 | 1 | 0.205 |
| `schemapile` **AFTER** | 7 | **30 (3–30)** | **24 (2–24)** | **6** | **6** | **6** | **7.14** | 2 | 0.840 |
| `wikidbs` BEFORE | 11 | 2 (2–3) | 1 (1–2) | 0 | 0 | 0 | 2.46 | 1 | 0.172 |
| `wikidbs` **AFTER** | 11 | **10 (2–21)** | **8 (1–17)** | **9** | **9** | **5** | **7.82** | 3 | 0.355 |
| `dlt` BEFORE | 8 | 5 (2–7) | 4 (2–6) | 4 | 5 | 0 | 0.38 | 7 | 0.239 |
| `dlt` **AFTER** | 8 | 5 (2–7) | 4 (2–6) | 4 | 5 | 0 | 0.38 | 7 | 0.239 |

`dlt` is byte-identical BEFORE and AFTER on every axis: 0 of its 37 mart columns
declare a `MartColumn.kind`, none of the five new op kinds appears, and it emits the
old `build_star` plan. `adapters/dlt.py::_library_marts` documents the reason for
declining the shape rather than inventing evidence (a dlt manifest
declares only `id BIGINT`, `<cursor> TIMESTAMP`, `payload JSON`, so no shape's parent
attribute / numeric measure / declared domain exists). It is nonetheless **a pool
that did not move**, and the fix is upstream in `tools/extract_dlt_manifest.py`.

Matched fivetran packages, BEFORE → AFTER, computed columns per task:
`reddit_ads` 6 → **102**, `apple_search_ads` 6 → **66**, `amazon_ads` 0 → **25**,
`snapchat_ads` 4 → **20**, `servicenow` 1 → 3.

### 0.4 Operator presence after the baseline count of 0 of 46

Two instruments cover different evidence. First, **AST predicates over the
compiled reference SQL** (task counts, tasks with retained reference results only):

| operator | BEFORE (n=45) | **AFTER (n=42)** | anchor (n=100) |
| --- | ---: | ---: | ---: |
| aggregation | 42 | 42 | 100 |
| join | 31 | 31 | — |
| null substitution | 18 | 29 | 91 |
| **extrema** | 9 † | **30** | 64 |
| **distinct** | 11 | 13 | 26 |
| **filtered aggregate** | **0** | **27** | 78 |
| **conditional (CASE)** | **0** | **27** | 80 |
| **ratio** | **0** | **26** | 55 |
| **window** | **0** | **25** | 12 |

† BEFORE's 9 "extrema" are a plain `MAX()` measure, not an argmax. AFTER's 30 are
`QUALIFY ROW_NUMBER() OVER (PARTITION BY … ORDER BY <measure> DESC, <tie-break>) = 1`
— the benchmark's argmax construct, which projects the row with the highest measure
after applying the tie-break.

Op-kind totals across the 42 AFTER tasks:
`source` 335, `derive` 177, `join` 99, `tie_break` 99, `filtered_aggregate` 54,
`conditional` 47, `ratio` 47, `extrema` 37, `window` 37, `aggregate` 32, `union` 8,
`dedupe` 7. BEFORE: `source` 328, `derive` 139, `tie_break` 87, `aggregate` 60,
`join` 36, `dedupe` 11, `union` 8 — and **zero of every other kind**.

Second, **the upstream prose tagger** (`ELT-Bench/analysis/analyze.py::tag_description`
over mart-column descriptions, which is what `data_model.yaml` provides to the
solver) — the only directly comparable instrument available for both corpora:

| | anchor | `fivetran` | `wikidbs` | `synsql` | `schemapile` | `dlt` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| mart columns | 2,466 | 525 | 149 | 153 | 183 | 37 |
| prose-computed | 55.0% | 45.3% | **76.5%** | **75.8%** | **79.8%** | 32.4% |
| distinct op families (was) | 13 | 9 (11 †) | **10** (2) | **11** (1) | **8** (2) | 4 (4) |
| tasks with `extrema` (was) | 64/100 | 0 (0) | **9/11** (0) | **10/10** (0) | **6/7** (0) | 0 (0) |
| tasks with `distinct` (was) | 26/100 | 1 (2) | **9/11** (0) | **10/10** (0) | **6/7** (0) | 0 (0) |
| tasks with `filtered_agg` (was) | 78/100 | 4 (—) | **9/11** (0) | **10/10** (0) | **6/7** (0) | 0 (0) |
| tasks with `ratio_pct` (was) | 55/100 | 0 | **9/11** (0) | **10/10** (0) | **6/7** (0) | 0 |
| tasks with `window_delta` | 12/100 | **0** | **0** | **0** | **0** | **0** |

`window_delta` remains at zero in every pool. The library
emits window functions (25 of 42 tasks) but never a period-over-period *delta* whose
prose the tagger recognizes — `temporal_grid`, the shape that carries `LAG`, is
selected on no real pool schema in this sample.

The specification/reference divergence that §4.4 found (109 fivetran columns whose
prose promises computation the reference does not perform, 26.4% of the pool) is now
measurable directly against `MartColumn.computed` instead of by proxy:

| pool | mart cols | prose says computed, key does not | key computes, prose does not say |
| --- | ---: | ---: | ---: |
| `synsql` | 153 | **0** | 5 |
| `wikidbs` | 149 | **0** | 5 |
| `schemapile` | 183 | **0** | 0 |
| `fivetran` | 525 | **35 (6.7%)** | **46** |

fivetran's specification/answer-key divergence dropped from 109/413 (26.4%) to
35/525 (6.7%) — **improved 4×, not eliminated**, and it now has a second failure in
the opposite direction (46 columns the reference computes and the prose does not say).

### 0.5 Difficulty-scorer changes required for this measurement

`corpus/difficulty.py::structural_features` counted **only** `MartOpKind.AGGREGATE`
and `MartOpKind.WINDOW`. Measured on this corpus: **27 of the 42 AFTER tasks emit a
GROUP BY and reported `aggregate_count = 0.0`**, because their op is labelled
`filtered_aggregate`. The scorer assigned a lower score when the plan used the more
specific aggregate label. The following additive changes were made:

* `aggregate_count` now counts the aggregate **family** (`aggregate` +
  `filtered_aggregate` + `distinct`) — one SQL construct, three labels.
* `window_count` counts `window` + `extrema` (an argmax compiles to
  `QUALIFY ROW_NUMBER() OVER (…)`, a window function by construction).
* `conditional` and `ratio` are projections and join `shaping_op_count`.
* Per-kind counts (`filtered_aggregate_count`, `distinct_count`, `extrema_count`,
  `conditional_count`, `ratio_count`) are emitted alongside the family totals, so
  the per-kind detail and the anchor's zero values remain available.

The change is additive: **every BEFORE number in this document is unchanged by it**
(legacy plans emit none of the new kinds), and the demo's `transform_score` stays
0.250 and its content hash stays `4676328c…`. AFTER `transform_score` medians move
from the pre-fix reading to: synsql 0.317 → **0.462**, fivetran 0.525 → **0.567**,
schemapile 0.725 → **0.840**, wikidbs 0.242 → **0.355**, dlt **0.239** (unchanged).
Note that §4.2's critique of the composite still stands in full — the anchor side is
still a placeholder, so these scores remain **non-comparable across the anchor row**.

### 0.6 Executed attack cases

Every declared attack case of every task with a retained reference result was
executed through the pipeline's own machinery
(`verification/attacks.py::run_attack`, which mutates the compiled reference and
scores exclusively through the one reward implementation) on all five populations.
`tools/verify_attackability.py` aggregates the result and binds each attack kind to
the construct it is the named wrong implementation of.

Corpus-wide results cover 37 of the 42 tasks with retained reference results: every
task that declares a
transform-discriminating attack at all; the 5 omitted are `fivetran` tasks that
declare none. Of 230 declared attacks, 229 were killed and 0 had mutation errors. One
was not killed on any population, as described below. BEFORE, the same pools declared 60 in total
(1.33/task); AFTER, 5.48/task.

Per construct, corpus-wide (`declared` counts task-instances of the case; a case
"kills" when the mutant scores below 1.0 on at least one population):

| construct | named wrong implementation | declared | **killed** | unkillable | populations that killed it |
| --- | --- | ---: | ---: | ---: | --- |
| `filtered_aggregate` | CASE lifted out of the aggregate (+ `filter_to_where`) | 50 | **50** | 0 | ctf 50, pri 43, res 43, str 41, dev 28 |
| `conditional` | CASE conditions: boundary flipped / `ELSE` dropped | 35 | **35** | 0 | ctf 33, str 28, res 27, pri 25, dev 13 |
| `LEFT JOIN` hop | INNER JOIN (hop 1 and hop 2 separately) | 31 | **31** | 0 | ctf 31, pri 16, res 16, dev 12, str 12 |
| `COALESCE` null default | default removed | 26 | **26** | 0 | ctf 26, pri 25, res 24, str 11, dev 10 |
| aggregate grain | `GROUP BY` key set changed | 25 | **25** | 0 | ctf 25, pri 19, res 19, str 19, dev 7 |
| `ratio` | `NULLIF` guard removed | 25 | **25** | 0 | ctf 25, dev 24, pri 24, res 24, str 24 |
| `window` + `extrema` | PARTITION BY / ORDER BY / tie-break dropped | 25 | **25** | 0 | ctf 23, res 19, pri 16, str 16, dev 6 |
| `distinct` | `COUNT(DISTINCT x)` → `COUNT(x)` | 13 | **12** | **1** | str 11, ctf 8, pri 8, res 8, dev 1 |

Every new construct's named incorrect implementation executes and reduces the
reward. The counterfactual population kills 221 of the 230 cases. On `schemapile`,
it is frequently the only population that separates the mutant from the reference.

The following two full reward matrices report the measured reductions.
`synsql__3d_object_modeling…__object_types_objects_top` (2 marts, 20 target columns,
15 computed):

| case | development | primary | resampled | counterfactual | stress |
| --- | ---: | ---: | ---: | ---: | ---: |
| reference | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `inner_join` | 1.00 | **0.50** | **0.50** | **0.50** | 1.00 |
| `no_null_default` | 1.00 | **0.50** | **0.50** | **0.50** | 1.00 |
| `dropped_filter` | **0.50** | **0.00** | **0.00** | **0.00** | **0.00** |
| `dropped_filter@filter_to_where` | **0.50** | **0.00** | **0.00** | **0.00** | **0.00** |
| `wrong_denominator` | **0.50** | **0.00** | **0.00** | **0.50** | **0.00** |
| `wrong_grain` | 1.00 | **0.00** | **0.00** | **0.50** | **0.00** |
| `wrong_window` | 1.00 | **0.50** | **0.50** | **0.50** | **0.50** |
| `custom@wrong_boundary_else` | **0.50** | **0.00** | **0.00** | **0.50** | **0.00** |
| `no_dedup` | 1.00 | **0.50** | **0.50** | 1.00 | **0.50** |
| `custom@wrong_boundary_inclusive` | 1.00 | **0.50** | **0.50** | 1.00 | **0.50** |

`schemapile__…autosolderwebapp…` (3 marts, 30 target columns, 24 computed) — only
the counterfactual distinguishes several mutants on this schema:

| case | development | primary | resampled | counterfactual | stress |
| --- | ---: | ---: | ---: | ---: | ---: |
| reference | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `inner_join` | 1.00 | 1.00 | 1.00 | **0.67** | 1.00 |
| `no_null_default` | 1.00 | **0.00** | **0.00** | **0.67** | 1.00 |
| `dropped_filter` (+`filter_to_where`) | 1.00 | 1.00 | 1.00 | **0.67** | 1.00 |
| `wrong_denominator` | **0.00** | **0.00** | **0.00** | **0.00** | **0.00** |
| `wrong_grain` | 1.00 | 1.00 | 1.00 | **0.67** | 1.00 |
| `wrong_window` | 1.00 | 1.00 | 1.00 | **0.67** | 1.00 |
| `custom@wrong_boundary_else` | 1.00 | 1.00 | 1.00 | **0.67** | 1.00 |

On `schemapile`, five of the eight discriminating mutants score 1.0 on development,
primary, resampled, and stress, and are separated only by the counterfactual's
witness rows. Remove that population and this task discriminates on three attacks
instead of eight.

#### Failures

1. **One attack is declared and unkillable.**
   `wikidbs__c00272__00272_geographicalscholarsdatabase :: no_dedup` scores **1.0 on
   every population** while declaring `expected_pass={stress: False}`. It is a
   **legacy-shape** task — 2 target columns, op sequence
   `source→source→dedupe→derive→join→aggregate→derive→tie_break`, i.e. the pre-build
   `build_star` — whose `SELECT DISTINCT` is a no-op on the provided rows.
   This is the same BEFORE defect on a task the shape library declined.
2. **13 of 230 declared attacks (5.7%) kill on a different population than declared.**
   Their `expected_pass` matrix is not met: the mutant dies on some population, just
   not the declared witness. By construct: `wrong_window` 3 (all wikidbs),
   `no_dedup` 4, `custom@wrong_boundary_inclusive` 2 (both synsql), `inner_join` 4
   (1 wikidbs + 3 dlt legacy). Named example: on
   `synsql__3d_object_modeling…__object_types_objects_top`, `no_dedup` and
   `custom@wrong_boundary_inclusive` both declare death on the counterfactual, score
   **1.0 there**, and die on primary/resampled/stress instead.
   The required-attack gate checks the matrix rather than whether the mutant is
   killed on any population.
   `tools/prove_plan_library.py` proves those claims on a synthetic 3-table fixture
   with hand-placed witness rows; on a real schema the shape's witness generator does
   not always reproduce them.
3. **`fivetran` still cannot distinguish correct from incorrect transform logic.**
   Across its 6 tasks it declares **one** transform-discriminating attack in total — a `no_dedup`
   on `servicenow` which scores `{dev 1.0, primary 1.0, resampled 1.0,
   counterfactual 1.0, stress 0.8}`, i.e. it kills only on stress and also misses its
   declared matrix. The other 23 cases are `constants` / `keys_only` / `no_op` /
   `skip_extraction`, which test only whether the solver emitted anything. This is
   the §3(c) finding unchanged: 255 recovered computed columns increased output
   width without increasing discrimination because the recovered marts are built by
   `build_projection` / `build_rollup` over inferred measures rather than by a library
   shape with declared witnesses. `dlt` is also unchanged at 0.375 attacks/task.

### 0.7 Remaining gaps

| gap | BEFORE | AFTER | verdict |
| --- | --- | --- | --- |
| `dlt` transform depth | 5 target / 4 computed, 1 op family | **identical** | Not closed. Blocked upstream in `tools/extract_dlt_manifest.py`: the manifest declares no real column schema, so no shape's evidence exists. Declining is correct; the pool did not move. |
| `fivetran` discriminating attacks | 0.00/task | 0.17/task | Not closed. 1 discriminating attack across 6 tasks. |
| `window_delta` (period-over-period) | 0/46 | **0/42** | Not closed. The library provides `LAG` in `temporal_grid`, but that shape is selected on no pool schema in this sample. Anchor: 12/100. |
| `wrong_grain` never instantiated | 0 of 88 marts | **25 task-instances declare it, all 25 kill** | Closed on synsql/wikidbs/schemapile; still 0 on fivetran and dlt. |
| declared attacks whose `expected_pass` matrix is met | UNMEASURABLE | **217 of 230 (94.3%)** | 13 kill somewhere other than the declared witness — the gate checks the matrix |
| the `cast` identifier-quoting defect | kills `wikidbs__c00058` | **still kills it** | Not closed (§5 corpus-wide item 4). |
| fivetran prose/answer-key divergence | 109/413 (26.4%) | 35/525 (6.7%) + 46 in the opposite direction | Improved 4×; not closed. |
| legacy `build_star` still in the corpus | — | **15 of 42 tasks** emit only legacy op kinds (all 8 dlt, 4 of 6 fivetran, 1 schemapile, 2 wikidbs) | partial rollout |

One new defect introduced by the build affected 2 of 47 tasks:
`generation/mart_plan.py::argmax_profile` (~line 3173) declares the extremal row's
identifier as `Extremum(column="top_row_id", source="f_id", type=ColumnType.BIGINT)`
— hard-coded, with a matching `COALESCE(…, 0)` null default. `ChainEvidence` has
no `bridge_key_type`, so when the bridge's key column is TEXT the compiled SQL is
`COALESCE("top_row_id", 0)` over a VARCHAR and DuckDB refuses:
`BinderException: Cannot mix values of type VARCHAR and INTEGER_LITERAL in COALESCE`.
It killed `synsql__3d_coordinate_system…rollup` (`components.component_id` TEXT) and
`schemapile__…openbis…schema_186` (`samples_all.id` TEXT), each after exhausting the
3-round repair budget. The fix is a `bridge_key_type` on `ChainEvidence` plus a
type-appropriate null default — the same treatment `bridge_amount_type` already has.

---

## Baseline conclusion

The generated corpus is larger than ELT-Bench and has lower transform difficulty.
The size difference is on the extract-and-load side, which the
benchmark reports as near-saturated for evaluated agents (best SRDEL 97–100%). The
lower difficulty is on the transform side, the only stage with measurable variance
(best SRDT 47.0–53.5%).

The main measurements are:

| | ELT-Bench (n=100) | generated (n=46) |
| --- | ---: | ---: |
| source columns to extract, median/task | 52.5 | **95–318** across the four wide pools |
| target columns to produce, median/task | 14 (min **6**, max 218) | **2–5** (fivetran 17) |
| target columns that must be **computed**, median/task | 10 (min **4**, max 92) | **1–3** |

4 of 46 generated tasks reach ELT-Bench's per-task minimum of 4 computed target
columns. All four are `fivetran` tasks whose computed columns are a synthetic
`COUNT(*)` created by the adapter rather than the package's measure. On the two operators
the upstream analysis names as its Hard-tier discriminators, the generated corpus
scores **extrema 0 of 46 tasks** (ELT-Bench: 64 of 100) and **distinct 0 of 46 in the
provided specification** (ELT-Bench: 26 of 100) — a `DEDUPE` op exists in 12 answer
keys but is never stated in the prose the solver is given.

Meanwhile `combined_score` says the opposite: generated median 0.299–0.601 against an
anchor median of 0.348, with **2 generated tasks in the `hard` band and 0 of the 100
ELT-Bench tasks in it**. §4 explains why that number is not comparable.

---

## 1. The comparison table

`corpus/difficulty.py::structural_features` + `structural_difficulty` in the recorded
implementation; medians, with (min–max) for the raw counts. The anchor row is the
comparison reference.

| pool | n | tables | backends | source cols | marts | mart cols | joins | aggregates | load | transform | composite |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **`eltbench_anchor`** | **100** | **6** (2–64) | **5** (2–5) | **52.5** (6–764) | **1.5** (1–9) | **14** (6–218) | **0 †** | **0 †** | **0.900** | **0.137 †** | **0.348 †** |
| `fivetran` | 9 | 10 (6–15) | 5 (3–5) | 148 (52–512) | 5 (1–6) | 17 (8–160) | 0 | 1 (0–6) | 1.000 | 0.430 | 0.601 |
| `wikidbs` | 11 | 9 (6–12) | 5 (4–5) | 318 (97–679) | 1 (1–1) | 2 (2–3) | 1 | 1 | 0.950 | 0.172 | 0.407 |
| `synsql` | 12 | 13 (4–29) | 4.5 (2–5) | 95 (43–148) | 1 (1–1) | 3 (2–3) | 1 | 1 | 0.965 | 0.160 | 0.400 |
| `schemapile` | 5 | 31 (14–69) | 5 (5–5) | 226 (90–654) | 1 (1–1) | 4 (3–4) | 2 | 1 | 1.000 | 0.205 | 0.444 |
| `dlt` | 8 | 5 (2–18) | 1.5 (1–2) | 15 (2–53) | 2.5 (1–3) | 5 (2–7) | 0 | 1.5 | 0.438 | 0.239 | 0.299 |
| `demo` (hand-written) | 1 | 3 | 3 | 8 | 1 | 3 | 2 | 2 | 0.443 | 0.250 | 0.308 |

† The anchor's `joins`, `aggregates`, `transform` and `composite` cells are
adapter artifacts, not measurements. `adapters/eltbench_anchor._anchor_mart` emits
exactly one `DERIVE` op per mart — 202 marts → 202 derives — because ELT-Bench
publishes its transform logic only as English column prose. So `join_count`,
`aggregate_count`, `window_count`, `filter_count`, `dedupe_count`, `union_count` and
`tie_break_count` are **0 on all 100 anchors by construction**, the anchor
`transform_score` has a maximum possible value of 0.400 under this representation,
and any pool that emits more than one op per mart scores higher by construction. The
last three columns are not comparable across rows. §4 quantifies this; §3c gives the comparison that
does work.

Bands (`corpus/selection.band_of`): anchor **46 easy / 54 medium / 0 hard**;
generated **8 easy / 36 medium / 2 hard**. Both figures are governed by the same
artifact — see §4.2.

---

## 2. Results by axis

"Match" means that the generated median is within roughly ±25% of the anchor
median. Other results are labeled harder or easier with the multiple.

### Load axis

| axis | anchor median | generated | verdict |
| --- | ---: | --- | --- |
| **backends/connectors** | 5 (p25 3.75, max 5) | fivetran 5, wikidbs 5, schemapile 5, synsql 4.5 · **dlt 1.5** | Match for four pools (delta 0 to −0.5); dlt easier, with 3.3× fewer |
| **hard backends** (rest/s3/mongodb) | 3 (max 3) | 3 for all pools except dlt (**1**) | Match; dlt easier |
| **source tables** | 6 (p75 10, max 64) | schemapile 31 (**+5.2×**), synsql 13 (+2.2×), fivetran 10 (+1.7×), wikidbs 9 (+1.5×), dlt 5 (−1.2×) | Harder for four pools |
| **source columns** | 52.5 (p75 88.25, max 764) | wikidbs 318 (**+6.1×**), schemapile 226 (+4.3×), fivetran 148 (+2.8×), synsql 95 (+1.8×), dlt 15 (**−3.5×**) | Harder for four pools; dlt easier |
| **declared FK relationships** | **UNMEASURABLE** (upstream provides no FK metadata; the adapter hard-codes `()`) | schemapile 64, synsql 16.5, wikidbs 13, dlt 0, fivetran 0 | not comparable |
| **rows to load** | 32,666 (min 16, max 157,378,539; corpus 366,755,377) | schemapile 6,900 · synsql 2,520 · fivetran 600 · dlt 300 · wikidbs (provided data, no `scale`; 157–1,147 rows/task per `dlt_wikidbs.md`) | Easier by 1–5 orders of magnitude; `load_score` does not read `PopulationSpec.scale` |

Four of five pools exceed the benchmark on extraction size. Upstream reports every
evaluated agent at ≥77% SRDEL and the best at 97–100%, so additional source tables
provide little training signal.

### Transform axis

| axis | anchor | generated | verdict |
| --- | ---: | --- | --- |
| **target models (marts)/task** | 1.5 (p75 3, max 9) | fivetran 5 (+3.3×) · dlt 2.5 · **synsql, wikidbs, schemapile: exactly 1 on every task** | fivetran harder; three pools are structurally constant |
| **target columns/task** | 14 (min **6**, p75 24, max 218) | fivetran 17 (match) · dlt 5 · schemapile 4 · synsql 3 · wikidbs 2 | 13 of 46 generated tasks reach the anchor minimum of 6; 6 of 46 reach the anchor median of 14, all fivetran |
| **computed target columns/task** (structural: named by an `AGGREGATE`/`WINDOW` op) | prose-measured 10 (min **4**, p75 15, max 92) | schemapile 3 · synsql 2 · dlt 2 · wikidbs 1 · fivetran 1 (max 6) | Easier by about 5× at the median; 4 of 46 tasks reach the anchor minimum of 4 |
| **distinct operator families/pool** (upstream tagger, mart-column prose) | **13** | fivetran 11 *(inherited vendor prose — see §3b)* · dlt 4 · wikidbs 2 · schemapile 2 · **synsql 1** | Easier |
| **extrema** (upstream: 64/100 tasks) | 161 cols / 64 tasks | **0 columns, 0 of 46 tasks — in every pool** | Absent |
| **distinct / `COUNT DISTINCT`** (upstream: 26/100 tasks) | 115 cols / 26 tasks | **0 in the prose.** A `DEDUPE` op exists on 12 of 46 tasks (wikidbs 11/11, demo) and is never mentioned in the provided prose | Absent from the specification |
| **conditional** (80/100), **filtered_agg** (78/100), **ranking_ties** (61/100), **ratio** (55/100), **window** (12/100) | 506 / 252 / 166 / 106 / 14 cols | 0 in synsql, wikidbs, schemapile; dlt has ranking_ties on 4 tasks; fivetran's counts are inherited vendor prose | Absent or limited |

### Discriminating power

The generated corpus includes executable incorrect-logic mutants, which the anchor
cannot represent. Counting only **transform-discriminating** attack
kinds (`inner_join`, `no_null_default`, `no_dedup`, `dropped_filter`,
`wrong_window`, `wrong_denominator`) and excluding the four plan-agnostic ones
(`constants`, `keys_only`, `no_op`, `skip_extraction`) that every mart offers:

| pool | attack cases/task (median) | **transform-discriminating attacks/task** | kinds present |
| --- | ---: | ---: | --- |
| `eltbench_anchor` | **UNMEASURABLE** (0 declared; the placeholder plan offers only the 4 plan-agnostic kinds) | — | — |
| `demo` | 5 | **4.00** | inner_join, no_dedup ×2, no_null_default |
| `wikidbs` | 6 | **2.45** | inner_join 11, no_dedup 11, no_null_default 5 |
| `schemapile` | 6 | **2.00** | inner_join 5, no_null_default 5 |
| `synsql` | 6 | **1.67** | inner_join 12, no_null_default 8 |
| `dlt` | 4 | **0.38** | inner_join 3 |
| `fivetran` | 4 | **0.00** | **none** — all 36 attacks are `constants`/`keys_only`/`no_op`/`skip_extraction` |

`fivetran` cannot currently distinguish correct transform logic from incorrect
transform logic. Its only attacks test whether the solver produced any output.

One gap is corpus-wide: `wrong_grain` is available on **61 of 88
generated marts** and is instantiated as a declared attack case **zero times**, in
every pool. Grain error is the failure mode a `GROUP BY` task most plausibly hits.

---

## 3. Main finding

The measurements separate structural size, transform depth, and discriminating
power. The current composite score does not distinguish a large task from a simple
one.

### (a) Structural size

`schemapile` ingests a 69-table / 258-relationship schema; `wikidbs` includes 679 source
columns in one task against an anchor p75 of 88.25; `synsql` spans 4→29 tables from a
pool of 16,027 admissible schemas. Backend mix matches the anchor (median 5
connectors, 3 hard backends) for four of five pools. This size does not produce
corresponding transform difficulty: `pearson(table_count,
transform_score) = −0.070` across schemapile's 7.7× span in table count, with
`mart_count` and transform-touched-table-count *literally constant* (population stdev
0.0000 for both). A 69-table schemapile task produces a mart over three tables; the
other 66 tables are extracted, loaded, and never referenced by any transform.

The one size axis where the generated corpus is decisively *below* ELT-Bench is
**row volume**: median 300–6,900 rows/task against the benchmark's 32,666, with four
tasks over 10M rows that upstream calls a qualitatively different failure mode
(exact-row-count grading plus append-only sync). No generated task is within three
orders of magnitude of those.

### (b) Transform depth: about 5× lower at the median

The generated mart is 2–5 columns wide with 1–3 computed columns and one operator
family. The ELT-Bench mart is 14 columns wide with 10 computed columns and 6 operator
families at the median, and the hard tail reaches 218 columns / 92 computed / 10
families. The minimum-threshold comparison is:

* **13 of 46 generated tasks reach ELT-Bench's minimum target width (6 columns).**
* **4 of 46 reach ELT-Bench's minimum computed-column count (4).**
* **0 of 46 produce an extremum, a window function, a ratio, a filtered aggregate or
  a `CASE` conditional in the answer key** (`window_count` = 0 on all 46;
  `filter_count` = 0 on 45 of 46 — only the demo carries a `FILTER`). The nearest thing to the
  benchmark's `distinct` — a `DEDUPE` op — exists on **12 of 46** (all 11 wikidbs
  plans plus the demo) and is **never mentioned in the provided column prose**, so a
  solver reading `data_model.yaml` is not told about it.

The plan shapes are templates. `synsql` emits the op
sequence `source→source→derive→join→aggregate→derive→tie_break` **12 times out of
12, with zero variation**, producing exactly two distinct transform feature vectors
and a `transform_score` that takes two values 0.0033 apart across a corpus spanning
4→29 source tables. `wikidbs` emits one template 11 times out of 11. `schemapile`
emits one three-table star regardless of whether the schema has 9 tables or 69.
Aggregate functions across all 12 synsql tasks: `COUNT` ×12, `SUM` ×8, nothing else.

### (c) Discriminating power

Structural size and transform depth are properties of the *specification*.
Discriminating power is a property of the *answer key and its adversarial closure*,
and it is close to orthogonal to both. The demo — 3 tables, 8 source columns, 3
target columns, `combined_score` 0.308, the lowest composite score in the
workspace by the composite — is the only task that has ever cleared the full gate
battery, and it carries 4 transform-discriminating attacks, 6 distinct op kinds and a
counterfactual population on which `inner_join` and `no_coalesce` score 0.0 while
development and stress score 1.0. `projcrm` (69 tables) scores 44% higher on the
composite and discriminates less.

Between pools, discriminating power tracks *plan variety*, not size: wikidbs 2.45
transform-discriminating attacks/task, schemapile 2.00, synsql 1.67, dlt 0.38,
fivetran 0.00. The discriminating conditions are often not exposed to the solver:
wikidbs preserves 35 of 150 declared FK edges as violated by the provided
rows (350 orphan child rows, 0 of 49 `required=True` edges violated — the adapter
derives `required` from the data and gets this right), but the single mart is a
parent roll-up whose LEFT JOIN excludes orphans.

The corpus exceeds the benchmark on size, is below it on transform depth, and
contains more adversarial-closure evidence than the benchmark's published
artifacts. Its targets remain small.

---

## 4. Measurement-model limitations

### 4.1 Comparable measurements

The load-side comparison is **exact per task, not only in aggregate**. Against
`ELT-Bench/analysis/data/tasks.csv`, all 100 anchors matched by name with zero
mismatches in either direction, Pearson and Spearman both **+1.000 with 100/100 exact
per-task matches** for `table_count` vs `n_source_tables`, `source_column_count` vs
`n_source_columns`, `backend_count` vs `n_connectors`, `mart_count` vs `n_models`,
`mart_column_count` vs `n_model_columns`. Corpus totals reproduce exactly: 835 source
tables, 8,247 source columns, 202 target models, 2,466 target columns, 366,755,377
rows, 60/100 tasks needing all five connectors. `measure-target` is deterministic —
two fresh runs produce byte-identical anchor stores.

*(Two published quantiles differ — connectors p25 4 vs 3.75, source columns p75 89
vs 88.25. Upstream `analyze.py` uses nearest-rank, `corpus/selection._quantile` uses
linear interpolation; re-running the same anchor values under upstream's estimator
reproduces 4.0 and 89.0 exactly. This explains the difference.)*

`table_count`, `backend_count`, `hard_backend_count`, `source_column_count`,
`mart_count` and `mart_column_count` are comparable on both sides. Every claim in
§2's load block and §3(a)–(b) rests on those six features.

### 4.2 Non-comparable anchor transform features

The anchor `transform_score` has a closed form, verified to 1e-12 on all 100 anchors:

```
transform_score(anchor) = 0.15·min(mart_count/10, 1)
                        + 0.15·min(mart_count/5,  1)
                        + 0.10·min(mart_column_count/30, 1)
```

The `join`, `aggregate` and `window` terms carry **0.60 of the transform weight and
are identically zero**, so the anchor transform score has a mathematical maximum of
**0.400** (observed max 0.385, `mailchimp`) and the anchor combined score has a
mathematical maximum of `0.3·1.0 + 0.7·0.400 = 0.580`
(observed max 0.5695). Consequences, all measured:

* Zero of the 100 ELT-Bench tasks reach the repo's `hard` band. `BAND_EDGES`
  cannot be calibrated against this distribution. When `fivetran` reports 2 hard-band
  tasks, that does not show that it is harder than ELT-Bench. Its mart plans expose
  features to the scorer that the ELT-Bench adapter does not.
* A generated task with 2 marts, 20 columns, 4 joins, 3 aggregates, 1 window and 6
  shaping ops scores 0.5333, higher than every anchor including `mailchimp` (9 marts,
  218 target columns, 92 of them computed, third-hardest task in the benchmark). A
  1-mart / 8-column / 1-join / 1-aggregate task sits at the 70th anchor percentile.
* The +0.894 Spearman between in-repo `transform_score` and upstream `t_score` is
  not evidence that the score measures transform difficulty. Upstream `t_score` regressed on `(n_models,
  n_model_columns)` alone gives R² = 0.667, and the in-repo score is a function of
  exactly those two variables. After regressing mart width out of upstream `t_score`,
  `corr(in-repo transform_score, residual) = +0.216`, while that residual correlates
  with `has_extrema` at +0.461 and `has_distinct` at +0.302. The transform score is
  primarily a mart-width measurement.
* `derive_count` and `shaping_op_count` equal `mart_count` exactly on all 100
  anchors. `relationship_count = 0` on all 100 is **UNMEASURABLE, not zero** —
  `mailchimp` and `shipping` include joins in their described transformations.
* `anchor_deviation_report()` will report a large positive deviation on all eight
  op-count features for *any* pool with real plans. Those deviations measure the
  adapter, not the corpus.

### 4.3 Parsing `evaluation/sql/` cannot recover transform operations

`anchor_baseline.md` §5.4 ranks "parse `ELT-Bench/evaluation/sql/*.sql` with sqlglot
and emit real `MartOp`s" as its first recommended fix, on the premise that 202
ground-truth transformation queries are committed. All 202 files were checked. They
are grading queries, not transformations:

```
total files:                202
containing SELECT *:        202
containing JOIN:              0
containing GROUP BY:          0
containing any of sum/count/avg/max/min(:  0
largest file: 117 bytes
```

Every one is `SELECT * FROM <schema>.<model> ORDER BY <sort key>;`. The checkout also
contains no dbt models directory or transformation SQL. ELT-Bench's
transform ground truth exists *only* as English column descriptions in
`elt-bench/*/<task>/data_model.yaml`. There is no transformation SQL to parse, so
the anchor's transform operations cannot be recovered structurally from these files.

### 4.4 Prose tagging across both corpora

Because the anchor's only transform artifact is column prose, the comparable instrument is
the one upstream itself uses: `ELT-Bench/analysis/analyze.py::tag_description`,
applied to mart-column descriptions on both sides. That input is what
`export/eltbench.py::build_data_model` provides to the solver as `data_model.yaml`, so
it is the same object on both sides. Imported unmodified, it reproduces all eleven
upstream operator counts on the anchors exactly (aggregation 833/100, null-sub
513/91, conditional 506/80, filtered-agg 252/78, ranking-ties 166/61, extrema 161/64,
distinct 115/26, arithmetic 114/20, ratio 106/55, existence-flag 41/25, window 14/12)
and 1,356/2,466 = 55.0% computed, confirming that the anchor import preserves
prose faithfully. Result across all six corpora:

| | anchor | fivetran | dlt | schemapile | wikidbs | synsql | demo |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mart columns | 2,466 | 413 | 37 | 18 | 27 | 32 | 3 |
| prose-computed | 1,356 (55.0%) | 132 (32.0%) | 12 (32.4%) | 13 (72.2%) | 16 (59.3%) | 20 (62.5%) | 3 |
| computed **per task** | **13.6** | 14.7 † | 1.5 | 2.6 | 1.5 | 1.7 | 3.0 |
| op families | **13** | 11 † | 4 | 2 | 2 | 1 | 2 |
| extrema, tasks | **64** | 0 | 0 | 0 | 0 | 0 | 0 |
| distinct, tasks | **26** | 2 † | 0 | 0 | 0 | 0 | 1 |

† `fivetran`'s prose numbers are false positives; this was a new finding in this analysis.
Cross-checking prose against structure — a column is structurally computed when an
`AGGREGATE`/`WINDOW` op names it and it is not a key column — gives:

| pool | mart cols | prose-computed | structurally computed | **prose-computed but structurally passthrough** |
| --- | ---: | ---: | ---: | ---: |
| synsql | 32 | 20 | 20 | **0** |
| dlt | 37 | 12 | 12 | **0** |
| wikidbs | 27 | 16 | 16 | **0** |
| schemapile | 18 | 13 | 13 | **0** |
| **fivetran** | **413** | **132** | **23** | **109 (26.4% of all its mart columns)** |

Four pools have exact agreement between generated prose and plan structure.
`fivetran` inherits the vendor's dbt field documentation onto columns its plan only
copies — `amazon_ads__ad_group_report.clicks` is documented "Total number of ad
clicks" while the mart plan is `source ×5 → derive → tie_break`, a pure projection.
This overstates difficulty and creates a **specification/answer-key
divergence** in 109 columns. A solver reading `data_model.yaml` is told to
aggregate something the reference passes through. It should be treated as a correctness
finding for the fivetran adapter owner, ahead of any difficulty work.

The tagger's error in the other direction is also measured and smaller: it scores
synsql `null_handling` at 0% although 8 of 12 marts structurally emit
`COALESCE(m_1, 0)`, because the generated prose says "0 if none." and the upstream
pattern is `\bif no\b`. The extrema/distinct/window/conditional zeros are **not**
affected — they were confirmed structurally (no `MAX`, no `DISTINCT`, no window
function, no `CASE` in any generated plan).

### 4.5 The caps and weights

| term | cap | anchors at/above cap | defensible? |
| --- | ---: | ---: | --- |
| `table_count` | 8 | **40/100** | **No.** Cap sits *below* the anchor p75 of 10 and 8× below the max of 64; `works_cycles` (64 tables) is indistinguishable from any 8-table task. |
| `backend_count` | 5 | **60/100** | **No as written.** The cap *is* the corpus maximum, so it rescales rather than saturates. |
| `hard_backend_count` | 3 | **63/100** | same |
| `join`/`aggregate`/`window`/`shaping` | 6/6/3/10 | **0/100 each** | **Inert.** Never approached on either side; asserted, never validated. |
| `mart_count` | 5 | 5/100 | reasonable |
| `mart_column_count` | 30 | 20/100 | **Too low.** The 218-column anchor and the 160-column fivetran task score identically to a 30-column one — and it is the only transform term with real discriminating power, at the smallest weight (0.10). |

Net effect: `load_score` takes only **14 distinct values over 100 tasks with a 40-way
tie at exactly 1.000**. A `load_score` near 1.0 identifies the top 40% of ELT-Bench
but does not distinguish positions within that group.

**The 0.3/0.7 weighting is defensible on its stated grounds** (SRDEL 97–100% vs SRDT
47.0–53.5%; 85.2% of 297 failed data models had the model present and the transform
defective). The caps invert it in practice: the measured median share
of `combined_score` contributed by the `0.3·load` term is **73.7% for anchors, 72.3%
for synsql, 64–70% for schemapile**. A composite intended to be 70% transform is
about 73% load, because the transform term remains low on both sides.

Two measured signals are unused: `source_column_count` is computed and carries no
weight, despite the widest dynamic range of any load feature (6→764, 127×); and
row volume is never computed at all, though every anchor's `PopulationSpec.scale` is
populated (`corr(log10 rows, load_score) = −0.116`).

### 4.6 One caveat about the target itself

ELT-Bench's per-task tiers are **not agent-measured**. `analyze.py` builds
`difficulty = 0.3·el + 0.7·t` from percentile ranks of its own tag-derived features
(`n_exposed_columns` 0.30, `n_derived_columns` 0.20, `n_distinct_ops` 0.15, …) and
then labels quartiles Easy/Medium/Medium-Hard/Hard. So "extrema appears in 88% of
Hard tasks vs 44% of Easy" is **partly definitional** — extrema is one of the tags
feeding the score that defines the tier. The only agent-*observed* facts in the
upstream analysis are the aggregate SRDEL/SRDT figures and the 297-failure
breakdown. The operator target is still the best available signal and this document
uses it, but it should not be treated as an empirically validated per-task
difficulty label.

### 4.7 Additional measurements needed

None of the following was done here:

1. **Adopt the prose tagger as the standing cross-corpus transform instrument**
   (§4.4). It is the only directly comparable operator measure that exists, it
   reproduces upstream exactly on the anchors, and it needs no new data. Wire it into
   `corpus/difficulty.py` as `operator_tags(task)` and report tag coverage next to the
   structural scores. Fix the two known bias directions: `\bif no\b` → also match
   "none"/"missing", and cross-check prose against plan so a passthrough column with
   aggregate prose is flagged rather than counted.
2. **Add a per-column `computed: bool` (or a provenance pointer) to `MartColumn`.**
   The IR cannot express the benchmark's 55%-computed transform measurement. This
   makes §3(b) measurable directly instead of by proxy.
3. **Extend `MartOpKind`** with `EXTREMA`, `DISTINCT`, `CONDITIONAL`, `RATIO` and
   `FILTERED_AGGREGATE`. The two hard-tier discriminators currently have nowhere to
   land, so no generator can be *asked* to produce them and no scorer can reward
   them.
4. **Empirical difficulty.** Nothing in this document is a solver measurement.
   `calibrate --empirical` on a stratified subset would convert every structural
   claim here into a pass-rate, and is the only way to validate `BAND_EDGES` against
   anything.
5. **Add `source_column_count` (cap ≈ anchor p75–p90, 88–200) and a log-scaled row
   volume feature to `LOAD_WEIGHTS`**, and raise `table_count`'s cap toward p90. The
   data is already in every IR.
6. Do **not** invest in reconstructing anchor `MartOp`s from SQL — §4.3, there is no
   SQL.

---

## 5. Required changes by pool

> Status as of the §0 re-measurement: Most of this section has been completed and
> its effect measured: fivetran's `dbt compile` / measure-recovery blocker (item 1)
> is fixed, schemapile's `_close_foreign_keys` bug is fixed, synsql's and wikidbs's
> one-template planners are replaced by the shape library, and corpus-wide items 1
> and 2 (widen the planner; add explicit extrema and distinct operations) landed. What
> remains open is listed in §0.7 — chiefly fivetran's zero discriminating-attack count
> (which item 1 was supposed to unblock and did not), `dlt` entirely, corpus-wide
> item 3 (`wrong_grain`, now instantiated on 3 pools but still absent on fivetran
> and dlt) and item 4 (the `cast` quoting defect, still open and still killing
> `wikidbs__c00058`). Read the text below as the diagnosis that produced the build.

The pools are ordered by distance from the benchmark distribution, with the work each
needs. The binding constraint in every case is generation rather than scoring:
retuning caps would change the numbers without changing a single task.

### 1. `fivetran`

Closest to the benchmark on target-side axes: **mart width 17 (anchor 14), marts
5 (anchor 1.5), 45.9 mart columns/task (anchor 24.7)** — the only pool that reaches
the anchor's target-side dimensions. It is also drawn from the same family of source
material as roughly a third of ELT-Bench, whose SaaS tasks *are* Fivetran dbt package
marts.

Three measured blockers, in order:

* `recover_measures` recovers 0 aggregate expressions from 67 of 67
  terminal marts (40 fail `sqlglot.parse_one` because `raw_code` contains
  `{{ ref(...) }}`; 27 bail on `{%` by design) — while **53 of those 67 marts
  textually contain `sum(`/`count(`/`min(`/`max(`/`avg(`**. The built manifest has
  `compiled_code = None` because `build_dbt_manifest.py` runs `dbt parse`, which does
  not compile. **Fix: run `dbt compile` in the manifest build (the generated duckdb
  profile can back it), or strip Jinja before parsing.** The pool's potential
  transform complexity can be determined only after this is tried.
* **109 columns whose prose promises computation the reference does not perform** (§4.4).
  Either provide the package measures (which fix 1 enables) or rewrite the inherited
  description. This is a correctness bug, not a difficulty gap.
* **28.1% column retention end-to-end** (1,468 declared → 413 provided), of which a
  further **153 columns are discarded without being recorded** by `_model_to_mart`'s
  aggregate branch in `_Grounding.dropped` or `plan.notes`. Record that loss, or
  provide the grounded passthrough alongside the measures.

There are also 0 transform-discriminating attacks across 9 tasks. Once package measures are included,
`no_null_default` / `wrong_grain` / `dropped_filter` become instantiable.

### 2. `wikidbs`

2.45 transform-discriminating attacks/task (the highest of the source pools), 11/11 plans
carry a `DEDUPE`, data with 35 of 150 FK edges violated, and
`required` correctly derived from the rows. Needs: **widen the target.** 2 mart
columns against an anchor median of 14 is the binding constraint; one template ×11
is the second. Also state `distinct` in the prose: the `SELECT DISTINCT` is in the
answer key and invisible to the solver, so the solver cannot implement it or be
graded on it as intended. Its anchor-matching `load_score` is
computed from assigned backends: `adapters/wikidbs.py:1263` hashes the table name
round-robin over five assigned connectors. Source width (318 columns) does not
contribute to the score.

### 3. `schemapile`

The 69-table / 258-relationship task produces a three-table star with 4 target
columns. Increasing schema size has been tested up to 69 tables and produces the
same plan. The mart planner needs multi-mart
output, more of the schema touched, extrema/distinct measures. One localized
generator bug blocks 3 of 8 candidates:
`generation/populations.py::_close_foreign_keys` (line 445) makes a single unordered
pass over relationships, so overlapping composite FKs overwrite each other (findy) and
children are closed before their parents (openbis). Needs a fixpoint over a
topologically ordered FK graph with per-column conflict resolution.

### 4. `synsql`

16,027 admissible schemas is the corpus's largest source of extraction diversity,
and its load profile exceeds the anchor (13 tables vs 6, 95 source columns vs
52.5). But **one plan shape, 12 times out of 12, `transform_score` spanning 0.0033,
one operator family.** `dedupe` is effectively unreachable: only 11 of 168,239 tables
(0.01%) lack a declared PK and the branch fires only when that table is also the
selected fact. Needs `adapters/synsql.py::_build_mart` changed — additional join
legs, extrema/argmax measures with explicit tie-breaks, `COUNT(DISTINCT …)`, `CASE`
conditionals, filtered aggregates, multi-mart output. **Until then it should not
count toward transform-side quota coverage.**

### 5. `dlt`

In an ablation, rebuilding all 8 TaskIRs with
`backends[i].options = {}` leaves `structural_features` **byte-identical and both
scores unchanged to full float precision**, because `structural_features` begins
`backends = {b.backend for b in task.backends}` and discards pagination, cursors,
write dispositions and transformer chains at the first statement. This is a scoring-model
finding: `load_score` measures the number of connector types, while dlt has few
connector types with multiple connector behaviors that the score omits.

Only 4 of 44 REST resources
paginate beyond one page (deepest 3, page_size 100); 26 declared incremental
cursors are never exercised because no population presents a later snapshot; and a
scan of every primary row file for `deleted`/`is_deleted`/`tombstone`/`archived`
returns **zero tombstones**. The `extraction_summary`
mart, a `UNION ALL` of `COUNT(*)` per endpoint — is scored as `union_count` folded
into `shaping_op_count`, indistinguishable from a filter.

Both changes are required: add multi-page fixtures, a second snapshot that exercises
cursors, and soft deletes; then represent those extraction features in
`LOAD_WEIGHTS`. One open bug: `dlt__matomo` fails reference with
`table 'visitors' column '_visits_id': cannot coerce 'visits_100' to bigint` — the
adapter synthesizes a string surrogate for a column it declares `bigint`.

### Corpus-wide changes

1. **Widen the mart planner.** Every pool's binding constraint is the same: 1–5
   target columns against an anchor minimum of 6 and median of 14.
2. **Add explicit extrema and distinct operations** (IR, planner, attack definitions,
   score). 0/46 vs 64/100 and 26/100 is the largest single gap to the benchmark.
3. **Instantiate `wrong_grain`.** It applies to 61 marts, but zero attack cases use
   it.
4. Fix the identifier-quoting defect in the reference compiler that killed
   `wikidbs__c00058` (a source column literally named `cast`, emitted unquoted). It
   will recur on any real schema using a reserved word.

---

## 6. Measurements not included

* **Empirical (solver-run) difficulty.** Every score here is structural.
  The SRDEL 97–100% / SRDT 47.0–53.5% figures are ELT-Bench's published agent
  results, not measurements of this corpus.
* **Anchor transform ops.** Not recoverable: the adapter does not reconstruct them
  and there is no upstream SQL to reconstruct them from (§4.3).
* **Anchor relationships and anchor attacks.** Both are artifacts of the
  placeholder plan, not properties of ELT-Bench.
* **The 55%-computed figure on the anchor side, structurally.** The IR has no
  per-column computed flag; §4.4's anchor column uses upstream's prose tagger, which
  carries the same false-positive risk demonstrated on fivetran and which cannot be
  cross-checked because ELT-Bench contains no reference SQL.
* **Sample sizes are small and non-random.** n = 5–12 per pool; quantiles at p25/p75
  are spread indicators, not stable estimates. synsql's 12 tasks are a deterministic
  size-stratified spread (lexicographically-first admissible schema at each table
  count), which skews topics toward `3d_*` prefixes. Transform-side uniformity
  generalizes by construction — it is one code path emitting one template — but the
  load-side quantiles would tighten with more tasks.
* **Contamination was not re-audited here.** One adjacency is worth a look by its
  owner: ELT-Bench includes `twitter_organic` (Fivetran's Twitter package family) and
  the fivetran pool includes a task from `dbt_twitter` (Twitter Ads). Model names do not
  collide (`twitter_organic__tweets` vs `twitter_ads__*`) but the metric vocabulary
  overlaps heavily.
* **For the original measurement, no repo code was modified and the 1,192-test suite
  was not re-run** because nothing
  changed that could affect it. Nothing was written to
  `elt-training-data/curation/packages_raw`.

  *(§0 update: the re-measurement changed one file —
  `corpus/difficulty.py`'s op-kind dispatch, §0.5 — and the suite was re-run twice:
  **1287 tests, OK, 1 visible skip**, before and after that change. The demo is
  **ACCEPTED, 10/10 gates**, and its content hash is still
  `4676328cd6a2cc4b788f9b85fc1e8b09a75d8a18d28e8e00e2ec882efdd49316`. Nothing was
  written to `packages_raw`.)*

* **§0 did not measure empirical difficulty either.** Every §0 number is structural
  or adversarial-execution; no solver was run on any generated task.
* **§0's `fivetran` attack execution is partial**: 1 of its 6 tasks was executed
  (the only one declaring a transform-discriminating attack). The other 5 declare
  none, so there was nothing to execute; this is the measured result.
* **§0 sample sizes are still small and non-random** (6–11 per pool, the same
  records as the BEFORE pass wherever they were still resolvable, so the comparison
  is matched rather than independent).

---

## 7. Reproduce

```bash
set -a && . ./.env && set +a  # from the repository root

# the anchor (deterministic; two fresh runs are byte-identical)
.venv/bin/elt-taskgen measure-target --workspace /tmp/anchor_baseline_ws \
  --bench-root ../ELT-Bench

# cross-pool tables, operator tagging, prose-vs-structure cross-check
.venv/bin/python docs/difficulty/scripts/measure_all.py
.venv/bin/python docs/difficulty/scripts/measure_discriminating_power.py
```

Both scripts read the workspaces below in place, write their JSON to
`/tmp/final_synth/`, and modify nothing. `measure_all.py` also imports
`ELT-Bench/analysis/analyze.py` unmodified for the operator tagger.

Workspaces read (built by the five per-pool measurement runs, see each report's own
Reproduce section):

| pool | workspace | reference-stage tasks |
| --- | --- | ---: |
| `eltbench_anchor` | `/tmp/anchor_baseline_ws` | 100 anchors |
| `synsql` | `ws-difficulty-synsql` | 12 |
| `fivetran` | `/tmp/ws_fivetran` | 9 |
| `dlt`, `wikidbs` | `/tmp/diffws` | 8 + 11 |
| `schemapile`, `demo` | `taskgen-workspace` | 5 + 1 |

### 7.1 Reproducing §0 (the AFTER measurement)

```bash
set -a && . ./.env && set +a  # from the repository root

# 1. rebuild a per-pool sample through ingest -> generate -> reference.
#    One workspace per pool so a pool can be re-run alone; each writes
#    <ws>/rebuild_report.json naming every task that did NOT reach reference.
.venv/bin/python tools/verify_difficulty_rebuild.py /tmp/verify_ws      synsql
.venv/bin/python tools/verify_difficulty_rebuild.py /tmp/verify_ws_ft   fivetran
.venv/bin/python tools/verify_difficulty_rebuild.py /tmp/verify_ws_sp   schemapile
.venv/bin/python tools/verify_difficulty_rebuild.py /tmp/verify_ws_wiki wikidbs
.venv/bin/python tools/verify_difficulty_rebuild.py /tmp/verify_ws_dlt  dlt

# 2. measure BOTH sides with ONE implementation. NOTE: the script that wrote
#    docs/difficulty/verify_before.json and verify_after.json was not kept, so
#    this step is no longer reproducible from this repo; the two JSONs are the
#    frozen record. docs/difficulty/scripts/measure_all.py is the surviving
#    (different-path, not byte-comparable) implementation.

# 3. attackability: execute every declared attack of every gold-frozen task
#    through the pipeline's own mutation + reward machinery, aggregated per
#    construct. ~4 min/task; writes docs/difficulty/verify_attacks.json.
.venv/bin/python tools/verify_attackability.py /tmp/verify_ws /tmp/verify_ws_wiki \
    /tmp/verify_ws_sp /tmp/verify_ws_dlt

# one task's full reward matrix, if you want to see a single case end to end
.venv/bin/python tools/prove_attacks.py /tmp/verify_ws <task-id>
```

The fivetran rebuild reuses an already-compiled dbt manifest when it finds one and
**refuses a parse-only manifest** (`_is_compiled` checks every model carries
`compiled_code`); absent one it shells out to `tools/build_dbt_manifest.py`, which
now runs `dbt compile`.

AFTER workspaces: `/tmp/verify_ws` (synsql), `/tmp/verify_ws_ft` (fivetran),
`/tmp/verify_ws_sp` (schemapile), `/tmp/verify_ws_wiki` (wikidbs),
`/tmp/verify_ws_dlt` (dlt), `/tmp/verify_demo_ws` (the demo fixture, driven by the
since-removed `demo` subcommand).

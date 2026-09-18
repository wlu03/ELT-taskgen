# Run report — `demo` (the built-in `customer_summary` fixture)

> **HISTORICAL (2026-08 demo snapshot).** Kept verbatim as a measured record of the run
> it describes. The `demo` subcommand and `FIX_PIPELINE.md` no longer exist, the
> pipeline has 15 ledger stages, and every one of the five source pools now
> reaches `release`. Read [`README.md`](../../README.md),
> [`docs/INTERFACES.md`](../INTERFACES.md) and [`docs/SOURCES.md`](../SOURCES.md)
> for what is true today.

One cold `elt-taskgen demo` run into an empty workspace, instrumented end to end: 13 ledger stages, 23 ledger rows, 3 content identities, 6 replayed agent exchanges, 15 executed attack mutants, 676 seconds, $0.00.

This is the reference implementation the other five sources are compared against. It is the only source with a one-command driver and the only one whose council transcripts are committed to the repo, so it is the only run where every agent exchange is byte-reproducible without spending money.

---

## 1. What this source is and where its data lives

**It has no data on disk.** The demo is the one source whose "raw material" is a Python literal. `demo_fixture.demo_task()` constructs a fully-populated `TaskIR` in memory; nothing in the module reads the filesystem, and there is no vendored corpus, no manifest, no download.

| fact | value |
| --- | --- |
| source id | `demo` |
| where the material lives | `src/elt_taskgen/demo_fixture.py` (19483 B, entry point `demo_task()`) |
| task id / family / cluster | `demo__customer_summary` (all three identical) |
| origin / license / attribution | `Origin.DEMO` / `CC0-1.0` / `elt-taskgen built-in demo fixture` |
| source tables | `customers`, `orders`, `order_items` |
| backends | `postgres` = 1, `mongodb` = 1, `files` (csv) = 1 |
| relationships | 2 (`orders.customer_id -> customers.customer_id` optional; `order_items.order_id -> orders.order_id` required) |
| marts | 1 — `customer_summary(customer_id, completed_order_count, total_spend)` |
| mart plan ops | 8 (`filter`, `dedupe`, `aggregate`, `join`, `join`, `aggregate`, `derive`, `tie_break`) |
| populations | 5 — development, primary, resampled, counterfactual, stress |
| attack cases in the fixture | 4 |
| lineage-root content hash | `4676328cd6a2cc4b788f9b85fc1e8b09a75d8a18d28e8e00e2ec882efdd49316` |

The only bytes committed for this source besides the fixture module are the six council transcripts:

```
tests/fixtures/transcripts/
├── semantic_author/eb332ddf1fd9dddd8bec63fe238474a88df43c3613670f44f3e6a12dc8226b12.json       24254 B
├── ambiguity_critic/1d5822c519c22f3fc56721176cad8553c5ecaf50c4616b7aba044e661cf37ea2.json        6554 B
├── population_adversary/4321ed487d7fcb2f01af3c445c87d6a72ab1c4917c9cb994a92f602fdefb1415.json   10466 B
├── shortcut_attacker/643a5094c37243cfc6add0b662221a1dbb8252057b20764008346ed6566fe7d2.json      12548 B
├── feasibility_reviewer/24c09154b9accd86cf4b081c3a70639e52c6d4a4e0b3d54128cfe377565e6e3b.json     946 B
└── independent_implementer/f0c30374787719cbf01fa4c52e315605aa55f08a0e6f1c02425b15dcbeaecb3a.json 94025 B
```

Six files, one per role, one exchange each. The filename **is** the transcript key:
`sha256(role_behavior_sha256(role) + "\n" + user_prompt)` (`review/providers.py:864`).

The run materializes its data at `generate` time. Measured row counts in this
run's workspace (**re-measured** after `source_data.realized_row_count` broke
the scale-hint == realized-count identity; the bolded figures are the ones that
moved, and the paragraph under the table explains why):

| population | declared scale | `customers` | `orders` | `order_items` | gold mart rows |
| --- | --- | --- | --- | --- | --- |
| development | 2 / 4 / 8 | 2 | 4 | 8 | 2 |
| primary | 1000 / 3000 / 9000 | **1026** | **2799** | **9506** | 1026 |
| resampled | 1000 / 3000 / 9000 | **1026** | **2799** | **9506** | 1026 |
| counterfactual | (literal rows) | 3 | 2 | 4 | 3 |
| stress | 200 / 20000 / 60000 | **194** | **20249** | **66306** | 194 |

**No realized count equals its declared scale, and none of them is round.** That
is the point, not an accident. The extract-load subtask is graded by
`upstream_eval.compare_stage1` — strict binary over this count vector and
nothing else — so while the generator realized `PopulationSpec.scale` exactly,
the whole EL answer was three round numbers a solver could read off the task
documentation ("approximately 1000 customers") and submit without opening an
artifact. `source_data.realized_row_count` now moves each table 2-7% off its
declared scale, deterministically (sha256 of `task_id`, table and the declared
count — no clock, no unseeded `Random`, byte-identical across clean rebuilds in
different workspace paths). It is keyed on the declared count rather than the
population name on purpose: `validate_population_coverage` forces
`primary.scale == resampled.scale` and `gates._gate_el_data_sensitivity`
requires the memorization pair's count vectors to agree, which they still do.
Development stays exact — 2 rows is below `REALIZED_DIVERGENCE_MIN_SCALE`, and
that population is solver-visible and ungraded anyway.

Stress diverges twice over: `realized_row_count` puts `orders` at 19285 and
`order_items` at 63149, and then the duplicate-header injection appends a
further 5% (it appends rather than replaces), landing 20249 and 66306.
`customers` has a primary key, so no duplicates are injected there and 194 is
the divergence alone. On-disk sizes: `populations/stress` ~7 MB,
`populations/resampled` ~1.1 MB, `populations/primary` ~1.1 MB,
`populations/{development,counterfactual}` 24 KB each.

---

## 2. BEFORE — the prior shape

For every other source, "BEFORE" is a file someone else wrote. For the demo, BEFORE is the fixture module, and the honest statement is that **a curator already did all of the work**. Here is the raw material, verbatim from `src/elt_taskgen/demo_fixture.py`.

The schema fragment — one table of three, with the two facts that make the task non-trivial (`nullable=True` on the FK, and an empty `primary_key` so duplicate headers are legal):

```python
TableSpec(
    name="orders",
    description="One row per order header (duplicates possible under stress).",
    columns=(
        ColumnSpec(name="order_id", type=ColumnType.INTEGER,
                   description="Unique order identifier."),
        ColumnSpec(name="customer_id", type=ColumnType.INTEGER, nullable=True,
                   description="Customer who placed the order; may be NULL."),
        ColumnSpec(name="status", type=ColumnType.TEXT,
                   enum_values=("cancelled", "completed"),
                   description="Order status."),
    ),
    primary_key=(),  # duplicates of full header rows allowed under stress
    business_key=("order_id",),
),
```

The seed rows — the counterfactual population is not generated, it is transcribed:

```python
COUNTERFACTUAL_LITERAL_ROWS: dict[str, tuple[Row, ...]] = {
    "customers": (
        {"customer_id": 10, "customer_name": "C10"},
        {"customer_id": 11, "customer_name": "C11"},
        {"customer_id": 12, "customer_name": "C12"},
    ),
    "orders": (
        {"order_id": 1101, "customer_id": 11, "status": "completed"},
        {"order_id": 1201, "customer_id": 12, "status": "cancelled"},
    ),
    "order_items": (
        {"order_id": 1101, "quantity": 1, "unit_price": 10.0},
        {"order_id": 1101, "quantity": 2, "unit_price": 15.0},
        {"order_id": 1101, "quantity": 1, "unit_price": 5.0},
        {"order_id": 1201, "quantity": 1, "unit_price": 100.0},
    ),
}
```

The connector snippet — backend assignment is three lines, and it is the entire multi-backend story:

```python
backends=(
    BackendAssignment(table="customers", backend=Backend.POSTGRES),
    BackendAssignment(table="orders", backend=Backend.MONGODB),
    BackendAssignment(table="order_items", backend=Backend.FILES,
                      options={"format": "csv"}),
),
```

**What a curator has to add to make it a task.** The fixture already supplies everything the pipeline cannot infer, and enumerating it is the most useful thing this source teaches — because for `synsql`, `wikidbs`, `schemapile` and `dlt`, each of these is a gap an adapter has to fill or a human has to write:

| curator input | fixture value | who supplies it for a real source |
| --- | --- | --- |
| mart contract (name, grain, key columns, typed output columns) | `customer_summary`, "one row per customer, including customers with no orders" | adapter heuristics or a human |
| declarative mart plan | 8 `MartOp`s | adapter; the LLM never writes it |
| trusted reference SQL | `REFERENCE_SQL` (DuckDB, 20 lines) | adapter or human — never an agent |
| five population specs with prose conditions | hand-written, incl. "INNER JOIN is indistinguishable here by design" | `generation/populations.py` defaults + human conditions |
| counterfactual literal rows | C10/C11/C12 | human |
| four required attack mutants with predicted matrices | `inner_join`, `hardcoded_primary_outputs`, `count_without_distinct`, `no_coalesce` | `verification/attacks.py` standard set + human |
| license / attribution | `CC0-1.0` | `config/sources.yaml` |

Only **one** artifact in the finished task is authored by a model: the solver-visible prose. Everything else is either hand-written in the fixture or computed. The demo therefore measures the factory's *plumbing* honestly and its *curation* not at all.

---

## 3. The exact commands that were run

```bash
cd /Users/wesleylu/Projects/Research/kang-lab/ELT-taskgen && set -a && . ./.env && set +a
rm -rf /tmp/runs/demo
.venv/bin/python -u -m elt_taskgen.cli demo \
    --workspace /tmp/runs/demo \
    --budget-per-task 3.00
```

**Replay vs live: the flag is not the decision.** `--budget-per-task 3.00` was passed and `--replay-only` was deliberately *not* passed — and it made no difference, because `cli.py::cmd_demo` hard-codes the choice:

```python
provider = _resolve_provider(args, workspace, replay_only=True)
```

`cmd_demo` also refuses to start unless `providers.transcripts_present(fixtures)` is true, and prints the refusal as an error with the remedy (`record-transcripts`). **The `demo` driver cannot make a live call.** Its banner says so on line 2 of every run:

```
elt-taskgen demo  (workspace: /private/tmp/runs/demo)
Offline, deterministic: replay-only council provider (recorded transcripts), no network access.
registered task 'demo__customer_summary'
running the pipeline (stages in FIX_PIPELINE.md order):
```

So this run is a **replay** run, not by choice but by construction. Measured live spend: **$0.0000**. To get live numbers for this task one must use `record-transcripts` (which requires keys) or drive the stages individually with `review --task-id demo__customer_summary --record`. The notional cost of the six recorded exchanges, priced with the table the CostMeter itself uses, is in §5.

Verification that the replayed prompts are the ones the current code produces (this is not part of the pipeline — it was run separately to make §5 quotable):

```python
from elt_taskgen import demo_fixture
from elt_taskgen.review import council, providers
task = demo_fixture.demo_task()
view = council._author_view(task)
providers.transcript_key("semantic_author", view) == "eb332ddf1fd9dddd8bec63f..."   # True
```

All six keys reproduce exactly; see §5.

One further command was run, as a control, to establish what `--record` actually does on this subcommand (result in §8.5):

```bash
rm -rf /tmp/runs/demo_record2
.venv/bin/python -u -m elt_taskgen.cli demo --workspace /tmp/runs/demo_record2 \
    --budget-per-task 3.00 --record        # exit 1, task REJECTED
```

### 3.1 The complete run output, verbatim

```
elt-taskgen demo  (workspace: /private/tmp/runs/demo)
Offline, deterministic: replay-only council provider (recorded transcripts), no network access.
registered task 'demo__customer_summary'
running the pipeline (stages in FIX_PIPELINE.md order):
  -> contamination_pre ...
     contamination_pre: pass
  -> generate ...
     generate: pass
  -> reference ...
     reference: pass
  -> author ...
     author: pass
  -> contamination_pre ...
     contamination_pre: pass
  -> generate ...
     generate: pass
  -> reference ...
     reference: pass
  -> review ...
     review: pass
  -> attack ...
     attack: pass
  -> contamination_pre ...
     contamination_pre: pass
  -> generate ...
     generate: pass
  -> reference ...
     reference: pass
  -> author ...
     author: pass
  -> review ...
     review: pass
  -> gates ...
     gates: pass
  -> calibrate ...
     calibrate: pass
  -> contamination_post ...
     contamination_post: pass
  -> select ...
     select: pass
  -> audit ...
     audit: pass
  -> release ...
     release: pass

Stage ledger for 'demo__customer_summary' (latest report per stage):
  intake               pass   rev=1  task_ir.json written
  contamination_pre    pass   rev=1  0 collision(s), 0 fatal
  generate             pass   rev=1  populations built=[] reused=['counterfactual', 'development', 'primary', ...
  reference            pass   rev=1  gold frozen for 5 population(s); determinism x3 identical
  author               pass   rev=1  solver prose unchanged; fidelity gate green
  review               pass   rev=1  14 finding(s), 0 fatal
  attack               pass   rev=1  15 attack case(s) executed on 5 population(s); 1 proposal(s) promoted, 0 ...
  gates                pass   rev=1  10/10 gates passed
  calibrate            pass   rev=1  structural difficulty only (empirical calibration not requested)
  contamination_post   pass   rev=1  0 collision(s), 0 fatal
  select               pass   rev=1
  audit                pass   rev=1  audit queue empty: no borderline collisions pending, license resolved
  release              pass   rev=1  frozen release release-862e2d9ce306b3e7 (1 task(s), 27 files)

Attack matrix (measured reward; '(exp pass/fail)' = spec expectation):
  case                                        development     primary         resampled       counterfactual  stress
  ----------------------------------------------------------------------------------------------------------------------------
  correct (reference)                         1.00 (pass)     1.00 (pass)     1.00 (pass)     1.00 (pass)     1.00 (pass)
  inner_join                                  1.00 (pass)     0.00 (fail)     0.00 (fail)     0.00 (fail)     1.00 (pass)
  hardcoded_primary_outputs                   0.00 (fail)     1.00 (pass)     0.00 (fail)     0.00 (fail)     0.00 (fail)
  count_without_distinct                      0.00            0.00            0.00            0.00 (fail)     0.00
  no_coalesce                                 1.00 (pass)     0.00 (fail)     0.00 (fail)     0.00 (fail)     1.00 (pass)
  proposed__population_adversary-02-af67d44a  1.00 (pass)     1.00 (pass)     1.00 (pass)     1.00 (pass)     0.00 (fail)

Counterfactual gold vs spec C10/C11/C12 rows: MATCH

Final verdict: ACCEPTED
Demo attack matrix reproduced exactly. Task frozen under /private/tmp/runs/demo/release.
```

Exit code 0. Wall clock, measured around the process: **676 s**.

The ledger reading printed at the end is the *latest report per stage*, which is why every line says `rev=1` and why the churn is invisible there — 23 rows collapse into 13 lines. §4.1 has the uncollapsed ledger.

---

## 4. Stage by stage

The ledger is the authority on what ran. `<ws>/state/taskgen.sqlite`, table `reports`, is INSERT-only; this run wrote the following rows.

### 4.1 The full 13-stage ledger (23 rows)

`sqlite3 /tmp/runs/demo/state/taskgen.sqlite "select id,stage,verdict,content_hash,created_at from reports order by id"`, with wall time derived from consecutive `created_at` deltas:

| id | stage | verdict | content_hash | created_at (UTC) | wall (s) | detail |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `intake` | pass | `4676328cd6a2` | 01:27:26.894 | — | task registered |
| 2 | `contamination_pre` | pass | `4676328cd6a2` | 01:27:26.910 | 0.016 | 0 collision(s), 0 fatal; index seeded with embedded deny lists |
| 3 | `generate` | pass | `4676328cd6a2` | 01:27:27.209 | 0.299 | populations built=[counterfactual, development, primary, resampled, stress] reused=[] |
| 4 | `reference` | pass | `4676328cd6a2` | 01:27:58.313 | **31.104** | gold frozen for 5 population(s); determinism x3 identical |
| 5 | `author` | pass | `af67d44ac510` | 01:27:58.329 | 0.016 | solver prose authored (3552 chars); fidelity gate green; content hash moves, all stages re-attest |
| 6 | `intake` | pass | `af67d44ac510` | 01:27:58.330 | 0.002 | task_ir.json written |
| 7 | `contamination_pre` | pass | `af67d44ac510` | 01:27:58.335 | 0.005 | 0 collision(s), 0 fatal |
| 8 | `generate` | pass | `af67d44ac510` | 01:27:58.337 | 0.001 | populations built=[] reused=[all five] |
| 9 | `reference` | pass | `af67d44ac510` | 01:28:24.375 | **26.038** | gold frozen for 5 population(s); determinism x3 identical |
| 10 | `review` | pass | `af67d44ac510` | 01:28:24.387 | 0.012 | 14 finding(s), 0 fatal |
| 11 | `attack` | pass | `d070c5726d31` | 01:37:49.178 | **564.791** | 15 attack case(s) executed on 5 population(s); 1 proposal(s) promoted, 0 rejected |
| 12 | `intake` | pass | `d070c5726d31` | 01:37:49.181 | 0.003 | task_ir.json written |
| 13 | `contamination_pre` | pass | `d070c5726d31` | 01:37:49.188 | 0.007 | 0 collision(s), 0 fatal |
| 14 | `generate` | pass | `d070c5726d31` | 01:37:49.189 | 0.001 | populations built=[] reused=[all five] |
| 15 | `reference` | pass | `d070c5726d31` | 01:38:14.247 | **25.057** | gold frozen for 5 population(s); determinism x3 identical |
| 16 | `author` | pass | `d070c5726d31` | 01:38:14.257 | 0.010 | solver prose unchanged; fidelity gate green |
| 17 | `review` | pass | `d070c5726d31` | 01:38:14.261 | 0.004 | 14 finding(s), 0 fatal |
| 18 | `gates` | pass | `d070c5726d31` | 01:38:33.298 | **19.038** | 10/10 gates passed |
| 19 | `calibrate` | pass | `d070c5726d31` | 01:38:33.301 | 0.002 | structural difficulty only (empirical calibration not requested) |
| 20 | `contamination_post` | pass | `d070c5726d31` | 01:38:33.306 | 0.006 | 0 collision(s), 0 fatal |
| 21 | `select` | pass | `d070c5726d31` | 01:38:33.309 | 0.003 | train=[demo__customer_summary], val=[], rejected={} |
| 22 | `audit` | pass | `d070c5726d31` | 01:38:33.310 | 0.001 | audit queue empty: no borderline collisions pending, license resolved |
| 23 | `release` | pass | `d070c5726d31` | 01:38:33.321 | 0.011 | frozen release release-862e2d9ce306b3e7 (1 task(s), 27 files) |

**23 rows, 13 distinct stages, 20 echoed stage executions, 3 identities, 0 repairs** (`select count(*) from repairs` = 0; `artifacts` = 60 rows). Ledger span first-to-last row: **666.427 s**. Wall clock of the whole process including registration and reporting: **676 s** (11 min 16 s).

Where the time actually goes:

| stage | executions | total (s) | share |
| --- | --- | --- | --- |
| `attack` | 1 | 564.791 | 84.7% |
| `reference` | 3 | 82.199 | 12.3% |
| `gates` | 1 | 19.038 | 2.9% |
| everything else (18 executions) | 18 | 0.399 | 0.06% |

Two stages are 97% of the run. Every other stage — including all five council roles and the independent implementer — completes in under a hundredth of a second, because they are transcript replays.

### 4.2 The three content identities

A content hash is the task's identity; a stage report is only valid at the hash it was recorded at (`engine.py:519-526`), so a hash-moving stage forces every earlier stage to re-attest.

| # | content_hash | created by | what changed |
| --- | --- | --- | --- |
| 1 | `4676328cd6a2cc4b788f9b85fc1e8b09a75d8a18d28e8e00e2ec882efdd49316` | `demo_fixture.demo_task()` at `intake` | the lineage root; `solver_prompt=""`, 4 attack cases |
| 2 | `af67d44ac510add898234038a5221978adbf6cd38537e8d7ea4aad135ff60290` | `author` (stage 5) | `solver_prompt` set to the 3552-char replayed opus-5 prose |
| 3 | `d070c5726d313ecce1bdcdd8bb788122fdab20e545cae09ffd3f33d72ba2b253` | `attack` (stage 11) | `attack_cases` grows 4 -> 5: `proposed__population_adversary-02-af67d44a` promoted |

Two observations that only a cold run makes visible:

* **The re-attest sweep is cheap except for `reference`.** After identity 2, `generate` reuses all five populations in 0.001 s and `contamination_pre` re-runs in 0.005 s — but `reference` genuinely rebuilds (26.0 s, then 25.1 s), because `freeze_gold` binds the gold bundle to `task_content_hash` and `answer_key/manifest.json` records it (`"task_content_hash": "d070c5726d31…"`). Three full reference executions is the price of two hash moves.
* **`attack` never re-ran at identity 3.** Its report was written *at the new hash* (row 11 carries `d070c5726d31`, not `af67d44a`), so on the sweep back through the stage list the resume predicate already sees a pass at the current identity. This is why the run costs ~565 s of attack rather than ~1130 s. It is also a subtle claim: the report asserts a 15-case measurement that was performed against the 14-case task, and then labels it with the 15-case hash.

**The identity chain measured here does not match the one already documented in this repo.** `docs/SOURCES.md:266` and `docs/RUN_EXAMPLE.json:38` record `4676328c -> 83258fd9 -> 3cff97c2`. Identity 1 still matches (it is pinned by `tests/test_models_round3.py:48` and `tests/test_models_pools.py:46`), but identities 2 and 3 are now `af67d44a` and `d070c572`. Identity 2 is a pure function of the fixture plus the committed author transcript, and it reproduces exactly:

```python
t2 = demo_fixture.demo_task().model_copy(update={"solver_prompt": author_transcript["response"]})
t2.content_hash()   # 'af67d44ac510add898234038a5221978adbf6cd38537e8d7ea4aad135ff60290'
```

so the `semantic_author` transcript was re-recorded after those documents were written, and every downstream identity moved with it. The promoted case name moved too: `proposed__population_adversary-01-83258fd9` in the old docs, `proposed__population_adversary-02-af67d44a` here (the finding index changed as well as the hash suffix). Both documents are stale and should be regenerated from this run.

### 4.3 Per-stage consumption and production

| stage | consumed | produced | wall |
| --- | --- | --- | --- |
| `intake` (×3) | the `TaskIR` in memory | `tasks/demo__customer_summary/task_ir.json` | 0.005 s total |
| `contamination_pre` (×3) | task family/schema fingerprints | ledger rows only; index at `state/contamination/` | 0.028 s total |
| `generate` (×3) | 5 `PopulationSpec`s | `populations/<pop>/rows/*.jsonl` (110 223 rows total) + `populations/<pop>/rendered/{postgres,mongodb,files}/` | 0.301 s total |
| `reference` (×3) | rendered backends + `REFERENCE_SQL` | `answer_key/gold/<pop>/{customer_summary.csv,stage1_counts.json}` (10 files), `answer_key/reference/{customer_summary.sql,solution.json}`, `answer_key/manifest.json`, `reports/determinism.json` | 82.199 s |
| `author` (×2) | `_author_view(task)` (3982 chars) | `solver_prompt` (3552 chars) on the TaskIR; prose-fidelity gate green | 0.026 s |
| `review` (×3) | 4 critic views (4350 / 4350 / 4350 / 6268 chars) | 14 `Finding` records in the ledger payload; 3 filter artifacts under `reports/filters/` | 0.016 s |
| `attack` (×1) | 14 findings + 4 standing cases + frozen gold | 15 dirs under `attacks/` (each `mutation_customer_summary.sql` + `rewards.json`); 1 promotion | 564.791 s |
| `gates` (×1) | everything above + the replayed independent build | `task/` public bundle, `answer_key/` private artifacts, `reports/contamination_post.json`, `reports/independent_build.json`; 10 gate records | 19.038 s |
| `calibrate` (×1) | the TaskIR alone | structural `DifficultyMeasurement` (load 0.4433, transform 0.25) | 0.002 s |
| `contamination_post` (×1) | the exported public bundle + answer key | ledger row, 0 collisions | 0.006 s |
| `select` (×1) | acceptance + difficulty | `train=[demo__customer_summary]`, `val=[]` | 0.003 s |
| `audit` (×1) | collisions + license | empty queue (`license: CC0-1.0`) | 0.001 s |
| `release` (×1) | the accepted task | `release/` — 27 content files + `release_manifest.json` + `checksums.sha256`, id `release-862e2d9ce306b3e7` | 0.011 s |

Final workspace footprint: **9.6 MB**, of which `populations/stress` is 6.6 MB and the frozen `release/` is 168 KB.

```
/tmp/runs/demo/
├── state/
│   ├── taskgen.sqlite                     # 23 reports, 60 artifacts, 0 repairs
│   └── contamination/                     # persisted fingerprint index
├── tasks/demo__customer_summary/
│   ├── task_ir.json                       # final TaskIR at d070c572
│   ├── populations/<pop>/rows/*.jsonl      # 110 223 rows across 5 populations
│   ├── populations/<pop>/rendered/         # postgres/*.sql, mongodb/*.jsonl, files/*.csv
│   ├── answer_key/gold/<pop>/              # 5 x (customer_summary.csv + stage1_counts.json)
│   ├── answer_key/reference/               # customer_summary.sql + solution.json
│   ├── attacks/<case>/                     # 15 cases: mutation SQL + measured rewards.json
│   └── reports/                            # per-stage evidence + reports/filters/*.json
└── release/
    ├── public/demo__customer_summary/      # config.yaml, data_model.yaml, documentation.md,
    │                                       #   schemas/*.csv, sources/ (DEVELOPMENT rows)
    ├── private/demo__customer_summary/     # gold/, gt/, reference/, evaluation/sql/,
    │                                       #   table.json, sort_key.json, flat_files_serving.json
    ├── release_manifest.json
    └── checksums.sha256                    # 28 entries (27 files + release_manifest.json)
```

---

## 5. PROMPTS — every role that was called

Six roles, one exchange each, all served from `tests/fixtures/transcripts/`. **The transcripts do not store the prompt text** — only `prompt_sha256`, `system_sha256`, `usage`, `raw_attempts` and `response`. Every view quoted below was therefore re-rendered from the current code and *verified by hash*: `providers.transcript_key(role, view)` reproduces the transcript's filename and `prompt_sha256` exactly, for all six. Any drift in the view builders or the system prompts would have broken these equalities.

| role | ROLE_SYSTEM key | system_sha256 | system len | provider / model | prompt_sha256 | view len | in tok | out tok | USD (notional) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `semantic_author` | `SHARED_PREFIX + _SEMANTIC_AUTHOR` | `6d2df14cca41…` | 10898 | anthropic / `claude-opus-5` | `eb332ddf1fd9…` | 3982 | 5287 | 5240 | $0.1574 |
| `ambiguity_critic` | `CRITIC_PREFIX + _AMBIGUITY_CRITIC` | `177a43258fcd…` | 8612 | anthropic / `claude-sonnet-5` | `1d5822c519c2…` | 4350 | 5135 | 890 | $0.0288 |
| `population_adversary` | `CRITIC_PREFIX + _POPULATION_ADVERSARY` | `1af4d6919bff…` | 9992 | anthropic / `claude-sonnet-5` | `4321ed487d7f…` | 6268 | 6928 | 1595 | $0.0447 |
| `shortcut_attacker` | `CRITIC_PREFIX + _SHORTCUT_ATTACKER` | `fd37fbae5afc…` | 9144 | anthropic / `claude-opus-5` | `643a5094c372…` | 4350 | 5241 | 1850 | $0.0725 |
| `feasibility_reviewer` | `CRITIC_PREFIX + _FEASIBILITY_REVIEWER` | `ccdd5cc1dd5e…` | 8819 | anthropic / `claude-haiku-4-5` | `24c09154b9ac…` | 4350 | 3916 | 33 | $0.0041 |
| `independent_implementer` | **none** (`role_system_prompt` returns `None`) | `e3b0c44298fc…` = sha256("") | 0 | openai_compat / `moonshotai/kimi-k2.7-code` | `f0c303747877…` | 9962 | 2202 | 10430 | $0.0000 |

**Actual USD spent by this run: $0.0000.** Every exchange was a cache hit; `cmd_demo` cannot call a provider. The USD column is the notional price of the six recorded exchanges under `providers.ANTHROPIC_PRICING_USD_PER_MTOK` — the same table the `CostMeter` charges against — and totals **$0.3074** against the `--budget-per-task 3.00` ceiling (10.2% of budget).

Routing comes from `config/agents.yaml` (`max_tokens` / `effort`): author `8192`/`high`, ambiguity `4096`/`medium`, population adversary `4096`/`medium`, shortcut attacker `4096`/`high`, feasibility `2048`/`null` (haiku rejects the parameter), implementer `16384`/`null`. `raw_attempts[0]` preserves the provider's own reply envelope. All four critics answered through the `report_findings` tool (`"stop_reason": "tool_use"`), not free text; the author answered as prose (`"stop_reason": "end_turn"`). Model-id recording is inconsistent: the feasibility reviewer's attempt records the dated snapshot `claude-haiku-4-5-20251001`, every other Anthropic attempt records only the alias (`claude-opus-5`, `claude-sonnet-5`), and the `openai_compat` attempt records no `stop_reason` at all. A transcript therefore does not reliably pin which weights produced it.

The `$0.0000` for the implementer is not free inference; it is an **unpriced route**. `config/agents.yaml` leaves `usd_per_mtok_input` / `usd_per_mtok_output` commented out for `openai_compat`, and `rates_for()` therefore returns `(0.0, 0.0)`. Anthropic models fail closed on a missing price ("budgets must never silently undercount"); OpenAI-compatible ones meter at zero. With `ELT_TASKGEN_OSS_*` pointing at OpenRouter, that 10430-token completion is real money the budget does not see.

### 5.1 `semantic_author` — the rendered view sent (verbatim, 3982 chars; chars 0-1521 shown)

```
TASK: Customer order summary

SOURCE SCHEMA:
- table customers (backend: postgres): One row per customer.
    - customer_id (integer): Unique customer identifier.
    - customer_name (text): Display name of the customer.
    primary key: customer_id
- table orders (backend: mongodb): One row per order header (duplicates possible under stress).
    - order_id (integer): Unique order identifier.
    - customer_id (integer; nullable): Customer who placed the order; may be NULL.
    - status (text; one of cancelled, completed): Order status.
    business key: order_id
- table order_items (backend: files): One row per line item of an order.
    - order_id (integer): Order this line item belongs to.
    - quantity (integer): Units purchased.
    - unit_price (decimal): Price per unit.
- relationship: orders(customer_id) -> customers(customer_id), optional (may be NULL or dangling)
- relationship: order_items(order_id) -> orders(order_id), required

MART PLAN SUMMARIES:
Mart 'customer_summary' is built by 8 declared operations:
1. [filter] Keep only orders with status = 'completed'. (tables: orders | columns: status | predicate: status = 'completed')
2. [dedupe] Deduplicate exact-duplicate completed order header rows: DISTINCT (order_id, customer_id). (tables: orders | columns: order_id, customer_id)
3. [aggregate] Per-order item total: SUM(quantity * unit_price) grouped by order_id. (tables: order_items | columns: order_id, quantity, unit_price | details: expression=quantity * unit_price; function=sum)
```

and it closes with the machine-checked contract:

```
=== BEGIN DATA MODEL ===
customer_summary:
  columns:
  - customer_id: integer. Unique customer identifier.
  - completed_order_count: integer. Count of DISTINCT completed orders; 0 if none.
  - total_spend: decimal. Sum of quantity * unit_price over items of completed orders; 0 if
      none.
  description: Per-customer completed-order activity summary.
  grain: One row per customer, including customers with no orders.
  key_columns:
  - customer_id
  rules:
  - Keep only orders with status = 'completed'.
  - 'Deduplicate exact-duplicate completed order header rows: DISTINCT (order_id, customer_id).'
  ...
=== END DATA MODEL ===

Write solver-visible prose describing the project and each mart. Do not include any SQL implementation or any output values.
```

The author's system prompt opens with the shared framing every role gets (`prompts.SHARED_PREFIX`, 2064 chars):

> You occupy one role in ELT-taskgen, an offline factory that turns candidate data projects into verifiable data-engineering tasks for reinforcement learning. […] EVERY ROLE HERE IS A PROPOSER. Nothing you write is a decision. Your answer is parsed into typed records and then CERTIFIED BY EXECUTABLE CODE — deterministic checkers, compiled mutants, measured rewards, fail-closed gates. Code decides; y…

### 5.2 The three critics that share one view (`ambiguity_critic`, `shortcut_attacker`, `feasibility_reviewer`) — 4350 chars, identical bytes

They receive the *same* user message; only the system prompt differs, which is why the three transcript keys differ despite an identical view. Verbatim head:

```
PUBLIC SOLVER PROSE:
# Customer order summary — solver specification

## Project overview

This project builds a single mart, `customer_summary`, from three source tables that must each be extracted from their own backend:

- `customers` — one row per customer — extracted from **postgres**.
- `orders` — one row per order header, where the same header row may appear more than once under some data conditions — extracted from **mongodb**.
- `order_items` — one row per line item of an order — extracted from **files**.

An order's `customer_id` is optional: it may be null, and it may also carry a value that matches no row in `customers`. Every row of `order_items` carries an `order_id` that belongs to an order.
```

and verbatim tail:

```
MART OUTPUT SCHEMAS:
- mart customer_summary (grain: One row per customer, including customers with no orders.; keys: customer_id)
    - customer_id (integer): Unique customer identifier.
    - completed_order_count (integer): Count of DISTINCT completed orders; 0 if none.
    - total_spend (decimal): Sum of quantity * unit_price over items of completed orders; 0 if none.

POPULATIONS (names and scales only):
- population development: scale [customers~2, order_items~8, orders~4]
- population primary: scale [customers~1000, order_items~9000, orders~3000]
- population resampled: scale [customers~1000, order_items~9000, orders~3000]
- population counterfactual: scale [literal constructed rows]
- population stress: scale [customers~200, order_items~60000, orders~20000]
```

Note the last line: the critics are told stress is `orders~20000, order_items~60000`. The materialized stress population is 20249 / 66306. The view reports the declared scale, not the built one — and since `source_data.realized_row_count` the two are guaranteed to differ, which is what stops the declared scale from being a free answer to the extract-load subtask.

### 5.3 `population_adversary` — the same view plus 1918 chars of conditions

This is the only role with a widened view, and the extra block is quoted verbatim below. It is also the one place where the information barrier is *deliberately* porous — the code says so (`council.py:228-247`, "HONEST LABELLING (Round-4 red team)"):

```
POPULATION CONDITIONS:
- population development: scale [customers~2, order_items~8, orders~4]
    condition: Tiny C1/C2 debug data: exactly two customers.
    condition: Every customer has at least one completed order (INNER JOIN is indistinguishable here by design).
    condition: No NULL customer_id, no duplicate rows.
- population primary: scale [customers~1000, order_items~9000, orders~3000]
    condition: Approximately 1000 customers.
    condition: Cancelled orders are present.
    condition: Some customers have no orders at all.
    condition: Some customers have orders but no completed orders.
    condition: Some orders have NULL customer_id.
    condition: Completed orders may have multiple items (COUNT DISTINCT matters).
- population resampled: scale [customers~1000, order_items~9000, orders~3000]
    condition: Same generator and conditions as primary; new seed and new id ranges (memorization check).
- population counterfactual: scale [literal constructed rows]
    condition: Literal constructed rows only — exactly the C10/C11/C12 case.
    condition: C10: customer with no orders -> (0, 0).
    condition: C11: one completed order with 3 items, 1*10 + 2*15 + 1*5 = 45; COUNT DISTINCT must be 1, item-grain COUNT would be 3.
    condition: C12: only a cancelled order worth 100 -> (0, 0).
    (this population is built from literal constructed rows; the row arrays themselves are not shown, but the conditions above are reproduced verbatim and may name concrete values)
```

Those four counterfactual conditions **are** the counterfactual gold: `(10,0,0.0) (11,1,45.0) (12,0,0.0)`. The label is honest about it, but the fact stands — one council role is handed a graded population's answer in prose.

### 5.4 `independent_implementer` — no system prompt, 9962-char user message

Sample index 0, no resample salt appended. The whole behavioral contract is in the user message; the head is the task-invariant preamble (`reference/independent.py::_IMPLEMENTER_PREAMBLE`), which describes the reward exactly as `upstream_eval.evaluate` implements it:

```
You are a senior data engineer. Implement the data-mart
transformations for the ELT project specified below, working ONLY
from that specification.

HOW YOUR ANSWER IS USED
Your SQL is not read as a proposal, it is EXECUTED. Each statement
you return is run unmodified against FIVE independently generated
datasets that all conform to the schema below but differ in scale,
skew, duplicate rates, null rates, and edge-case coverage. You see
none of them. The output of every mart is then compared column by
column against a frozen answer key for that dataset: the row count
must be identical, every declared column must be present (column
names are matched case-insensitively), numeric values must agree
within a relative tolerance of 1e-2, text must agree after trimming
and case folding, and NULLs must line up on both sides — a NULL
where a value is expected, or a value where a NULL is expected, is a
mismatch. A mart is all-or-nothing, on every one of the five
datasets: logic that happens to be right on the data you picture
and wrong on data you did not is wrong.
```

then the authored prose verbatim under `--- TASK DESCRIPTION ---`, then the schema, then:

```
--- RESPONSE FORMAT (STRICT) ---
Respond with ONLY one JSON object, no prose and no markdown fences,
whose keys are exactly the mart names ("customer_summary") and whose
values are complete standalone DuckDB SELECT statements producing
exactly the mart's declared columns, with those exact column names.
The source tables exist under their exact names; do not create
tables, load data, or emit more than one statement per mart.
The object is parsed by a strict schema check: any other shape —
extra keys, a missing mart, an empty string, prose outside the
object, an explanation of what you would do — is discarded as a
protocol failure and never read for intent.
```

---

## 6. AGENT OUTPUTS — what each role actually returned

### 6.1 `semantic_author` (opus-5, 5240 output tokens -> 3552 chars kept)

The prose is the only model-authored artifact in the released bundle. It survived the deterministic prose-fidelity gate and the declarative-prose gate. Rule 7, the rule the whole task turns on, reads:

> 7. For customers without completed orders, both measures read 0 and are never null: `completed_order_count` is 0 and `total_spend` is 0. The same holds wherever there is no amount to total — `total_spend` is 0 rather than null. A completed order with no item rows still counts toward `completed_order_count`.

The fixture's `MartSpec.grain` is one clause: `"One row per customer, including customers with no orders."` The author expanded it into the sharpest sentence in the document, and the second half is entirely new material:

> **Grain.** One row per customer, including customers with no orders at all. Every customer present in `customers` produces exactly one row, and nothing else produces a row: a completed order whose `customer_id` is null, or whose `customer_id` matches no customer, is attributed to no output row and therefore appears nowhere in the mart.

That clause resolves the NULL-FK case that the fixture only implies via `Relationship(required=False)`.

### 6.2 The council: 14 findings, 0 fatal

`review` reported `14 finding(s), 0 fatal` at both identity 2 and identity 3. Distribution: `ambiguity_critic` 3, `population_adversary` 5, `shortcut_attacker` 6, `feasibility_reviewer` 0.

**`ambiguity_critic` (3 findings, all `major`, all `route_hint: specification`, all `suggested_attack: no_dedup`)** — every one of them attacks rule 2:

1. *"Deduplication rule keys only on (order_id, customer_id), leaving orders with same order_id but differing status/customer_id ambiguous"* — "One reading: dedupe only after filtering to completed orders […] Another reading: dedupe is applied on the raw orders table before filtering by status, potentially collapsing a completed and a cancelled row into one status value nondeterministically."
2. *"Order-level duplicate across differing customer_id values not addressed: same order_id with two different customer_id values"* — "Since order_items join by order_id, the same item total could be attributed to both customers under one reading, but only to one customer under another."
3. *"Order-items deduplication is unspecified even though order_items may contain exact duplicate line-item rows"* — "One reading sums all matching rows including exact duplicates (higher total_spend); another reading deduplicates identical item rows first (lower total_spend)."

**`population_adversary` (5 findings)** — one is a genuine, executable, correct prediction; two are self-cancelling noise:

* Finding 00 (`major`) has the detail field `"This is a placeholder - see specific findings below."` and a 400-character run-on summary. It is not a finding; it is a model losing the thread, and nothing in the pipeline rejects it.
* Finding 01 (`major`) reasons carefully about INNER JOIN coverage and then ends: *"No defect here, retracting."* — a retraction that stays in the ledger at severity `major` because the schema has no way to express one.
* **Finding 02 (`major`, `route_hint: population`, with a `proposed_case`)** — the one that mattered: *"Rule 2 requires deduplicating exact-duplicate header rows […] Only the 'stress' population condition states 'Exact-duplicate order header rows are present'. Primary and resampled conditions list 'Completed orders may have multiple items (COUNT DISTINCT matters)' but never mention duplicate order headers […] So a no_dedup implementation would pass primary, resampled, counterfactual, and development, and only be caught by stress."*
* Finding 03 (`minor`, `dropped_filter`) — no population is stated to contain an orphan `customer_id` (present in `orders`, absent from `customers`).
* Finding 04 (`info`, `inner_join`) — no population guarantees a completed order with zero matching `order_items` rows. Severity `info`, so it is never compiled.

**`shortcut_attacker` (6 findings: 5 × `minor` with probes, 1 × `info`)** — this role did its job precisely. All five probes were compiled and executed:

* `constants`: *"the solver emits the development population's outputs verbatim, i.e. hard-coded rows for customer_summary […] on every population, computing nothing."*
* `keys_only`: *"SELECT customer_id FROM customers ORDER BY customer_id, with the measure columns completed_order_count and total_spend junked to constant 0."*
* `skip_extraction`: *"skip extraction of `order_items` from the **files** backend altogether […] and set total_spend=0 for every row (justified as the rule-7 null default). Two of three output columns would be fully correct."*
* `no_op`: *"emit customer_summary with columns customer_id, completed_order_count, total_spend and zero rows […] this probe must be executed against counterfactual specifically as standing evidence that its customers table is non-empty."*
* `constants` (second): *"emit the correct customer_id set […] with completed_order_count fixed at 1 and total_spend fixed at a single constant amount for every row."*
* `info`: *"Population development at customers~2 is small enough that its gold mart is fully transcribable by hand […] not to be compiled on its own."* — an observation the role explicitly declines to have compiled.

**`feasibility_reviewer` (0 findings)** — 33 output tokens, `{"findings":[],"role":"feasibility_reviewer"}`. Per its own prompt, that empty list asserts it traced every mart column and every rule. haiku-4-5 emitted it after 3916 input tokens.

### 6.3 The promoted attack case: predicted vs measured

Finding `population_adversary-02-af67d44a` attached a `proposed_case` of kind `no_dedup` with a full five-population prediction. `attacks.promote_proposed_cases` compiled it, executed it on all five populations against the frozen gold, and compared:

| population | predicted `expected_pass` | measured reward | verdict |
| --- | --- | --- | --- |
| development | `true` | 1.0 | match |
| primary | `true` | 1.0 | match |
| resampled | `true` | 1.0 | match |
| counterfactual | `true` | 1.0 | match |
| stress | `false` | 0.0 | match |

Exact match on all five, so the case was promoted to `required=True` and appended to `task.attack_cases` — which is what moved the task to identity 3. `0 rejected`; no `rejected_proposal.json` was written anywhere in the workspace.

The compiled mutant (`attacks/proposed__population_adversary-02-af67d44a/mutation_customer_summary.sql`) is the reference with `DISTINCT` dropped from the CTE and `COUNT(DISTINCT …)` weakened to `COUNT(…)`:

```sql
WITH completed_orders AS (
  SELECT
    order_id,
    customer_id
  FROM orders
  WHERE
    status = 'completed'
), order_totals AS (
  SELECT
    order_id,
    SUM(quantity * unit_price) AS order_total
  FROM order_items
  GROUP BY
    order_id
)
SELECT
  c.customer_id AS customer_id,
  COUNT(co.order_id) AS completed_order_count,
  COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM customers AS c
LEFT JOIN completed_orders AS co
  ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot
  ON ot.order_id = co.order_id
GROUP BY
  c.customer_id
ORDER BY
  c.customer_id
```

The prediction is *correct and non-obvious*: the adversary reasoned from population **conditions** (only stress declares duplicate headers) to a reward matrix, without seeing a single row, and the executor confirmed it. This is the strongest single piece of evidence in the run that the council is doing real work.

### 6.4 The independent implementer's SQL, verbatim

`moonshotai/kimi-k2.7-code` via OpenRouter, 10430 output tokens, response parsed to one mart:

```sql
SELECT
  c.customer_id,
  COALESCE(o.completed_order_count, 0) AS completed_order_count,
  COALESCE(o.total_spend, 0) AS total_spend
FROM customers c
LEFT JOIN (
  SELECT
    od.customer_id,
    COUNT(DISTINCT od.order_id) AS completed_order_count,
    COALESCE(SUM(ot.item_total), 0) AS total_spend
  FROM (
    SELECT DISTINCT order_id, customer_id
    FROM orders
    WHERE status = 'completed'
  ) od
  LEFT JOIN (
    SELECT order_id, SUM(quantity * unit_price) AS item_total
    FROM order_items
    GROUP BY order_id
  ) ot ON od.order_id = ot.order_id
  GROUP BY od.customer_id
) o ON c.customer_id = o.customer_id
ORDER BY c.customer_id
```

It scored **1.0 on all five populations** — `dual-build-agreement: status=agreed, samples=1`. Structurally it is not the reference: it pre-aggregates per customer in a derived table and applies `COALESCE` outside, where the reference aggregates in the outer query. It reached the same answer through a different shape, which is exactly what the gate is for. It also independently rediscovered both `DISTINCT`s the mutants attack.

---

## 7. AFTER — the final TaskIR, gates, matrix, verdict

### 7.1 The frozen task

| field | value |
| --- | --- |
| `task_id` | `demo__customer_summary` |
| `family_id` / `cluster_id` | `demo__customer_summary` / `demo__customer_summary` |
| `origin` / `license` / `attribution` | `demo` / `CC0-1.0` / `elt-taskgen built-in demo fixture` |
| `content_hash` | `d070c5726d313ecce1bdcdd8bb788122fdab20e545cae09ffd3f33d72ba2b253` |
| `status` at freeze | `released` |
| tables / columns | 3 / 8 |
| relationships | 2 (1 optional, 1 required) |
| backends | `postgres` 1, `mongodb` 1, `files` 1 |
| marts | 1 — `customer_summary`, key column `customer_id` |
| mart columns | 3 (`customer_id`, `completed_order_count`, `total_spend`) |
| populations | 5 (110 223 source rows total) |
| `solver_prompt` | 3552 chars |
| `attack_cases` | 5 (4 fixture + 1 promoted) |
| `revisions` | **1** — `{revision: 1, reason: "initial intake revision", parent: null, hash: 4676328c…}` |
| structural difficulty | load `0.4433`, transform `0.2500` |
| release | `release-862e2d9ce306b3e7`, 27 files, 168 KB |

The revision chain is worth flagging: the task moved through three content identities but recorded **one** revision. `author` and the attack promoter mutate the IR without appending a `Revision`, so the revision log does not describe the identity history — the `reports` ledger does. `cli._demo_task_is_resumable` depends on exactly this (it checks `revisions[0]` carries the pristine hash), so it is deliberate, but "revision chain" and "identity chain" are not the same object and should not be read as such.

### 7.2 Gate results — 10/10

| gate | passed | evidence |
| --- | --- | --- |
| `trusted-solution` | yes | reward 1.000000 on all five populations; `independent_build: agreed` |
| `determinism` | yes | 3 destroy/rebuild/rerun cycles on `primary`, all `e73c6e674ca6…` |
| `degenerate-zero` | yes | `no_op`, `keys_only`, `constants` all reward = 0.000000 |
| `required-mutants` | yes | 5 required mutants win/lose exactly where `expected_pass` says; 0 leaks |
| `shortcut-probes` | yes | all 6 shortcut-kind probes score < 1.0 on at least one graded population |
| `data-sensitivity` | yes | `primary-vs-counterfactual: differs`, `primary-vs-resampled: differs` |
| `info-content` | yes | `2/2 non-key columns vary over 1000 rows` |
| `populations-load` | yes | 14 / 13000 / 13000 / 9 / 84200 rows across 3 tables |
| `contamination-clean` | yes | 0 collisions, 0 fatal at `d070c572` |
| `dual-build-agreement` | yes | 1.000000 on all five populations, `samples: 1` |

The battery is ten gates, and the tree still says otherwise in five places. `cli.py:3228` (`demo` help text), `README.md:10`, `README.md:150` and `docs/INTERFACES.md:298` all say "15 stages" — `engine.STAGE_ORDER` has 13. `README.md:25`, `README.md:78` and `README.md:156` say "eight fail-closed gates" — `gates.GATE_NAMES` has 10, and this run printed `10/10 gates passed`. Also note `README.md:150` describes the demo as using "the deterministic mock council provider"; it uses the replay-only transcript provider against real recorded model output. The code is the authority in all three cases.

### 7.3 The measured attack matrix

Printed by the run, reward measured by `upstream_eval.evaluate` alone:

```
  case                                        development     primary         resampled       counterfactual  stress
  ----------------------------------------------------------------------------------------------------------------
  correct (reference)                         1.00 (pass)     1.00 (pass)     1.00 (pass)     1.00 (pass)     1.00 (pass)
  inner_join                                  1.00 (pass)     0.00 (fail)     0.00 (fail)     0.00 (fail)     1.00 (pass)
  hardcoded_primary_outputs                   0.00 (fail)     1.00 (pass)     0.00 (fail)     0.00 (fail)     0.00 (fail)
  count_without_distinct                      0.00            0.00            0.00            0.00 (fail)     0.00
  no_coalesce                                 1.00 (pass)     0.00 (fail)     0.00 (fail)     0.00 (fail)     1.00 (pass)
  proposed__population_adversary-02-af67d44a  1.00 (pass)     1.00 (pass)     1.00 (pass)     1.00 (pass)     0.00 (fail)
```

Plus the ten finding-compiled probes, measured but not required. The five that the `shortcut-probes` gate asserts:

| probe | kind | dev | primary | resampled | counterfactual | stress |
| --- | --- | --- | --- | --- | --- | --- |
| `finding__shortcut_attacker-00` | `constants` | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `finding__shortcut_attacker-01` | `keys_only` | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `finding__shortcut_attacker-02` | `skip_extraction` | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `finding__shortcut_attacker-03` | `no_op` | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `finding__shortcut_attacker-04` | `constants` | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |

`finding__shortcut_attacker-00` scoring 1.0 on development is the shortcut attacker's own prediction confirmed: the development split *is* transcribable, which is precisely why development is excluded from `GRADED_POPULATIONS`.

The three `no_dedup` mutants compiled from the ambiguity critic's findings, and `finding__population_adversary-02`, all measure `1.0 / 1.0 / 1.0 / 1.0 / 0.0` — identical to the promoted case, because the compiler produces the same mutant for the same kind. Four findings, one distinct executable consequence.

### 7.4 Verdict

```
Counterfactual gold vs spec C10/C11/C12 rows: MATCH

Final verdict: ACCEPTED
Demo attack matrix reproduced exactly. Task frozen under /private/tmp/runs/demo/release.
```

Exit code 0. Selection put the task in `train`, `val` empty, nothing rejected.

---

## 8. ANALYSIS

### 8.1 What this source teaches that the others cannot

* **It is the only end-to-end timing baseline.** 676 s wall, 85% of it in one stage, on a 3-table / 1-mart / 110k-row project. Every other source is larger, and `attack` cost scales as (cases × populations × rows). A source with 6 marts and a stress population an order of magnitude bigger does not cost 2× this; it costs closer to 20×. The demo is the smallest possible case and it still takes eleven minutes.
* **It is the only source where the agent layer is byte-reproducible.** Six committed transcripts, six hash-verified prompts, zero network. Any change to a view builder or a system prompt breaks a transcript key and the demo fails loudly — which makes the demo a regression test for the *prompts*, not just the plumbing.
* **It is the only source that demonstrates the promotion loop end to end** — a critic making a falsifiable five-population prediction, the executor confirming it exactly, and the task's identity moving as a result.
* **It is the only source that proves nothing about ingestion.** No adapter runs. No manifest is parsed. No license is resolved from a corpus. No contamination collision is ever hit (`0 collision(s)` at all three call points, and the index is seeded only with the embedded deny lists — this workspace never ran `measure-target`, so the firewall was disarmed for the entire run).

### 8.2 Where the difficulty actually comes from

Structural difficulty scores load `0.4433` and transform `0.2500`. The load score is carried almost entirely by `backend_count = 3` and `hard_backend_count = 1`; the transform score by `join_count = 2`, `aggregate_count = 2`, `dedupe_count = 1`. But the *measured* difficulty — which mutants actually die where — lives in three places, and only one of them is in the SQL:

1. **The optional foreign key.** `orders.customer_id` nullable + `Relationship(required=False)` is what makes LEFT vs INNER observable. `inner_join` and `no_coalesce` both die on primary/resampled/counterfactual and both survive development and stress, because those two populations declare "Every customer has at least one completed order (INNER JOIN is indistinguishable here by design)". The difficulty is a property of the *population conditions*, not the schema.
2. **The empty `primary_key` on `orders`.** That one line is what makes duplicate headers legal, which is what makes `no_dedup` a live mutant, which is the only thing the promoted case measures. Remove it and four of the fourteen findings become unexecutable.
3. **The C11 row.** `1×10 + 2×15 + 1×5 = 45` with three item rows for one order is the entire counterfactual population, and it is the only thing that kills `count_without_distinct`. Three rows of hand-written data do more discriminating work than the 84 200-row stress population.

The stress population, by contrast, discriminates almost nothing on its own: it is the *only* population that catches `no_dedup`, and it is a population where INNER JOIN and missing COALESCE both score 1.0. It is 6.6 MB and 70% of the reference-execution time for exactly one bit of discriminating information.

### 8.3 What the agents got right

* The **shortcut attacker** was the strongest role. Five concrete, parameterized, compilable probes; correct severity discipline (the one observation it did not want executed is marked `info` and was not compiled); and it predicted `constants`-on-development scoring 1.0, which is exactly what happened. It also named the right risk for `no_op` — "this probe must be executed against counterfactual specifically as standing evidence that its customers table is non-empty."
* The **population adversary's finding 02** is the single best artifact in the run: a five-population reward prediction derived purely from prose conditions, confirmed exactly by execution.
* The **author** added a grain clause that the fixture's `MartSpec` never states (null / unmatched `customer_id` is attributed to no row), closing a real gap.
* The **independent implementer** produced a structurally different correct solution, which is what makes `dual-build-agreement` meaningful rather than circular.

### 8.4 What the agents got wrong, and what the harness let through

* **`population_adversary-00` is not a finding.** Its `detail` is literally `"This is a placeholder - see specific findings below."` and its `summary` is a 400-character run-on containing the phrase "but more critically". It carries severity `major`. Nothing rejects it. It is in the ledger, it counts toward the `14 finding(s)` figure, and a downstream reader has no way to tell it from finding 02.
* **`population_adversary-01` retracts itself** — "No defect here, retracting." — and stays at severity `major`. The `Finding` schema has no retraction and no self-nullifying severity, so the council's own second thoughts are unrepresentable and simply accumulate.
* **The three ambiguity findings are one finding.** All three attack rule 2, all three carry `suggested_attack: no_dedup`, and all three compile to the *same* mutant with the *same* measured matrix as `finding__population_adversary-02`. Four findings, one executable consequence, four separate 25-second executions. There is no dedup on the compiled-mutant side.
* **The feasibility reviewer contributed nothing measurable.** 33 output tokens, empty list. That may be correct, but the run produces no evidence either way; the empirical cross-check its prompt promises (calibrator variants scoring zero) only runs under `--empirical`, which `demo` never passes.
* **Yield: 14 findings -> 11 compiled probes -> 1 promoted case -> 0 repairs.** The council changed the task exactly once. Everything else it said was either uncompilable, duplicated, or already covered by the four hand-written fixture mutants.

### 8.5 What is weak or missing, specifically for this source

* **The demo cannot make a live call, and its flags say otherwise.** `demo --replay-only` and `demo --budget-per-task 3.00` are accepted and inert: `cmd_demo` passes `replay_only=True` unconditionally, so no call is ever priced or made. **`demo --record` is worse than inert — it destroys the run**, and this was measured, not inferred:

  ```bash
  .venv/bin/python -m elt_taskgen.cli demo --workspace /tmp/runs/demo_record2 \
      --budget-per-task 3.00 --record          # exit code 1
  ```
  ```
    -> author ...
    -> author ...
    -> author ...
    -> author ...
    author               fatal  rev=4  repair budget exhausted (3 rounds); rejecting, never silently re-accepting
  ```

  `--record` sets `refresh=True`, which skips the cache lookup (`providers.py:1696`); `replay_only=True` then raises on the very next line. The recorded failure text is:

  ```
  TranscriptMissingError: replay-only mode: no recorded transcript for role 'semantic_author'
  / prompt eb332ddf1fd9 (searched: .../tests/fixtures/transcripts, /private/tmp/runs/demo_record2/transcripts)
  ```

  The file `tests/fixtures/transcripts/semantic_author/eb332ddf1fd9dddd….json` is in the first directory it names. The message is false, and the repair router then classifies it as a `specification` failure three times (`repairs` table: three rows, all `stage author failed; routed as specification`) before rejecting the task. Three flags on one subcommand: two silent no-ops and one that fabricates a spec defect. Either `demo` should reject these flags, or `cmd_demo` should honor them.
* **The OpenRouter route meters at $0.** Anthropic models fail closed on a missing price; `openai_compat` returns `(0.0, 0.0)` from `rates_for()` when the config omits rates, and `config/agents.yaml` omits them. The `independent_implementer` burned 10430 output tokens against a paid endpoint that the `CostMeter` recorded as free. Any budget claim for a live run of this pipeline is wrong by the size of that route.
* **Two committed documents are stale on this source.** `docs/SOURCES.md:266` and `docs/RUN_EXAMPLE.json:38,84` record identities `83258fd9` / `3cff97c2` and case name `proposed__population_adversary-01-83258fd9`; the measured values are `af67d44a` / `d070c572` and `proposed__population_adversary-02-af67d44a`. Identity 2 is a pure function of committed inputs, so this is a re-recorded transcript that nobody propagated.
* **The contamination firewall was never armed.** `contamination_pre` at identity 1 reports `"seeded_embedded_deny_lists": true`; identities 2 and 3 report `false`. `measure-target` was not run into this workspace (`/tmp/runs/demo/reference/` does not exist), so the index the checks ran against is:

  | index file | fingerprints | kinds present |
  | --- | --- | --- |
  | `state/contamination/eltbench.json` | 100 | `family:` only |
  | `state/contamination/spider2_dbt.json` | 16 | `family:` only |
  | `state/contamination/ade_bench.json` | 4 | `family:` only |
  | `state/contamination/admitted.json` | 37 (written by this run) | `family:`, `schema:`, `schema-table:`, `sql:`, `text:`, `deps:` |

  120 embedded family-name strings, no `schema:` fingerprints at all — against the 1232 fingerprints that `docs/SOURCES.md` §R records `measure-target` as arming from the 100 pinned ELT-Bench anchors. All three `contamination_pre` checks, the `contamination_post` scan and the `contamination-clean` gate are green against a token check, not a firewall. On the demo that is harmless — the fixture cannot collide with itself — but the demo is where a reader learns to read that line, and it teaches the wrong reading.
* **The counterfactual conditions hand the adversary the gold.** `C11: one completed order with 3 items, 1*10 + 2*15 + 1*5 = 45` is rendered verbatim into the `population_adversary` view. The code labels this honestly rather than hiding it, which is the right call — but it means the one role with the widest view is also the one role that could, in principle, reason from an answer rather than from a specification.
* **`attack` re-uses its measurement across an identity change.** Row 11's report is stamped `d070c572` but measures the 14-case task that existed at `af67d44a`. It saves ~565 s and it is almost certainly fine (the added case was measured during promotion), but it is the one place in the run where a report's content hash is not the hash the work was performed against.
* **The accepted run exercises no repair at all.** `repairs` table: 0 rows; every stage passed first time at every identity. The bounded-repair system, the five routes, and the `--repair-proposer` path are untested by the reference implementation the other five sources are compared against. The only repair behaviour this source demonstrates is the misrouting above — a transport failure (`TranscriptMissingError`) classified as a `specification` defect and "repaired" three times before rejection, which is the wrong route for a missing-transcript condition and burns the whole budget getting there.

## 9. ADDENDUM (2026-08-12) — the extract-load mutant battery lands; the EL variant ships

The two gates that refused the EXTRACT_LOAD variant ("10/12 gates passed ->
variant REFUSED") are now green, and the release ships all three bundles.
Ledger for release `release-dc31d4a0edfd0b79` (47 files; the EL-refused
release was 38 — the 9 new files ARE the `__el` bundle):

```
gates                pass  10/10 gates passed
gates_extract_load   pass  12/12 gates passed -> variant ACCEPTED
gates_transform      pass  11/11 gates passed -> variant ACCEPTED
release              pass  frozen release release-dc31d4a0edfd0b79 (1 task(s), 47 files)
```

Task identity moved on purpose: `19a93072…` -> `5f415b42…` (fixture; the
authored task freezes at `19363ecd…`) when the catalogue gained the
`fabricate_counts` pair and `header_as_row` became required. Council
transcripts were re-recorded for the new identity ($0.38 + $0.003 for the
independent-loader seed); the pipeline itself replays at $0.

### 9.1 What was added

`models.AttackKind` now declares all eight extract-load kinds that
`gates.EXTRACTION_MUTANT_KIND_NAMES` was already censusing
(`undeclared_el_kinds` is empty), and `verification/attacks.py` materializes
them as `directive:load:` mutations of the RENDERED artifacts — except
`fabricate_counts`, the one mutant with no artifact edit: it SUBMITS a
stage-1 answer read off nothing (the declared scale hint, or the frozen
primary vector via `directive:load:fabricate_counts:primary`) and builds no
warehouse at all. It is the mutant the §"realized row counts" divergence was
a precondition for: while the generator realized every hint exactly, the
documentation's numbers WERE the correct answer.

Applicability is decided, never asserted: a REQUIRED case with a missing
surface still raises (fail closed), while an informational probe records
`attacks/<case>/inapplicable.json` with the reason and contributes no reward
entry — the shortcut-probes gate reads that record and reports the honest
absence instead of failing on deleted evidence. On this task
`wrong_source_file` is the recorded exclusion: three tables on three
different backends, no same-backend pair to swap.

### 9.2 The measured EL matrix (evaluate_variant(EXTRACT_LOAD), frozen demo)

From `el_attack_proof.json` (regenerated against the frozen workspace);
1.0 = LEAK, 0.0 = kill; populations dev/pri/res/cf/str:

| case                          | dev | pri | res | cf  | str | note |
| ----------------------------- | --- | --- | --- | --- | --- | ---- |
| partial_backend (REQ)         | 0   | 0   | 0   | 0   | 0   | table absent -> count vector incomplete |
| duplicate_on_load (REQ)       | 0   | 0   | 0   | 0   | 0   | 2N != N wherever N >= 1 |
| header_as_row (REQ)           | 0   | 0   | 0   | 0   | 0   | typed column rejects header text (kill by load-error, recorded as such) |
| fabricate_counts (REQ)        | 1   | 0   | 0   | 0   | 0   | dev realizes its hint exactly BY DESIGN (below divergence floor, ungraded) |
| fabricate…_primary_echo (REQ) | 0   | 1   | 1   | 0   | 0   | leaks on the memorization pair — the recorded proof it carries zero EL signal |
| skip_extraction (REQ)         | 0   | 0   | 0   | 0   | 0   | postgres backend served empty through the real readers |
| truncate_table (probe)        | 1   | 0   | 0   | 1   | 1   | only `customers` spans a unit (1026 > 500-row postgres batch) |
| null_row_drop (probe)         | 1   | 0   | 0   | 1   | 1   | kills exactly where the spec puts NULL FKs — population-structure evidence |
| stale_snapshot (probe)        | 0   | 0   | 0   | 0   | 0   | every population graded against another's counts |
| wrong_source_file             | —   | —   | —   | —   | —   | INAPPLICABLE, recorded: no same-backend pair with differing counts |

`required-mutants(EL)`: "all 5 required extract-load mutant(s) win and lose
exactly where their expected_pass map says … 0 leaks; 4 required case(s)
excluded as inadmissible" (the four transform mutants — recorded, not
counted). `degenerate-zero(EL)`: empty_load / one_table_only /
uniform_source / fabricate_scale_hint / fabricate_primary_vector all score 0
on every graded population — the scale-hint submission that used to score
1.0 on primary and resampled now scores 0.0 there, which is the reward-floor
claim this whole sequence exists to make. `el-independent-load`: a
cross-family loader, shown only the public `__el` bundle, scored 1.0 on all
five populations.

# ELT-taskgen

> **HISTORICAL (2026-08-14 snapshot).** Kept verbatim as a record. The `demo`
> subcommand and `FIX_PIPELINE.md` no longer exist, the pipeline has 15 ledger
> stages, and every one of the five source pools now reaches `release`. Read
> [`README.md`](../README.md), [`docs/INTERFACES.md`](INTERFACES.md), and
> [`docs/EXECUTION_MODEL.md`](EXECUTION_MODEL.md) for what is true today. Its
> split-public-task policy and unscoped “certified” vocabulary are superseded;
> the body remains unchanged as historical evidence.

An offline factory that turns candidate data projects into verified RLVR ELT tasks — LLM agents propose and criticize; executable code decides acceptance.

One curated project produces two RLVR task units: Extract + Load (`__el`) and
Transform (`__t`). They have separate stage-specific rewards and must both be
accepted; the parent `TaskIR` is a shared curation record, not a third task.
See [`docs/TWO_STAGE_RLVR_CONTRACT.md`](docs/TWO_STAGE_RLVR_CONTRACT.md).

---

## 1. How it works

A candidate project (a dbt manifest, a SynSQL schema, a dlt connector spec, a SchemaPile record, a WikiDBs database, or the built-in demo fixture) is normalized into a typed Task IR and pushed through a **15-stage** pipeline. The authority on the order is `engine.STAGE_ORDER`:

```
intake -> contamination_pre -> generate -> reference -> author -> review
  -> attack -> gates (shared integrity) -> gates_extract_load
  -> gates_transform -> calibrate -> contamination_post -> select
  -> audit -> release
```

In prose: intake/extract -> pre-generation contamination check -> five
populations -> private reference execution + gold freeze -> semantic authoring
-> council review -> executable attacks -> shared integrity checks -> separate
EL and T acceptance batteries -> difficulty calibration -> post-generation contamination scan
-> selection -> human audit queue -> frozen release. Repair routing (5 bounded
routes) is **not** a ledger stage — it is what the engine does *between* stages
when one fails, so it never appears in `STAGE_ORDER`.

The two mermaid diagrams in [`FIX_PIPELINE.md`](FIX_PIPELINE.md) are the normative picture of this order (main pipeline, plus the reference-execution/gold-freeze detail). Module-level interface contracts live in [`docs/INTERFACES.md`](docs/INTERFACES.md). Six per-source run reports — what each source actually does today, measured — are in [`docs/runs/`](docs/runs/).

Design invariants, enforced structurally where possible:

- **Executable code decides.** The council produces findings only. Shared
  integrity uses the twelve fail-closed gates in `gates.GATE_NAMES`; EL and T
  then run their own reward-specific rosters. An `AcceptanceReport` cannot
  claim acceptance unless every recorded gate passed, and release also checks
  complete roster coverage.
- **Two task rewards, one scorer module.** `verification/upstream_eval.py`
  implements strict-binary Stage 1 reward for EL and per-mart Stage 2 reward
  for T. No FULL task is selected or released.
- **Five populations, same schema and logic, different data conditions:** development (tiny, solver-visible), primary (hidden reward), resampled (new seed — memorization check), counterfactual (constructed to break wrong logic), stress (scale/skew/duplicates/ties).
- **Determinism everywhere.** No wall-clock in generated artifacts; all RNG is derived per column (`derive_seed(task_id, population, table, column)`); canonical JSON/YAML dumps; repeated clean rebuilds must be byte-identical.
- **Fail closed.** Missing evidence or artifacts is a gate failure, never a pass. Repairs are routed from CHANGED ARTIFACTS (hash diffs), never from an LLM's opinion, and the repair budget is bounded — exhaustion rejects.
- **Append-only state.** The engine ledger (SQLite) is INSERT-only; the latest verdict is `MAX(id)`, and a later rejection always shadows an earlier accept.

---

## 2. Module structure

```
src/elt_taskgen/
├── models.py                # canonical typed Task IR: ids, tables/columns/relationships,
│                            #   backends, marts+grains, populations, reference metadata,
│                            #   attack cases, revisions + content hashes (FINAL — no edits)
├── engine.py                # append-only SQLite ledger + resumable 13-stage orchestration;
│                            #   stage runners injected by cli.py; unwired stage = abort
├── repair.py                # failure classification + routing: specification / population /
│                            #   runtime / reference / fatal, derived from artifact hash diffs
├── catalog.py               # the vendored-source catalog: root, Origin, license and
│                            #   attribution per pool (written down in config/sources.yaml)
├── adapters/
│   ├── dbt.py               # Fivetran/dbt manifest -> CandidateSpec -> TaskIR (connected-
│   │                        #   subgraph extraction; trivial cuts rejected)
│   ├── synsql.py            # SynSQL data.json/tables.json -> TaskIR; question/sql/cot/
│   │                        #   external_knowledge quarantined + leak-guarded
│   ├── dlt.py               # connector endpoint graph -> extraction task (typed stub)
│   ├── schemapile.py        # permissive SchemaPile record -> TaskIR, per-record licensing
│   ├── wikidbs.py           # WikiDBs database dir -> TaskIR on REAL rows, provenance stripped
│   └── eltbench_anchor.py   # pinned ELT-Bench task dirs -> anchor TaskIRs (measurement
│                            #   only — selection reads them; release refuses them)
├── generation/
│   ├── populations.py       # the five population specs + coverage validation
│   ├── source_data.py       # constraint-aware row generation + the five renderers
│   │                        #   (postgres SQL, mongodb jsonl, REST fixture dir, s3, csv)
│   └── mart_plan.py         # declarative record of intended relational ops per mart
├── reference/
│   ├── solution.py          # trusted extract+load + transform (SQL over DuckDB)
│   ├── runner.py            # clean-environment execution + N-run determinism evidence
│   ├── independent.py       # cross-family dual build of the mart transforms; feeds the
│   │                        #   'dual-build-agreement' gate (gold may not certify itself)
│   └── gold.py              # stage-1 counts + ordered stage-2 gold, frozen with hashes
├── review/
│   ├── council.py           # semantic author + four critic roles; pluggable provider;
│   │                        #   findings only (the council can block, never approve);
│   │                        #   leak scan: literal fragments + sqlglot anonymized-AST
│   ├── prompts.py           # written ROLE_SYSTEM prompts for the five council roles
│   ├── prose_fidelity.py    # deterministic completeness gate: authored prose must
│   │                        #   represent every mart/grain/column/rule (else SPEC repair)
│   ├── declarative_prose.py # deterministic gate: the prose states OUTCOMES, not SQL mechanics
│   ├── repair_proposer.py   # route-scoped repair patches, mechanically certified before
│   │                        #   a byte reaches the workspace; bounded, abstains into audit
│   ├── metrology.py         # council efficacy harness; live-admission record
│   └── providers.py         # real LLM backends (Anthropic + OpenAI-compatible), transcript
│                            #   replay/memoization, role routing (config/agents.yaml),
│                            #   cost budgets — key-optional, fail-closed
├── verification/
│   ├── contamination.py     # ONE index, TWO call points (pre/post) vs ELT-Bench ∪
│   │                        #   Spider2-DBT ∪ ADE-Bench ∪ previously admitted tasks
│   ├── attacks.py           # findings -> executable mutants. models.AttackKind has 12
│   │                        #   members: inner_join, no_dedup, wrong_grain,
│   │                        #   wrong_denominator, constants, keys_only, skip_extraction,
│   │                        #   dropped_filter, wrong_window, no_null_default, no_op,
│   │                        #   custom. Four of them (constants, keys_only, no_op,
│   │                        #   skip_extraction) are gates.SHORTCUT_KINDS.
│   ├── gates.py             # the TWELVE fail-closed acceptance gates (gates.GATE_NAMES)
│   ├── filters.py           # deterministic pre-council filters: code-only rejection
│   │                        #   BEFORE any provider spend
│   ├── perturbation.py      # the derived SCRATCH layer: a value bijection over a
│   │                        #   licensed snapshot, so 'data-sensitivity' can be proved
│   │                        #   on provided-rows pools (WikiDBs)
│   └── upstream_eval.py     # THE reward: stage-1 exact counts, stage-2 total-order
│                            #   column-compare with numeric tolerance (ELT-Bench semantics)
├── corpus/
│   ├── difficulty.py        # structural (load vs transform scored separately) + empirical
│   ├── calibration.py       # SolverCalibrator: measured per-variant difficulty from the
│   │                        #   PINNED solver roster in config/agents.yaml
│   └── selection.py         # acceptance predicate, quotas, family/cluster independence,
│                            #   train/val split isolation, anchor-profile comparison
├── export/
│   ├── eltbench.py          # upstream-compatible bundle: config.yaml, data_model.yaml,
│   │                        #   schemas/*.csv, table.json, sort keys, every-column ORDER BY
│   └── release.py           # freeze only: public bundles + private answer keys + manifest
├── demo_fixture.py          # the built-in customer_summary worked example (spec-fixed)
└── cli.py                   # subcommands: demo, record-transcripts, metrology, ingest-dbt,
                             #   ingest-synsql, ingest-schemapile, ingest-wikidbs, ingest-dlt,
                             #   measure-target (alias ingest-anchor), generate, reference-run,
                             #   review, attack, validate, calibrate, select, release, export,
                             #   triage, audit   (20 subcommands; `elt-taskgen --help` is the
                             #   authority)
```

---

## 3. Quick start

Requires Python >= 3.11. Dependencies: `pydantic` v2, `duckdb`, `pyyaml`, `sqlglot`.

```bash
python -m venv .venv
.venv/bin/pip install -e .

# the acceptance test of the whole system (offline, deterministic).
# MEASURED cold, into an empty workspace: 676 s wall — ~11 minutes, not 3.
# 85% of that is the `attack` stage. See docs/runs/demo.md.
.venv/bin/elt-taskgen demo

# run the test suite (~17 min on an idle machine; do not run it concurrently
# with anything writing to src/):
.venv/bin/python -m unittest discover -s tests
```

**The suite never spends money.** Exported credentials are not consent to use
them: a test that can bill a real API also requires the explicit opt-in
`ELT_TASKGEN_LIVE_TESTS=1`, and without it that test either replays recorded
transcripts (free) or SKIPS with a visible reason naming the variable and the
cost it declined. `ELT_TASKGEN_LIVE_TESTS` is referenced by exactly one test
module — the live council metrology run in `tests/test_council_efficacy.py`
(one paid completion per specimen x critic role over the blind metrology mix,
capped by the `$2.00` per-task CostMeter budget it constructs; note this is the
test's own ceiling, *not* the CLI's `--budget-per-task`, which defaults to
`$2.00` in `cli.py` and is what a real run should be given explicitly):

```bash
# opt in deliberately; this one bills the ANTHROPIC_API_KEY account:
ELT_TASKGEN_LIVE_TESTS=1 .venv/bin/python -m unittest tests.test_council_efficacy
```

All pipeline state lands in `./taskgen-workspace/` (override with `--workspace`):

```
taskgen-workspace/
  state/taskgen.sqlite          # append-only engine ledger
  state/contamination/          # persisted contamination fingerprint index
  tasks/<task_id>/
    task_ir.json                # canonical TaskIR dump
    task/                       # PUBLIC solver-visible bundle
    answer_key/                 # PRIVATE gold + reference + evaluation artifacts
    populations/<pop>/          # generated rows + rendered source backends
    attacks/<case>/             # mutated SQL + measured per-population rewards
    variants/<variant>/         # export-time subtask variants + per-variant reward.json
    reports/                    # evidence files + ledger report copies
  transcripts/<role>/<key>.json # recorded provider exchanges for replay/memoization
                                # (prompt_sha256, system_sha256, usage, response,
                                #  raw_attempts — the prompt TEXT is not stored)
  reference/anchors/            # imported ELT-Bench anchor TaskIRs — the GOAL we
                                # measure against: arms the contamination firewall
                                # and sets the target difficulty distribution.
                                # Measurement-only, NEVER trainable, never a source.
                                # (legacy <ws>/anchors/ is migrated here on load)
  release/                      # immutable frozen release (public/ + private/ + manifest)
```

---

## 4. The demo

`elt-taskgen demo` drives the built-in `customer_summary` fixture through all **13** stages with **no network and no external services** — but there is no mock provider any more. `MockProvider` was deleted. The council exchanges are **replayed real model output**: seven committed transcripts under `tests/fixtures/transcripts/<role>/` (one per role: `semantic_author`, `ambiguity_critic`, `population_adversary`, `shortcut_attacker`, `feasibility_reviewer`, `independent_implementer`, `independent_loader`), served by `RoutedProvider` in replay-only mode. `cli.py::cmd_demo` passes `replay_only=True` unconditionally, so the demo *cannot* make a live call — and `--budget-per-task` / `--replay-only` are inert on it. If the transcripts are absent the demo exits non-zero telling you to run `record-transcripts` with an API key; it never silently invents an answer.

The project: `customers(customer_id, customer_name)`, `orders(order_id, customer_id, status)` and `order_items(order_id, quantity, unit_price)` served from three different backends (PostgreSQL load SQL, MongoDB jsonl, flat CSV), transformed into one mart `customer_summary(customer_id, completed_order_count, total_spend)`. Rules: include customers with no orders; count DISTINCT completed orders; spend from completed-order items only; COALESCE to 0.

The counterfactual population is exactly the C10/C11/C12 case from the spec: C10 has no orders (0, 0); C11 has one completed order with 3 items where 1×10 + 2×15 + 1×5 = 45 and COUNT DISTINCT must be 1 (item-grain COUNT would say 3); C12 has only a cancelled 100 order (0, 0).

The demo exits 0 only when the task is ACCEPTED by all **twelve** gates AND the measured rewards reproduce the required attack matrix exactly. Measured, cold run (`docs/runs/demo.md` §3):

| submission | development | primary | resampled | counterfactual | stress |
| --- | --- | --- | --- | --- | --- |
| correct (reference) | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| INNER JOIN | 1.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| hard-coded primary outputs | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 |
| COUNT without DISTINCT | 0.0 | 0.0 | 0.0 | 0.0 (returns 3 for C11) | 0.0 |
| no COALESCE | 1.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| `proposed__population_adversary-02-af67d44a` (promoted at `attack`) | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |

Only the counterfactual cell of the `COUNT without DISTINCT` row is a *spec expectation*; the other four cells in that row are measured 0.0 but carry no expectation. The last row is not in the fixture — it is a council proposal promoted during `attack`, which is why a cold run advances the task through three content identities (`4676328c` fixture -> `af67d44a` authored prose -> `d070c572` promoted attack case) and writes 23 ledger rows across 13 stages.

Every attack is an executable mutant scored by the single upstream reward — the matrix is measured, never asserted by an LLM. A second `demo` invocation resumes from the ledger and completes in well under a second (measured: 0.0 s, zero stage executions): resumability is a LINEAGE predicate — the stored task's revision root must carry the pristine fixture hash — not an equality against the fixture.

---

## 5. Other subcommands

All subcommands accept `--workspace PATH` (default `./taskgen-workspace`).

| command | what it does |
| --- | --- |
| `ingest-dbt --package dbt_<name>` (or `--manifest m.json`) | vendored Fivetran package (manifest built on demand) or a prebuilt dbt manifest -> connected-subgraph TaskIR candidates |
| `ingest-synsql --tables t.json --db-id ID [--data d.json]` | SynSQL schema -> TaskIR, with the answer-leak guard |
| `ingest-schemapile --index idx.json [--list]` | clustered SchemaPile index -> one TaskIR candidate (per-record licensing) |
| `ingest-wikidbs --part N [--list]` | WikiDBs database dir -> TaskIR carrying its REAL rows |
| `ingest-dlt --connector NAME [--all]` | dlt connector manifest (`config/dlt_connectors/`) -> TaskIR candidate |
| `record-transcripts --task-id ID` | one-time seeding: run author+council LIVE (requires API keys) and persist transcripts for offline replay |
| `metrology` | council efficacy harness over the demo fixture; a pass writes the `council.live_admitted` admission record that admits live council routing, a block REVOKES it with a tombstone. The record is CONSULTED (`$ELT_TASKGEN_ADMISSION`), never copied |
| `measure-target --bench-root PATH` | pinned ELT-Bench = the GOAL: load `reference/anchors/` + arm the contamination firewall (measurement-only, never trainable, NOT a source; deprecated alias `ingest-anchor`) |
| `generate --task-id ID` | run the pipeline through population materialization |
| `reference-run --task-id ID` | through reference execution + gold freeze |
| `review --task-id ID` | through semantic authoring + council review |
| `attack --task-id ID` | through executable attack measurement |
| `validate --task-id ID` | through the acceptance gate battery |
| `calibrate --task-id ID` | through structural difficulty measurement |
| `select --task-id ID` | through the selection predicate |
| `export --task-id ID` | write the upstream-compatible bundle from frozen gold |
| `release --task-id ID` | run everything remaining and freeze the immutable release |
| `triage` | ADVISORY audit triage: label queued tasks for a human reviewer (never approves) |
| `audit {list,approve,reject}` | the human audit queue — the only path that signs anything off |

`elt-taskgen --help` is the authority on this list (20 subcommands plus the deprecated `ingest-anchor` alias). Each stage command is resumable orchestration (`engine.run(..., until=stage)`): completed stages at the current content hash are skipped, failures route through the bounded repair system, and fatal findings (contamination, licensing) reject.

---

## 6. What actually runs today

Six sources were run end to end and instrumented; the reports are in
[`docs/runs/`](docs/runs/) and are the evidence base for this table. **Ingest
works for all six. Exactly one of them produces a TASK.**

| source | furthest stage reached today | verdict | report |
| --- | --- | --- | --- |
| `demo` | `release` (all 13) | **ACCEPTED** — 10/10 gates, `release-862e2d9ce306b3e7` | [`docs/runs/demo.md`](docs/runs/demo.md) |
| `synsql` | `review` (fatal in the ledger; `attack` + `gates` measured out of band, 9 PASS / 1 FAIL) | rejected — 2 fatal findings x 3 repair rounds | [`docs/runs/synsql.md`](docs/runs/synsql.md) |
| `fivetran` (dbt) | `reference` (fatal) | rejected — `PlanCompilationError` x 3 rounds | [`docs/runs/fivetran.md`](docs/runs/fivetran.md) |
| `dlt` | `generate` (fatal, shipped path) / `author` (fatal, curator-probe path) | rejected on both paths | [`docs/runs/dlt.md`](docs/runs/dlt.md) |
| `wikidbs` | `reference` (fatal) | rejected — `PlanCompilationError` x 3 rounds | [`docs/runs/wikidbs.md`](docs/runs/wikidbs.md) |
| `schemapile` | `generate` (fatal) | rejected — counterfactual placeholder | [`docs/runs/schemapile.md`](docs/runs/schemapile.md) |

Five of the six died on one of two things that have nothing to do with the
source material: an empty `populations=()` at `generate`, or a `MartPlan` that
`reference/solution.py::compile_plan_sql` refuses. Total measured spend across
the six runs: **$4.15**. Cross-source synthesis:
[`docs/runs/README.md`](docs/runs/README.md).

---

## 7. Reference material

- Pinned benchmark for reward semantics and export shape: `ELT-Bench` (`elt-bench/snowflake/<db>/config.yaml`, `evaluation/{table.json, sort_key.json}`, `eva_stage2.py`).
- Proven gate ideas ported (with provenance noted in docstrings) from `elt-training-data/curation/scripts/`: `mutation_gate.py`, `determinism_check.py`, `data_mutation_gate.py`, `degenerate_probe.py`, `info_content.py`.

---

## 8. How to keep this honest

Most of the false claims this documentation has carried were *derivable from the
code* — a count that drifted when the code changed and the prose did not. Those
claims should not be maintained by hand at all. The derivable set:

| claim | single source of truth | where it is asserted in prose |
| --- | --- | --- |
| gate count + gate names (**12**) | `verification/gates.py::GATE_NAMES` | README §1, §2, §4; `docs/RUN_EXAMPLE.json` |
| stage count + stage order (**13**) | `engine.STAGE_ORDER` | README §1, §4; `docs/INTERFACES.md`; `cli.py`'s `demo` help text |
| council role list (author + 4 critics) | `review/prompts.py::ROLE_SYSTEM`, `review/council.py::CRITIC_ROLES` | README §2, §4 |
| routed role list (**9**: the 5 above, `independent_implementer`, `independent_loader`, `audit_triage`, `repair_proposer` — 7 on `anthropic`, 2 on `openai_compat`) | `config/agents.yaml` `roles:` | README §4 |
| subcommand list (**20** + 1 alias) | `cli.build_parser()` / `elt-taskgen --help` | README §2, §5 |
| `Origin` members (**8**) | `models.Origin` | `docs/SOURCES.md` §13 |
| `AttackKind` members (**12**) + `SHORTCUT_KINDS` (**4**) | `models.AttackKind`, `gates.SHORTCUT_KINDS` | README §2 |
| backends (**5**) / renderers (**5**) / repair routes (**5**) | `models.Backend`, `generation/source_data.py::render_*`, `repair.RepairRoute` | README §1, §2 |
| the demo lineage-root hash | `demo_fixture.demo_task().content_hash()`, pinned by `tests/test_models_round3.py` and `tests/test_models_pools.py` | `docs/SOURCES.md` §1, `docs/RUN_EXAMPLE.json` |

**Suggested tests** (none exist today; a docs agent cannot add them — these are
for whoever owns `tests/`):

1. `test_docs_counts.py::test_gate_count_in_readme` — regex the digit/word next
   to "fail-closed gates" out of `README.md` and assert it equals
   `len(gates.GATE_NAMES)`. Same for the twelve gate names appearing verbatim.
2. `test_docs_counts.py::test_stage_count` — assert the number next to
   "-stage pipeline" / "all N stages" in `README.md`, `docs/INTERFACES.md` and
   `cli.py`'s `demo` help string equals `len(engine.STAGE_ORDER)`. **`cli.py`
   still says "15 stages" (`cli.py:3366`, the `demo` subparser `help=`) and is out of this track's scope —
   it needs fixing by whoever owns `cli.py`.**
3. `test_docs_counts.py::test_subcommands_documented` — every choice in
   `build_parser()`'s subparser map appears in README §5's table.
4. `test_docs_counts.py::test_demo_identity_chain` — recompute identity 2 from
   the fixture plus the committed `semantic_author` transcript
   (`demo_task().model_copy(update={"solver_prompt": transcript["response"]}).content_hash()`)
   and assert `docs/RUN_EXAMPLE.json` records it. This is the exact claim that
   went stale when the transcript was re-recorded.
5. `test_docs_counts.py::test_module_tree` — every non-`__init__`
   `src/elt_taskgen/**/*.py` appears in README §2's tree, and every path in the
   tree exists. (This one caught eight modules missing from the tree in this
   pass: `catalog.py`, `adapters/schemapile.py`, `adapters/wikidbs.py`,
   `reference/independent.py`, `review/declarative_prose.py`,
   `review/repair_proposer.py`, `verification/filters.py`,
   `corpus/calibration.py`.)

Rule of thumb for this repo: **if a number in prose can be computed from the
code, a test should compute it.** Anything that cannot (wall-clock timings,
spend, per-source furthest stage) must be labelled with the run that measured
it, as the tables above are.

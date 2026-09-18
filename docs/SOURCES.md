# ELT-taskgen source inventory and runbook

ELT-taskgen has five task sources and one measurement-only reference benchmark.
ELT-Bench defines the comparison target and supplies forbidden contamination
fingerprints; it is never a task source. Section R appears before the source
sections to make that distinction explicit. This runbook records each source's
purpose, data location, exercised ingest command, resulting `TaskIR` shape,
independence unit, and operational requirements.

The dated ingest outputs and run snapshots below were executed against the
vendored data on disk. They are historical measurements, not proof that the
vendored bytes still match a named upstream or that a cloud runtime passed.
Current corpus and certification command blocks are operator procedures; they
make no success claim without the resulting verified bundle and, for a runtime
claim, a separate pinned live certification record.

Of the historical run logs under `docs/runs/`, only `demo.md` is published in
this repository. The five source-pool logs linked below are maintainer-local:
they quote hidden test material (reference solutions, graded population rows
and population conditions) and model transcripts.

## Current contract and five historical releases

In this document, "runnable" means that ingest succeeds. All five task-source
pools historically reached the legacy `release` stage. One task per pool is
frozen under `runs/<pool>_elt/release/`. These five on-disk releases predate the combined
runtime contract: each was frozen with two public subtask units (`__el` and
`__t`), accepted by separate roster-complete semantic batteries:

| § | source | historical drive | legacy task | legacy release id |
| --- | --- | --- | --- | --- |
| §3 | `fivetran` (dbt) | `runs/dbt_elt` | `dbt__reddit_ads__reddit_ads__account_report_d7014f04` | `release-01de4cddaad00ebe` |
| §4 | `synsql` | `runs/synsql_elt` | `synsql__soil_composition_and_horizon_analysis__soil_profiles_horizons_top` | `release-3616cd60f4d03a7c` |
| §5 | `dlt` | `runs/dlt_elt` | `dlt__workable` | `release-3deffd88541ebf07` |
| §6 | `schemapile` | `runs/schemapile_elt` | `schemapile__github_com_opencog_language_learning_e0eb3bdecec0__042316_poc_corpora_with_left_wall_sql` | `release-471190eecd9f4778` |
| §7 | `wikidbs` | `runs/wikidbs_elt` | `wikidbs__c00012__00012_metec_solarwatt_team_members_db` | `release-ceaa36a7f44db955` |
| §R | ELT-Bench | — | n/a — measurement only, never trainable | — |

Every one of those five ledgers carries a `release` row with verdict `pass` and
a historical `release_manifest.json` at `schema_version 2.0`,
`corpus_profile el_t`. Those fields describe the legacy split-public layout;
they are not the format produced by the current freezer. The manifests predate
the current `public_layout`, `release_mode`, and `certification_ids` fields and
carry no pinned live-runtime record. Their `pass` verdict therefore establishes
neither current bundle verification, independently refreshed upstream-source
provenance, nor Airbyte/warehouse/dbt runtime certification.

A current freeze writes schema `3.5`, profile `eltbench_end_to_end`, and
`public_layout combined`. It ships exactly one destination-bound runtime export
at `public/<task_id>/`. EL and T remain distinct private semantic phases, and
their suffixed
`private/<task_id>__el/reward.json` and
`private/<task_id>__t/reward.json` records are evidence, not public task units.
DuckDB population warehouses are retained only under the parent task's private
`oracle/` tree. The release also pins the canonical evaluator-only
TaskIR at `private/<task_id>/semantic/task_ir.json`, enabling no-cloud combined
semantic replay without exposing types, plans, or reference SQL publicly, and
schema 3.5 additionally freezes
`private/<task_id>/provenance/ingest_provenance.json` and binds its exact source
selector, upstream revision, source digest, adapter digest, and license evidence
along with its typed coordinator-entry projection, catalog, and pool-specific
SchemaPile index or WikiDBs family-map/node-inventory digests into the release
manifest and release identity. The separate ingest receipt binds the complete
five-source manifest. It also retains schema 3.4's
difficulty evidence plus chained semantic, runtime-bundle, and certification
identities. A separate pinned live record is
required for any scoped
`runtime_*_certified:<destination>` claim; see
[`EXECUTION_MODEL.md`](EXECUTION_MODEL.md).

The five schema-2 releases stay readable and are never re-judged, but they are
no longer fully verifiable: their `.duckdb` warehouses are pinned under
`duckdb-census/1` and this code computes census version `2`, so
`elt-taskgen verify --release runs/<pool>_elt/release` re-checks every
byte-pinned file and then fails (exit 1) on each warehouse. Re-running the
semantic batteries and re-freezing under the current schema-3.5 contract is what
makes a drive current and bundle-verifiable again; it does not certify a cloud
runtime. §1's
`demo` fixture is no longer a pool row at all: it is an in-code self-test with no
subcommand of its own (see §1).

The two historical blockers named here — `populations=()` at `generate`, and a
`MartPlan` that `reference/solution.py::compile_plan_sql` refused — are closed;
the per-source run reports in `docs/runs/*.md` describe the runs that hit them
and are historical, not the current census.

### Build and verify a current five-source corpus

Ingest at least one candidate from each real adapter into a single workspace,
then run the corpus coordinator:

```bash
ET=.venv/bin/elt-taskgen
WS=/tmp/five-source-corpus
AGENTS=/secure/config/agents-tier-a.yaml
INGEST=/secure/run/five-source-ingest.yaml
CORPUS_BUDGET=35    # operator-selected circuit-breaker target for five tasks

$ET pipeline --ingest-manifest "$INGEST" --workspace "$WS" \
  --agents-config "$AGENTS" --workers 4 \
  --budget-per-task 7 --budget-total "$CORPUS_BUDGET"

# On the tier-A/B host, create the pre-freeze observation at a new path.
$ET runtime certification attest-unbound \
  --agents-config "$AGENTS" --mount-root "$WS" \
  --out /secure/run/sandbox_attestation.json

$ET pipeline --ingest-manifest "$INGEST" --workspace "$WS" \
  --agents-config "$AGENTS" --workers 4 \
  --budget-per-task 7 --budget-total "$CORPUS_BUDGET" \
  --release-dir /tmp/five-source-release \
  --sandbox-attestation /secure/run/sandbox_attestation.json

$ET verify --release /tmp/five-source-release
```

Copy `config/five_source_ingest.example.yaml` to `$INGEST` and follow the
artifact instructions at the top of that file. It names the current verified
selectors and pins every primary source, adapter, catalog, SchemaPile index,
WikiDBs family map, and complete WikiDBs node inventory. In particular, the
operator must supply the dbt Twitter v1.3.1 compiled manifest with sha256
`f467ddab9e8518bfe51e86827df5e4cbcdb01fd95cf91c5c8bab3f03ebf0dfd4`;
the stale v1.3.0 local build is not an acceptable substitute. See the README's
"Pinned five-source intake" section for checkout and installed-wheel paths.

The source names enforced by default are exactly
`dbt,dlt,synsql,schemapile,wikidbs`. That guard checks the scheduled registered
roster before corpus publication; an intentionally partial development run must
pass `--require-sources ""`. With no `--task-id`, all registered candidates are
scheduled. With no `--size`, every scheduled task that remains eligible is
selected. `--size` is a post-processing selection count, not a scheduling or
spend cap: use explicit `--task-id` values and `--budget-total` to bound those.
Family/cluster deduplication and the EL+T pair requirement still fail closed.

The default four workers are isolated processes. Each owns its provider,
meter, engine, and database connection. WAL plus per-task process locks permits
different task identities to advance concurrently and serializes duplicate
work on one identity. A total live-call target is divided across the scheduled
tasks and no worker can reserve another worker's share. This is a circuit
breaker, not a prepaid aggregate cap: a first turn reserves only the known
prompt-input floor, so each worker can cross its slice by one final recorded API
call before halting.

Release-quality selection runs current empirical calibration by default and
requires exactly the EL and T records at the current TaskIR hash, under the
active pinned solver-roster fingerprint, with non-empty measured tiers. A
release freeze defaults to `release_mode=certified` and additionally requires a
fresh sealed tier-A/B isolation attestation. That label covers release
attestation, not destination execution. Use `--development-release` only for an
explicitly unlabelled local artifact; add `--allow-structural-difficulty` only
when a structural-only development artifact is genuinely intended. Neither
switch creates a runtime-certification claim.

`attest-unbound` seals both the exact `metrology.sandbox` pin and the canonical
content identity of the complete agents configuration. The certified pipeline
compares both values with its active `--agents-config`; use the same complete
file for curation, attestation, and freeze. Legacy sandbox schema-1.1 records
remain readable by unlabelled/development consumers but cannot back labels.
Post-freeze runtime certification independently re-observes the release-bound
sandbox and requires the same sealed config identity as the frozen release.
Its complete cleanup receipt must record exactly four grader-lane commands per
staged population (Terraform init/apply and dbt version/run); zero, missing, or
extra command evidence fails closed.

Do not describe a source as having a current release until the new immutable
directory exists and `elt-taskgen verify --release <dir>` succeeds. A
byte-verified development release is still not a certified release, and even a
certified release's sandbox attestation is not evidence that Airbyte and a real
warehouse passed. The latter requires the external certification state machine
described in the [execution model](EXECUTION_MODEL.md).

The remaining sections contain ingest details. Each "verified ingest command"
was confirmed to run and register a candidate. Registration alone does not make
a candidate a task; the candidate must complete all required pipeline stages.

---

This document records fixes to `cmd_demo`'s lineage-based resume predicate
(§1), the metrology skip guard (§12), the `ingest-synsql` contamination check
(§8, now in `adapters/synsql.py::assert_uncontaminated`), the dbt mart-key
fallback (§3), and the ELT-Bench anchor-store location and command name (§R).

Conventions binding on every section:

* Run from the repo root.
* `.venv/bin/elt-taskgen` is the installed console script; `.venv/bin/python -m
  elt_taskgen.cli` is equivalent.
* `--workspace` defaults to `./runs`. Every command below names it
  explicitly so a verification run cannot silently mutate the committed
  workspace.
* The vendored tree is read-only input. No adapter writes under
  `curation/packages_raw/`. This is enforced, not assumed — see §9.
* Every ingest routes through `verification/contamination.py::check_pre`
  before registering; a fatal collision is a refusal with a non-zero exit,
  never a warning. `ingest-synsql` was the one exception and no longer is —
  see §8.

Data roots referenced throughout:

```
DATA  = $ELT_TASKGEN_DATA_ROOT/curation/packages_raw
BENCH = ../ELT-Bench
```

Both are overridable via `${ELT_TASKGEN_DATA_ROOT}` / `${ELT_TASKGEN_BENCH_ROOT}`;
in a source checkout, unset values may discover same-named sibling checkouts.
An installed wheel requires explicit variables and never embeds a developer's
home directory. The one
place the four facts per pool (root, Origin, license, attribution) are written
down is `config/sources.yaml`.

---

## R. ELT-Bench — measurement-only reference

> ELT-Bench is not a task-source pool. Its imported records cannot become
> training tasks.

### Description

The pinned ELT-Bench checkout contains 100 tasks imported as measurement-only
`TaskIR` anchors (`Origin.ELTBENCH_ANCHOR`). They define the benchmark target
and must not be used for training.

### Measurement functions

1. All 100 anchors yield 1232 forbidden `family:` and `schema:` fingerprints
   for the `ContaminationIndex`. These fingerprints cause `dbt_iterable` and
   `dbt_netsuite` to be refused at ingest (§8). Without the anchor store, a
   workspace has only the embedded family-name deny lists.
2. `corpus/selection.py`
   builds `anchor_profile()` from the store and reports how far the candidate
   corpus deviates from the benchmark's difficulty / connector / size shape.
   Anchors define the comparison profile and are not selection candidates.

### Non-training enforcement

Two code paths enforce the measurement-only contract:

* `corpus/selection.py:248` makes an anchor unselectable: "measurement-only;
  never selectable". Selection cannot put one in a corpus.
* `export/release.py:309` refuses `Origin.ELTBENCH_ANCHOR`. Even
  a hand-planted anchor cannot be shipped in a bundle.

Every TaskIR emitted by the adapter carries
`origin == Origin.ELTBENCH_ANCHOR`, and `save_anchor_store()` refuses to write a
non-anchor into the reference store.

### Data location

`BENCH` (`../ELT-Bench`).

### Verified command

```bash
.venv/bin/elt-taskgen measure-target \
  --workspace /tmp/verify/ws \
  --bench-root ../ELT-Bench
```

Output:

```
imported 100 anchor task(s); contamination index armed with 1232 anchor fingerprint(s)
reference store: /tmp/verify/ws/reference/anchors  (measurement-only; never trainable)
```

`ingest-anchor` is a deprecated alias for `measure-target`. It prints a notice
on stderr and performs the same operation. The alias was renamed because its
old name suggested that ELT-Bench was a task source.

`--db <name>` measures one database instead of all 100. It is useful for a fast
smoke test but does not provide the full contamination index:

```bash
.venv/bin/elt-taskgen measure-target --workspace WS \
  --bench-root .../ELT-Bench --db asana
# imported 1 anchor task(s); contamination index armed with 15 anchor fingerprint(s)
```

One database provides 15 fingerprints instead of the full set of 1232.

### Store location and migration

Since 2026-08, each anchor is stored at
`<workspace>/reference/anchors/eltbench__<db>.json`. The previous location was
`<workspace>/anchors/`, beside `<workspace>/tasks/`.

Both `save_anchor_store()` and `load_anchor_store()` call
`migrate_legacy_anchor_store()` first. It moves any surviving
`<workspace>/anchors/*.json` into `reference/anchors/`, emits a
`DeprecationWarning` naming the files, and removes the old directory once it is
empty. Skipping an old store would omit its contamination fingerprints, so a
legacy file whose destination differs byte-for-byte causes an error.

The migration is atomic at the operation level: it reads every legacy file and
compares every destination before moving anything. Any conflict aborts the
operation, reports all conflicts, and leaves the workspace unchanged. The
earlier implementation moved files one at a time and stopped at the first
conflict. Example output for a legacy workspace:

```
NOTE: 'ingest-anchor' is a deprecated alias for 'measure-target'. ELT-Bench is the GOAL, not a training source.
NOTE: migrated 1 anchor file(s) from the deprecated /tmp/ws/anchors to /tmp/ws/reference/anchors; ...
migrated 1 legacy anchor file(s) into reference/anchors/
imported 1 anchor task(s); contamination index armed with 15 anchor fingerprint(s)
```

### Stored anchor shape

```
task_id     eltbench__asana
family_id   eltbench__asana          cluster_id eltbench__asana
origin      eltbench_anchor          license    CC-BY-SA-4.0
attribution ELT-Bench pinned anchor task 'asana' (measurement-only, never released)
tables      11 (0 relationships)
marts       asana__task, asana__user, asana__project, asana__team, asana__tag
backends    mongodb=3, rest=2, postgres=2, files=2, s3=2
status      draft
```

Across all 100 anchors: 835 source tables, 2 to 64 tables each.

### Independence unit

Each ELT-Bench database has one family, `eltbench__<db>`. This unit is used only
for fingerprinting and profile comparison, not selection.

### Requirements and notes

* Run `measure-target` before source ingest. Without the anchor store, ingest
  seeds only the embedded family-name deny lists. Those lists detect family
  collisions but not schema-level collisions that require all 1232 anchor
  fingerprints.
* The catalog pool key is `eltbench`, not `eltbench_anchor`, even though the
  `Origin` is `eltbench_anchor`. The pool key is the family-id namespace, and
  the adapter has always emitted `eltbench__<db>`; renaming it would move all
  100 family ids and every fingerprint derived from them. A regression test
  pins this identifier.
* Anchors declare no relationships because ELT-Bench does not ship FK metadata,
  so anchor `deps:` fingerprints do not exist. Contamination against anchors is
  carried by `family:` and `schema:` fingerprints.
* `load_anchor_store()` writes when it migrates a legacy store so that the old
  workspace retains its contamination coverage. A read-only legacy workspace
  therefore fails instead of silently omitting the store.

---

## 1. demo — the built-in fixture

### Description

The demo is a hand-authored three-table fixture (`customers`, `orders`,
`order_items`) for exercising the full pipeline without external data or
network access. It is a self-test, not a corpus.

### Data location

The fixture is constructed in code by
`src/elt_taskgen/demo_fixture.py::demo_task()` and has no on-disk source data.

### Test entry point

There is no demo command. The `demo` subcommand was
removed on 2026-08-14 and the parser refuses it with an argparse `invalid
choice` (exit 2). The fixture is exercised by the offline test suite
(`.venv/bin/python -m unittest discover -s tests`), which is the full-system
acceptance test, and by any test that calls `demo_fixture.demo_task()`
directly.

Elsewhere, `--replay-only` forbids live provider calls and fails any stage whose
transcript is missing. Use it for offline reproducibility checks. There is no
`--offline` flag.

### TaskIR shape

```
task_id     demo__customer_summary
family_id   demo__customer_summary   cluster_id demo__customer_summary
origin      demo                     license    CC0-1.0
attribution ELT-taskgen built-in demo fixture
tables      customers, orders, order_items   (2 relationships)
marts       customer_summary
backends    postgres=1, mongodb=1, files=1
populations development, primary, resampled, counterfactual, stress
attack_cases 4
content_hash 4676328cd6a2cc4b788f9b85fc1e8b09a75d8a18d28e8e00e2ec882efdd49316
```

### Independence unit

The fixture has one family, `demo__customer_summary`. It is not a pool and does
not participate in selection.

### Requirements and notes

* The `content_hash` is pinned evidence. It is asserted independently by
  `tests/test_models_round3.py:48` and `tests/test_models_pools.py:46`. If a
  change moves it, two tests fail, and every piece of evidence bound to the demo
  task is invalidated. Do not update the literal only to match new output;
  resolve the change that moved the hash. It was unchanged when this runbook
  was verified.
* The pipeline has **15** ledger stages (`engine.STAGE_ORDER`: intake,
  contamination_pre, generate, reference, author, review, attack, gates,
  gates_extract_load, gates_transform, calibrate, contamination_post, select,
  audit, release). An earlier revision of this document asserted 13, which
  predated the two per-unit gate stages. The ledger is the authority; a literal
  in prose is not.
* The shared acceptance battery has **13** gates (`gates.GATE_NAMES`:
  trusted-solution, determinism, degenerate-zero, required-mutants,
  shortcut-probes, data-sensitivity, info-content, populations-load,
  contamination-clean, dual-build-agreement, referential-integrity,
  declared-scale-reconciliation, mart-key-unique). The per-unit rosters
  (`gates.VARIANT_GATE_NAMES`) hold 15 gates each for `extract_load` and
  `transform`. Earlier revisions said eight, then ten.
* There is no mock council provider. `MockProvider` was deleted. The
  canonical `tests/fixtures/transcripts/` tree is no longer a complete demo
  replay set: it contains only the `independent_implementer` entry whose
  current prompt and system digest the migration can still prove. In
  particular, the stale live `semantic_author` fixture was removed after the
  author's system/behaviour changed. Its byte-preserved original remains under
  `tests/fixtures/transcripts_legacy/semantic_author/`, but it is archival data,
  not replay evidence: `tests/test_transcript_fixture_migration.py` proves that
  neither the live nor legacy store can serve it under the current route.
  `tests/test_repair_budget.py` reads only its response text as static,
  fidelity-green prose for a stub provider; that test does not claim the old
  response answers the current author instructions. A replay miss is a non-zero
  exit, never an invented answer.
* The demo is slow: a single run is ~10-15 minutes of CPU on an unloaded
  machine because it materializes five populations, compiles and executes the
  attack matrix, and runs calibration. It is not a smoke test.
* The removed demo command used block-buffered output when redirected to a
  file, so its log could remain silent for many minutes. Progress was visible
  in `<workspace>/tasks/demo__customer_summary/`, not by tailing the log.
* The former `cmd_demo` resume defect is fixed. Resumability now depends on
  lineage. `cmd_demo` previously compared `existing.model_copy(update={
  "solver_prompt": ""}).content_hash()` to the pristine fixture hash. The
  pristine `solver_prompt` is `""`, so that substitution removed the
  author stage's prose; it did not account for `attack_cases`, which
  the Round-3 promoter grows (`1 proposal(s) promoted` →
  `proposed__population_adversary-02-af67d44a`). Either field alone breaks the
  equality, so every second `demo` run re-registered the task and redid the
  entire pipeline. This caused about 10 minutes of repeated work and the
  `tests/test_cli.py::TestDemoEndToEnd::test_second_run_resumes_without_rework`
  failure. A field allowlist can become stale, and `engine.register()`
  overwrites `task_ir.json`. `cli._demo_task_is_resumable()` now requires a
  well-linked revision chain whose root (`revisions[0]`, revision 1, no parent)
  carries `pristine.content_hash()`.
  The content check that no fixture attack case is missing runs in `cmd_demo`
  on every fresh or resumed run. In one workspace over three consecutive runs,
  run 1 took 567 s, with 20 echoed stage executions and 23 ledger rows spanning
  three identities
  (`4676328c` fixture → `af67d44a` author prose → `d070c572` attack
  promotion). Runs 2 and 3 took 0.0 s, executed no stages, and left the ledger and
  `task_ir.json` byte-identical, banner `resuming registered task
  'demo__customer_summary' (lineage root 4676328cd6a2, current identity
  d070c5726d31)`. Registering a genuinely different task under the demo id
  makes the predicate refuse and print
  `WARNING: ... does not descend from this demo fixture (fixture 4676328cd6a2,
  stored f8268011bc4a) — re-registering from scratch`.
* The measured identity chain is historical, not current replay evidence. An
  earlier revision read `4676328c → 83258fd9 → 3cff97c2`;
  the later recorded run moved that chain to `4676328c → af67d44a →
  d070c572`. Identity 1 remains pinned by two tests, but the latter identities
  depended on an author response recorded under instructions that have since
  changed. The stale live author fixture was therefore removed rather than
  re-keyed under current behavior. Applying the preserved legacy response as
  plain data can reproduce the old `af67d44a…` content hash, but that calculation
  does not certify the response as current and must not be used to populate a
  replay store. The migration test makes this distinction executable; the
  repair-budget test uses the text only behind a stub provider.
* Wall-clock figures differ between measurements: 567 s in the resume
  experiment and 676 s in the fully
  instrumented cold run of `docs/runs/demo.md`. 85% of either is the `attack`
  stage. Treat any single demo timing as a sample, not a constant.
* The repeated attestations in run 1 are required because a report is
  only valid at the content hash it was recorded at, so a hash-moving stage
  forces re-attestation at the new identity. Per-stage executions were
  `contamination_pre` 3, `generate` 3, `reference` 3, `author` 2, `review` 2,
  `intake` 3 (un-echoed), everything else 1. Only `reference` is expensive
  (`freeze_gold` binds gold to `task_content_hash`, so it genuinely rebuilds);
  the rest re-attest in well under a second.

---

## 2. Vacated source number — see §R

This slot previously listed `eltbench_anchor` as source 2, which incorrectly
suggested that ELT-Bench was training material. ELT-Bench instead supplies
contamination fingerprints and the target difficulty distribution. Two code
paths prevent training use (`corpus/selection.py:248`,
`export/release.py:309`). Section R documents it above §1. The command is
`measure-target` (`ingest-anchor` remains a deprecated alias) and the store now
lives under `<workspace>/reference/anchors/`.

Sections 3–7 retain their existing numbers because this document and commit
history cross-reference them.

---

## 3. fivetran (dbt) — vendored Fivetran dbt packages

> Historical run snapshot ([`docs/runs/fivetran.md`](runs/fivetran.md); not
> current):
> ingest works. The pipeline reaches `reference`, where
> `compile_plan_sql` raises `PlanCompilationError` on three consecutive repair
> rounds and the task is rejected. 1560 s, $1.0453. No task produced. Note the
> emitted artifact has `relationships=0` and zero join ops even though the
> package itself contains five `sys_choice` self-joins — the transform in the
> material does not survive into the TaskIR.

### Description

The source contains 16 clean Fivetran dbt packages.
Each is a real analytics-engineering project: declared sources, staging models,
and marts. The adapter cuts the model DAG into candidate tasks, one per
connected mart cluster.

### Data location

`DATA/fivetran/dbt_<family>/`.

### Verified ingest command

```bash
.venv/bin/elt-taskgen ingest-dbt --workspace /tmp/verify/ws --package dbt_servicenow
```

The command performs every step from vendored package to registered task. It
mirrors the package into `<workspace>/dbt_builds/<pkg>/package/`, runs `dbt
deps`, runs `dbt parse` to compile `target/manifest.json`, extracts candidates,
runs contamination checks, and registers the candidates. No hand-built
manifest is required.

Output:

```
mirroring DATA/fivetran/dbt_servicenow -> /tmp/verify/ws/dbt_builds/dbt_servicenow/package
dbt deps  (.../integration_tests)
dbt parse (.../integration_tests)
manifest: .../integration_tests/target/manifest.json
  project=integration_tests  rebuilt  fingerprint=cadddeaebfce989d
skipped cut: trivial cut [...stg_servicenow__task_sla...]: fewer than 2 source tables (no join surface)
  dbt__servicenow__servicenow__activity_summary_d95604da
    family=dbt__servicenow  license=Apache-2.0
    source tables=15  marts=5  relationships=0
    backends: files=4, mongodb=5, postgres=3, rest=1, s3=2
    registered
1 candidate task(s) ingested from dbt_servicenow
```

Useful variants, all verified:

```bash
# report without registering
.venv/bin/elt-taskgen ingest-dbt --workspace WS --package dbt_stackadapt --dry-run
# parse the vendored package directly instead of its integration_tests project
.venv/bin/elt-taskgen ingest-dbt --workspace WS --package dbt_servicenow --project package
# fail if ANY connected cut had to be skipped
.venv/bin/elt-taskgen ingest-dbt --workspace WS --package dbt_servicenow --strict
# legacy: ingest an already-compiled manifest by path
.venv/bin/elt-taskgen ingest-dbt --workspace WS --manifest /path/to/target/manifest.json
```

`--root` changes the source tree, so the catalog's commit no longer attests
the selected bytes. When the catalog has provenance for that record, a custom
root must name its full verified commit explicitly:

```bash
.venv/bin/elt-taskgen ingest-dbt --workspace WS --package dbt_twitter \
  --root /path/to/pinned/fivetran \
  --source-commit b610ac7661d788a5395520487925d9331603d254
```

`--source-upstream` is required only when the catalog record has no upstream
URL. The preflight runs before dbt or dependency work. A cached manifest is
also reusable only when every source-owned byte in its build mirror matches
the selected vendored tree; project-name equality alone is deliberately
insufficient because different releases share that name. A mismatch fails
closed and requires a deliberate `--rebuild-manifest`.

### TaskIR shape

```
task_id     dbt__servicenow__servicenow__activity_summary_d95604da
family_id   dbt__servicenow          cluster_id dbt__servicenow
origin      dbt                      license    Apache-2.0
attribution Fivetran dbt package: dbt_servicenow (https://github.com/fivetran/dbt_servicenow @ 472785a61215)
tables      15 (0 relationships)
marts       servicenow__activity_summary, servicenow__change_request_enhanced,
            servicenow__incident_enhanced, servicenow__problem_enhanced,
            servicenow__user_enhanced
backends    mongodb=5, files=4, postgres=3, s3=2, rest=1
populations ()  -- candidate stage; `generate` materializes them
```

### Independence unit

Each vendored package has one family, `dbt__<family>`. A package may yield more
than one task in that family: `dbt_sage_intacct`
registers both `dbt__sage_intacct__sage_intacct__balance_sheet_217298ce` and
`dbt__sage_intacct__sage_intacct__ap_ar_enhanced_e0637d28`, one per disconnected
mart cluster in the model DAG. Treat the family, not the task, as the unit when
splitting train/test.

### Measured status of all 16 vendored directories

The following table records a full `--dry-run` sweep:

| Package | Result |
|---|---|
| dbt_amazon_ads | 1 task, 11 tables, 7 marts |
| dbt_amazon_selling_partner | no candidate — 2 of 3 marts declare no defensible key (was 1 task, 14 tables, 3 marts) |
| dbt_apple_search_ads | 1 task, 10 tables, 6 marts |
| dbt_fivetran_log | 1 task, 6 tables, 5 marts |
| dbt_reddit_ads | 1 task, 14 tables, 6 marts |
| dbt_sage_intacct | 1 task (4 tables/1 mart); the 2-mart cut is skipped — `__balance_sheet` / `__profit_and_loss` declare no defensible key |
| dbt_salesforce_marketing_cloud | 1 task, 8 tables, 5 marts |
| dbt_servicenow | 1 task, 15 tables, 5 marts |
| dbt_snapchat_ads | 1 task, 9 tables, 5 marts |
| dbt_stackadapt | 1 task, 8 tables, 5 marts |
| dbt_twitter | 1 task, 10 tables, 6 marts (family `dbt__twitter_ads`) |
| dbt_iterable | REFUSED — `family:iterable` collides with `spider2_dbt` |
| dbt_netsuite | REFUSED — `family:netsuite` collides with `spider2_dbt` (2 candidates) |
| dbt_aws_cloud_cost | no candidate — single-source package, no join surface |
| dbt_ga4_export | no candidate — single-source package, no join surface |
| dbt_mixpanel | no candidate — single-source package, no join surface |

Two former placeholders, `dbt_stripe` and `dbt_zendesk`, were removed from disk
on 2026-08-14 along with their catalog carve-out. They remain unreachable:
`family:stripe` and `family:zendesk` are Spider2-DBT fingerprints, so either one
is refused at `contamination_pre` no matter which pool it arrives through.

Ten of the 16 directories yield 10 ingestible candidate tasks. Three are
structurally unusable (single-source, no join surface), two are contaminated, and two cuts
(one whole package) are refused for having no defensible mart key — see the
`key_columns` note below.

### Requirements and notes

* The first build of a package requires network access. `dbt deps` fetches
  `fivetran_utils` and `dbt_utils` from `hub.getdbt.com`. Later builds reuse the
  mirrored `dbt_packages/`; `--no-deps` skips deps entirely. A deps failure with
  no cached packages fails closed with that exact explanation.
* dbt is not in this repository's virtual environment. The builder shells out to
  `elt-training-data/.venv/bin/python3 -m dbt.cli.main` (dbt-core 1.10.22,
  Python 3.9.6 — both verified). That venv's `dbt` console script is unusable:
  its shebang is `#!/Users/weslu/Desktop/elt-training-data/.venv/bin/python3`,
  a path that does not exist on this machine. Invoke dbt as a module rather
  than as `dbt`. `--dbt-python` overrides the interpreter.
* `python3 -m dbt.cli.main` prints two harmless warnings on every invocation (a
  `runpy` RuntimeWarning about `dbt.cli.main` already being in `sys.modules`,
  and a urllib3/LibreSSL `NotOpenSSLWarning`). They are noise, not failure.
* `manifest.json` is a build intermediate, not a task artifact: dbt stamps
  `generated_at` and `invocation_id` into it, so it is not clock-free.
  Idempotence is asserted on the clock-free `manifest_fingerprint()` and on the
  TaskIR content hashes instead.
* The family comes from the dbt project name, not the directory name. The
  directory `dbt_twitter` produces family `dbt__twitter_ads`.
* `--manifest <path>` does not receive catalog identity: ingesting
  the same servicenow manifest by path yields family
  `dbt__servicenow_integration_tests` and license `unspecified`. `--package` is
  the path that gets the real family, license and attribution.
* `<workspace>/dbt_builds/<pkg>/package/integration_tests/dbt_packages/<pkg>` is
  a symlink back to its own ancestor, created when `dbt deps` resolves the package's
  `local: ../` dependency. Disk usage is fine (~10 MB/package, `find` reports
  max depth 8), but a symlink-following recursive walk follows the cycle.
  `glob.glob('**', recursive=True)` returned paths with
  `dbt_packages/servicenow/integration_tests/` repeated ~20 times. Anything that
  walks a workspace (backup, archive with `tar -h`, a cleanup script) should use
  `find` semantics, which do not follow symlinks by default.
* Mart `key_columns` are derived from ranked evidence. A mart without evidence
  is skipped. This replaced the old
  `col_names[0]` fallback, which measured out as `_fivetran_synced` (a Fivetran
  load timestamp) on 12 of 84 real terminal marts and as an arbitrary column on
  69 of 84. `adapters/dbt.py::mart_key_columns` now tries, in order:
  `dbt_utils.unique_combination_of_columns` (composite) → `unique` tests → the
  model's outermost `GROUP BY` → the `not_null` test set (Fivetran declares
  these on exactly the grain columns) → the `<entity>_id` implied by the model
  name → an identifier carried from an upstream declared key. Two invariants
  gate every rule: no load-metadata column (`is_load_metadata_column`:
  `_fivetran*`, `dbt_run_date`, `_file`, …) may be a key, and at least one
  column must remain outside the key. `dbt_servicenow` before → after:

  ```
  servicenow__activity_summary        ['average_minutes_open_to_close'] -> ['impact','priority','source_relation','task_state','task_updated_date','urgency']  (GROUP BY grain)
  servicenow__change_request_enhanced ['_fivetran_synced']              -> ['change_request_id']  (<entity>_id)
  servicenow__incident_enhanced       ['_fivetran_synced']              -> ['incident_id']        (<entity>_id)
  servicenow__problem_enhanced        ['_fivetran_synced']              -> ['problem_id']         (<entity>_id)
  servicenow__user_enhanced           ['accumulated_roles']             -> ['sys_user_id']        (upstream declared key)
  ```

  The rule that produced a key is recorded in `MartSpec.plan.notes`. Note this
  was never a grading defect — `upstream_eval.sort_rows` and
  `export.evaluation_sql` append every remaining column themselves, so the
  total order never depended on `key_columns` — but `verification/gates.py`
  uses `key_columns` to partition mart columns, and `answer_key/sort_key.json`
  plus every solver-facing view state it as the mart's grain.
* This validation reduced the ingestible candidate count from 12 to 10.
  `dbt_amazon_selling_partner` (no key evidence for `__item_inventory` /
  `__orders`) and one of `dbt_sage_intacct`'s two cuts (`__balance_sheet` /
  `__profit_and_loss`, genuinely keyless amount-by-account facts) are now
  refused with a named reason rather than keyed arbitrarily. The other
  `dbt_sage_intacct` cut (`__ap_ar_enhanced`) still registers.
* `manifest_fingerprint()` changed (dbt_servicenow: `7aacb8831f03e754` →
  `cadddeaebfce989d`). It hashes the parsed `CandidateSpec`, which now also
  carries `raw_code` (for the GROUP BY rule) and composite-test columns — both
  of which can move a TaskIR, so including them is correct. Expect one extra
  rebuild per package on the first run after this change.
* `relationships=0` for most Fivetran packages: they declare `relationships`
  tests on models, and the adapter only lifts source-to-source ones.

---

## 4. synsql — SynSQL-2.5M schemas

> Historical run snapshot ([`docs/runs/synsql.md`](runs/synsql.md); not
> current): ingest
> works. The furthest ledger stage is `review`, which reports 2 fatal findings
> across 3 repair rounds. `attack` and `gates` were run out of band on this task
> and produced a full attack matrix plus a 9-PASS / 1-FAIL
> gate battery (the one failure is a gate for a stage that never ran); `export`
> also succeeded. Despite those out-of-band results, the task is rejected and
> no task was produced.
> ~10 800 s and ~$2.33, of which a large share is metrology attempts and a
> full-suite re-run, so do not read that wall time as pipeline cost.

### Description

SynSQL-2.5M is a large corpus of synthetic relational schemas. Only its schema
data is ingestible.

### Data location

`DATA/SynSQL-2.5M/` contains `tables.json` (306 MB, schemas) and `data.json`
(9.3 GB, answer-bearing).

### Verified ingest command

```bash
.venv/bin/elt-taskgen ingest-synsql \
  --workspace /tmp/verify/ws \
  --tables $ELT_TASKGEN_DATA_ROOT/curation/packages_raw/SynSQL-2.5M/tables.json \
  --db-id fisheries_data_and_management
```

Output:

```
registered synsql__fisheries_data_and_management__captains_landings_summary  (family synsql__fisheries_data_and_management)
```

### TaskIR shape

```
task_id     synsql__fisheries_data_and_management__captains_landings_summary
family_id   synsql__fisheries_data_and_management
cluster_id  synsql__cluster_cdc5ca42e3ad
origin      synsql                   license    Apache-2.0
attribution SynSQL-2.5M schema (db_id: fisheries_data_and_management)
tables      16 (20 relationships)
marts       captains_landings_summary
backends    files=4, mongodb=4, rest=4, s3=3, postgres=1
populations ()  -- candidate stage
```

### Independence unit

Each `db_id` has one family, `synsql__<db_id>`. The `cluster_id` is
shape-derived (`synsql__cluster_<hash>`) and is not equal to the family. Two
different `db_id`s with the same normalized shape share a cluster.

### Requirements and notes

* `data.json` is answer-bearing and is not an ingest input. It is used only
  by the adapter's leak guard, via the optional `--data` flag. Passing it makes
  the ingest slower but runs the answer-leak check; omitting it skips that guard.
* Pick a `db_id` with real FK structure. Much of SynSQL is a handful of tables
  with no join surface. `fisheries_data_and_management` (16 tables / 20 FKs) is
  a verified example. `tables.json` holds 16,583 distinct `db_id`s`, but this
  pool has no `--list` command; supply the `db_id` directly.
* Candidate stage: `populations` is empty. `generate` fills it.
* `ingest-synsql` now runs the contamination pre-check (it was the one
  path that did not — see §8). The gate is
  `adapters/synsql.py::assert_uncontaminated`, called before the optional
  `--data` answer-leak scan and before the engine is opened, so a refused
  candidate costs neither a workspace entry nor a pass over the 9 GB
  `data.json`. Measured live exposure remains nil: none of the 16,583 `db_id`s
  collide with any deny-list family.

---

## 5. dlt — vendored dlt verified-source connectors

> Inventory status (2026-08-14): all 29 sources in dlt's official verified
> catalog are vendored at commit `3957506…`. Twelve have committed task
> manifests, six can structurally form a TaskIR, and only Personio, Pipedrive,
> and Workable are admitted by the source catalog. The older run narrative in
> [`docs/runs/dlt.md`](runs/dlt.md) is historical and must not be used as the
> current pool census.

“Verified Sources” is dlt's upstream catalog name. It is not an ELT-taskgen
bundle-verification result or a real-runtime certification claim.

### Description

The source contains 29 vendored dlt connectors at one pinned commit. Each is a
`@dlt.resource` graph describing a real extraction topology (endpoints,
pagination, primary keys, incremental cursors, parent/child transformers).
`tools/extract_dlt_manifest.py` reads the connector by AST without importing it
and writes curator-reviewable YAML under `config/dlt_connectors/`. The adapter
reads that YAML. Contaminated and transport-only connectors are
kept as auditable raw source material but never receive an ingestible task
manifest.

### Data location

The connector sources are at `DATA/dlt/dlt_<name>/source/` (pinned commit
`3957506893a7da821dbcc6acd51c7ca4475d1f53`). Extracted manifests are committed
at `config/dlt_connectors/<name>.yaml`.

### Verified ingest command

```bash
.venv/bin/elt-taskgen ingest-dlt --workspace /tmp/verify/ws --connector workable
```

Output:

```
registered dlt__workable  (family dlt__workable, 17 source tables [1 rest / 5 files],
9 FK edge(s), marts ['jobs_stages_top', 'dim_jobs'], license Apache-2.0)
```

Discovery and bulk:

```bash
.venv/bin/elt-taskgen ingest-dlt --workspace WS --list   # 12 task manifests + resource counts
.venv/bin/elt-taskgen ingest-dlt --workspace WS --all    # 3 catalog-admitted connectors
```

Audit all 29 raw connectors independently of task admission, and re-extract
task manifests only when the pinned checkout moves. Without `--out`, the
extractor overwrites its target, so use a scratch directory first:

```bash
.venv/bin/python tools/audit_dlt_inventory.py --out config/dlt_source_inventory.yaml
.venv/bin/python tools/extract_dlt_manifest.py --all --out /tmp/dlt_regen
.venv/bin/python tools/extract_dlt_manifest.py --connector dlt_freshdesk \
  --include-excluded --out /tmp/dlt_regen
```

The extractor and inventory audit are deterministic. A repeated audit of
the same pinned tree is byte-identical. Curator-authored column blocks survive
manifest regeneration; an orphaned block is a hard failure:

```bash
.venv/bin/python tools/audit_dlt_inventory.py --out /tmp/dlt_inventory_a.yaml
.venv/bin/python tools/audit_dlt_inventory.py --out /tmp/dlt_inventory_b.yaml
cmp /tmp/dlt_inventory_a.yaml /tmp/dlt_inventory_b.yaml
```

### TaskIR shape

```
task_id     dlt__workable
family_id   dlt__workable            cluster_id dlt__workable
origin      dlt                      license    Apache-2.0
attribution dlt verified source: dlt_workable (https://github.com/dlt-hub/verified-sources/... @ 3957506893a7)
tables      17 (9 relationships)     -- 8 base resources + 9 parent-dependent details
marts       jobs_stages_top, dim_jobs
backends    files=5, mongodb=4, postgres=4, rest=1, s3=3
populations development, primary, resampled, counterfactual, stress
```

### Independence unit

Each connector has one family, `dlt__<connector>`.

### Historical task-manifest subset

The following 12-connector table records the extractor's view from before
`config/sources.yaml` existed. It is retained for its recorded shapes; it is
not the current admission census, and its
`workable` row disagrees with the ingest output quoted above (the measured row is
`17 (1 rest / 5 files) | 9 FK edges | 2 marts`). The parenthetical counts only
the REST and FILES backends, so it sums to the table count only for connectors
that use no other backend — `workable`'s remaining 11 tables are mongodb,
postgres and s3.

| Connector | tables (rest/files) | FK edges | marts |
|---|---|---|---|
| chess | 3 (0/3) | 0 | 1 |
| freshdesk | 6 (6/0) | 0 | 3 |
| google_analytics | 2 (0/2) | 0 | 1 |
| matomo | 2 (1/1) | 1 | 3 |
| mux | 2 (1/1) | 0 | 1 |
| notion | 1 (0/1) | 0 | 1 |
| personio | 9 (9/0) | 2 | 3 |
| pipedrive | 18 (17/1) | 2 | 3 |
| slack | 4 (3/1) | 0 | 2 |
| workable | 17 (1 rest / 5 files; + 4 mongodb, 4 postgres, 3 s3) | 9 | 2 |
| airtable | SKIPPED — no static resources | | |
| strapi | SKIPPED — no static resources | | |

Three of the 12 are currently admitted: `personio`, `pipedrive`, and `workable`.
The other nine carry a task manifest but are refused by `config/sources.yaml`'s
`excluded` list (ingest exits 2 naming the reason) — which is why `--list`
prints 12 and `--all` ingests 3.

### Requirements and notes

* `airtable` and `strapi` name their resources at runtime (Airtable from its
  metadata API; Strapi from a required `endpoints: List[str]` argument), so the
  extractor records `endpoints: []` plus the unresolved reason and the loader
  raises a dedicated `NoStaticResources`. A curator can make them ingestible by
  naming the collections in the YAML. The extractor does not invent unresolved
  collection names.
* The backend split is inferred and requires curator review. An endpoint whose
  resource body shows pagination evidence gets `Backend.REST`; otherwise
  `Backend.FILES` (a one-shot snapshot dump). That makes chess,
  google_analytics and notion all-FILES. Recorded per endpoint as `paginated:`
  in the manifest.
* Transformer children get a synthesized FK column `_<parent>_id` (dlt links
  transformer output to its parent by internal row id, not a business key),
  marked as synthesized in the `ColumnSpec` description. Parents with a
  composite or absent primary key get no FK edge at all — the dependency
  survives only in `BackendAssignment.options['parent']`.
* Two heuristics deliberately do not fabricate, and say so in each endpoint's
  `notes`: a `write_disposition='merge'` with no statically resolvable key is
  written as `append`; a conditional disposition is written as the
  default-argument branch with the alternative named.
* Known extractor limits, each surfacing as an incomplete manifest rather than a
  guess: `async def` resources are not walked; declarative
  `rest_api_resources(config)` sources are not decoded (so pipedrive's v2 source
  contributes 0 endpoints); runtime-named resources land in `unresolved`.

---

## 6. schemapile — the permissive SchemaPile subset

> Historical run snapshot ([`docs/runs/schemapile.md`](runs/schemapile.md);
> not current):
> ingest works, including the per-record licensing refusal and the union-find
> family identity. The pipeline stops at `generate`, fatal: the
> counterfactual population is still the generation-module placeholder. 98 s,
> $0.1942. No task produced.

### Description

SchemaPile provides 22,989 scraped SQL schemas with permissive licenses, keyed
by source filename. Selection uses a prebuilt index, so ingest does not scan the
327 MB corpus.

### Data location

`DATA/schemapile/schemapile-perm.json` (327 MB, one
JSON object keyed by `"<file>.sql"`; each record carries
`INFO.{URL,LICENSE,PERMISSIVE}` and `TABLES`).

### Verified ingest commands

Build the index first. On the 327 MB corpus, this took 5.0 s wall time and
191 MB peak RSS (`/usr/bin/time -l`). The streaming decoder avoids loading the
complete corpus with `json.load`, but the accumulated index (22,989 record
metrics plus the cluster union-find) remains in memory. The measured peak is
therefore about 191 MB, not the approximately 45 MB stated in the build notes:

```bash
.venv/bin/elt-taskgen-schemapile-index --out /tmp/verify/schemapile_index.json --top 15
```

Then list candidates and ingest one:

```bash
.venv/bin/elt-taskgen ingest-schemapile --workspace WS --index /tmp/verify/schemapile_index.json --list --limit 6

.venv/bin/elt-taskgen ingest-schemapile \
  --workspace /tmp/verify/ws \
  --index /tmp/verify/schemapile_index.json \
  --cluster github_com_kykrueger_openbis_c106a4e57b99
```

Output:

```
registered schemapile__github_com_kykrueger_openbis_c106a4e57b99__045413_schema_047_sql
  family     schemapile__github_com_kykrueger_openbis_c106a4e57b99
  license    APACHE-2.0
  attribution SchemaPile (permissive subset): 045413_schema-047.sql <https://github.com/kykrueger/openbis/...> [APACHE-2.0]
  schema     40 table(s), 102 relationship(s), 1 mart(s)
```

`--key <record>` ingests an exact record instead of a cluster representative.

### Corpus totals

These values come from a complete scan:

```
22,989 records   198,756 tables   101,221 resolvable FK edges
ALL 22,989 are PERMISSIVE with a non-blank LICENSE
  MIT 14,673 | Apache-2.0 6,488 | BSD-3 970 | Unlicense 268 | CC0 183 | BSD-2 114 | ECL 98 | ISC 78 | ...
14,597 distinct repos -> 14,305 clusters (2,794 hold >1 record; largest holds 281)
5,742 records (25%) clear the default relational filter
4,414 clusters have an ingestible representative
```

### TaskIR shape

```
task_id     schemapile__github_com_kykrueger_openbis_c106a4e57b99__045413_schema_047_sql
family_id   schemapile__github_com_kykrueger_openbis_c106a4e57b99
cluster_id  == family_id   (for this pool the component IS the family)
origin      schemapile               license    APACHE-2.0   (per record)
tables      40 (102 relationships)
marts       experiments_samples_summary
backends    rest=13, mongodb=9, postgres=8, files=6, s3=4
populations development, primary, resampled, counterfactual, stress  (SYNTHETIC)
content_hash e59e1a76f20fc9e276b280a5fb1b28f6b59dbd1f07fe153df98f127e2b90d0ba
```

### Independence unit

Each cluster has one family. A cluster is a union-find component over two
relations per record: `repo:<host+owner+repo
from INFO.URL>` and `shape:<normalized-DDL fingerprint>`. Records sharing
either are one family. A repository's 139 migration files are one family, and
a schema vendored into a fork is placed in the same family as its original. The
cluster digest is
keyed on the repository set, not the member set, so a new migration file landing
in a known repo does not move an existing family id.

### Requirements and notes

* License is checked per record. `config/sources.yaml` declares
  `license_per_record: true` for this pool, so `catalog.selection()` refuses
  to build a selection unless the caller passes the record's own `INFO.LICENSE`.
  A record with a null LICENSE must be skipped, never ingested under a pool
  default. `assert_usable_license` is the single gate.
* Populations are synthetic. SchemaPile's per-column sample values (≤3
  scraped literals) are deliberately ignored — third-party data of unknown
  provenance. Rows are generated from declared types/nullability/keys/FKs, with
  `SYNTHETIC_POPULATION_POLICY` as the first condition of each population.
* The relational filter defaults (`min_tables=4, max_tables=40, min_columns=12,
  min_foreign_keys=3, min_linked_table_pairs=2, min_tables_with_pk=2`) are
  deliberately stricter than SchemaPile's ~4-table median, so a single migration
  fragment cannot become a task; `max_tables` rejects 200-table ERP dumps. All
  six are CLI flags. `--list` recounts against whatever thresholds are actually
  in force, so overridden flags never print the index's stored numbers.
* A join surface is required by the adapter, independent of the
  filter flags: a record with 0 resolvable FK edges raises
  `RelationalFilterError` even with `--min-foreign-keys 0`.
* Identity is recomputed from the record rather than read from the index.
  Editing a `cluster_id` in the index JSON does not change the emitted
  `family_id`; the adapter re-derives it from the repository set. This fails
  closed and prevents re-keying a family by editing the index.
* The index (`runs/schemapile/index.json`, 15 MB) is gitignored
  derived data. Rebuild it in ~5 s with the tools command above.
* `tools/` is a namespace package with no `__init__.py`; the shipped CLI does
  not depend on it. The index models and `load_index` live in the adapter.

---

## 7. wikidbs — WikiDBs Wikidata-derived databases

> Historical run snapshot ([`docs/runs/wikidbs.md`](runs/wikidbs.md); not
> current): ingest
> works with real rows, with 553 provenance strings quarantined and nothing
> leaked. The pipeline stops at `reference` with `PlanCompilationError` on
> three repair rounds in about 2 seconds, without reaching a model
> call, so $0.00. No task produced.

### Description

WikiDBs contains 213 GB of Wikidata-derived relational databases, each with a
schema and populated CSV rows. This is the only pool whose TaskIRs contain real
rather than generated data.

### Data location

`DATA/WikiDBs/part-N/"<NNNNN> <db_name>"/`; each
directory holds `schema.json`, `tables/*.csv` (real rows),
`tables_with_item_ids/`, and `schema.pdf`. The independence instrument is
`DATA/schema_pools/wikidbgraph/graph_artifacts/` (8,929,097 similarity edges at
threshold 0.94 over 100k nodes).

### Verified ingest commands

Discovery visits a bounded window of one part and does not scan the full 213 GB:

```bash
.venv/bin/elt-taskgen ingest-wikidbs --workspace WS --list --part 0 --scan 120 --limit 8
```

```
  node   component  size  tabs  fks   rows  directory
    50      c00050     1     9   15    248  00050 UNIVERSITY_HUMANITIES_ECONOMICS_LODZ_STAFF
    58      c00058     1     8   14    199  00058 HansAbichFilmography
    22      c00022    11     9   14    843  00022 Chromo_Domain_Superfamily_Db
    ...
```

Ingest one:

```bash
.venv/bin/elt-taskgen ingest-wikidbs \
  --workspace /tmp/verify/ws \
  --db-dir "$ELT_TASKGEN_DATA_ROOT/curation/packages_raw/WikiDBs/part-0/00050 UNIVERSITY_HUMANITIES_ECONOMICS_LODZ_STAFF"
```

Output:

```
registered wikidbs__c00050__00050_university_humanities_economics_lodz_staff
  (family wikidbs__c00050, 9 tables, 14 link(s), 248 real rows in the primary
   population, policy 'provided-rows', license CC-BY-4.0)
```

The directory name contains a space and must be quoted.

The `fks` column in `--list` and the `link(s)` count after ingest are different
quantities and will not always agree (node 50 lists `fks 15` but ingests as
`14 link(s)`): `--list` counts foreign keys declared in `schema.json`, while the
TaskIR keeps only the relationships that resolve to a table present in the same
database. Use the ingest count for the TaskIR.

The family map is committed (`config/wikidbs_family_map.csv.gz`), so ingest
requires no additional map configuration. To inspect or rebuild it:

```bash
.venv/bin/python tools/wikidbs_family_map.py verify --map config/wikidbs_family_map.csv.gz
.venv/bin/python tools/wikidbs_family_map.py lookup --node 50     # -> node 00050 -> c00050 (size 1)
.venv/bin/python tools/wikidbs_family_map.py build                # rebuilds from the 273MB edge CSV
```

`verify` and `lookup` were run here; `build` was not. It re-derives the map
from the 8.9M-edge CSV and rewrites the committed artifact, which would move
family ids if the threshold or the edge file changed. Run it only for an
intentional map recomputation, then run `verify` on the result.

`verify` cross-checks the map against the upstream analysis report:

```
100000 nodes, 71235 components (34874 connected, 65126 singletons)
connected_components 6109 == report_components 6109
largest_component   10703 == report_largest_component 10703
edge_rows         8929097
```

### TaskIR shape

```
task_id     wikidbs__c00050__00050_university_humanities_economics_lodz_staff
family_id   wikidbs__c00050          cluster_id wikidbs__cluster_f50c63837953
origin      wikidbs                  license    CC-BY-4.0
attribution WikiDBs (Wikidata-derived relational databases): 00050 UNIVERSITY_HUMANITIES_ECONOMICS_LODZ_STAFF
tables      9 (14 relationships)
marts       university_information_academic_staff_profiles_summary
backends    postgres=3, files=2, rest=2, s3=1, mongodb=1
populations development, primary, resampled, counterfactual, stress
            all on the 'provided-rows' policy; 248 REAL rows in primary
```

### Independence unit

Each WikiDBGraph connected component at similarity threshold 0.94 has one
family, `wikidbs__c<NNNNN>`, where `NNNNN` is the
lowest-numbered node in the component. Two databases from one component are one
family — which is why `--list` prints at most one candidate per component and
shows the component `size`. Of 100,000 nodes: 6,109 connected components plus
65,126 singletons = 71,235 independent families available.

### Requirements and notes

* The `family_id` segment must be identity under `slugify_family` (`cNNNNN` or
  `unmapped`); if it is not, the adapter fails closed rather than silently
  renaming the family namespace.
* Check for `wikidbs__unmapped`. When the family map is absent, or the
  on-disk node-id correspondence does not verify, `family_segment_for` returns
  the conservative `FALLBACK_FAMILY_SEGMENT = "unmapped"` with the reason
  recorded; it never guesses a component. As a result, a bulk selection run
  with a missing map would put every
  database into the single family `wikidbs__unmapped`, eliminating independence
  between those databases. Assert on the returned family ids and keep
  `config/wikidbs_family_map.csv.gz` in place. The component id is `'c'` plus
  the zero-padded minimum member node id
  (`tools/wikidbs_family_map.py:262`).
* The pool is 213 GB. Never write a command that walks it. `--list` is bounded
  by `--part` / `--start` / `--scan` for exactly this reason.
* Ingest is bounded by envelopes (table count, and a total data-cell cap). A
  database that is too large is refused with the cell count and a pointer to
  `--list` — pick a smaller one, do not raise the cap casually.
* `--no-verify-nodes` skips the on-disk node-id correspondence check (5 part
  listings). Use it for a database copied outside the pool root, not for an
  ingest from the source pool.
* `slugify_family` is not injective and WikiDBs directory names repeat
  across `part-N`. Anything selecting many databases at once must check for
  `task_id` collisions itself.

---

## 8. Contamination enforcement

Every ingest command runs
`ContaminationIndex.check_pre` before registering, seeding the embedded deny
lists (`eltbench` 100 families, `spider2_dbt` 16, `ade_bench` 4) when the index
is unarmed. `family:`, `schema:`, `shape:`, `sql:`,
`fixture:` and `data:` matches are fatal and cause refusal; `schema-table:`,
`shape-table:`, `deps:` and `text:` matches are borderline (human audit queue).

`schema:` is an exact hash of the sorted (table, column, TYPE) shape of a whole
schema and `schema-table:` the same per table; `shape:` / `shape-table:` are the
type-blind pair, hashing sorted, normalized column names only. The type-blind
pair survives retyping and is required to activate the index. A store holding
only typed hashes grades `NAME_ONLY`, not `ARMED`, and the `release` stage
requires `ARMED` unconditionally.

The following tamper tests fed each new adapter a candidate engineered to
collide with a real anchor. The workspace contained all 100 ELT-Bench anchors
and 1232 fingerprints:

| Pool | Tamper | Result |
|---|---|---|
| dbt | real package `dbt_iterable` | `contamination FATAL: family/spider2_dbt: family:iterable` → REFUSED, exit 2 |
| dbt | real package `dbt_netsuite` | `family:netsuite` → both candidates REFUSED, exit 2 |
| dlt | manifest `connector:` renamed to `asana` | `family/eltbench: family:asana` → SKIPPED, exit 2 |
| wikidbs | `schema.json` `database_name` set to `asana` | `family/eltbench: family:asana` → REFUSED, exit 2 |
| schemapile | synthetic record cloning `eltbench__zuora`'s 19 tables exactly | `schema:3b0baa1b…` → REFUSED, exit 2 (was exit 1 when this run was recorded; normalized since), plus 10 borderline `schema-table:` hits |
| synsql | real `tables.json` entry with `db_id` set to `asana` | `contamination FATAL [family/eltbench] fingerprint family:asana present in corpus 'eltbench'` → `REFUSED: 1 fatal contamination collision(s) — synsql__asana__… was NOT registered`, exit 2, no `tasks/` directory and no engine ever opened |
| synsql | untouched neighbouring `db_id` (control) | `registered synsql__embedded_system_partition_management__…`, exit 0 |

In the SchemaPile case, the family name did not collide. The whole-schema
fingerprint alone matched an anchor and caused refusal. SchemaPile's family
space is structurally unlikely to produce a bare `family:` collision (family ids are
`schemapile__github_com_<owner>_<repo>_<digest>`), so schema-level detection is
what protects that pool.

### Contamination-refusal exit code

All ingest commands now return exit 2 for contamination refusal. `ingest-schemapile`
used to refuse with exit 1 and print its own message dialect
(`contamination[FATAL] schema: …`), so a wrapper testing `if [ $? -eq 2 ]` —
the convention `ingest-dbt`, `ingest-dlt`, `ingest-wikidbs` and `ingest-synsql`
already followed — misread a refusal from that one pool. It now returns 2
and prints the shared shape:

```
  contamination FATAL [<kind>/<against>] <detail>
REFUSED: <n> fatal contamination collision(s) — <task_id> was NOT registered
```

The full contract is in the `cli.py` module docstring (INGEST EXIT-CODE
CONTRACT) and in `elt-taskgen ingest-schemapile --help` /
`ingest-wikidbs --help`: 0 success, 1 empty result (the query was fine
and returned nothing — only `ingest-wikidbs --list` over an empty scan window;
widen `--scan`/`--start`), 2 refusal or fail-closed error (nothing entered
the workspace). Exit 1 is deliberately not flattened into 2: it reports an
empty search window, not a refusal. Treating both as exit 2 would incorrectly
report an empty search as contamination.

### `ingest-synsql` pre-check

`cmd_ingest_synsql` was previously the only ingest path that registered without
calling `check_pre` — it went straight from `to_task_ir` to `engine.register`,
so a contaminated SynSQL candidate was caught only later, by the engine's
stage-2 `contamination_pre`.

It now follows the same gate as every other pool. The gate is in the adapter
(`adapters/synsql.py::assert_uncontaminated`, mirroring
`adapters/dlt.py`) rather than only in the CLI, because `to_task_ir` is also
called programmatically by tests and tools that hand the result straight to
`Engine.register`. The CLI owns the printing and the exit code only; the
refusal wording and exit 2 copy `cmd_ingest_wikidbs` verbatim. Note the
adapter-level gate is a helper that a programmatic caller must call, as with
dlt. `Engine.register` itself remains ungated; stage-2 `contamination_pre`
remains the fallback check for callers that skip the helper.

Measured live exposure was and remains zero: all 16,583 `db_id`s in
`tables.json` were scanned against the three deny lists (exact and
`normalize_name`-normalized), and none collide. This was an ingest-boundary
consistency and fail-closed defect, not a live contamination exposure. The
tamper rows above therefore use a manually renamed `db_id`.

Reproduce the scan:

```bash
.venv/bin/python - "$DATA/SynSQL-2.5M/tables.json" <<'PY'
import sys, re
from elt_taskgen.verification.contamination import (
    ELTBENCH_FAMILIES, SPIDER2_DBT_FAMILIES, ADE_BENCH_FAMILIES, normalize_name)
deny = {normalize_name(x) for x in
        set(ELTBENCH_FAMILIES)|set(SPIDER2_DBT_FAMILIES)|set(ADE_BENCH_FAMILIES)}
pat = re.compile(rb'"db_id"\s*:\s*"([^"]+)"'); seen=set()
with open(sys.argv[1],'rb') as f:
    while chunk := f.read(1<<22):
        for m in pat.finditer(chunk): seen.add(m.group(1).decode())
print(len(seen), sorted(d for d in seen if normalize_name(d) in deny))
PY
```

This scan establishes the zero-exposure result and should be repeated whenever
the deny lists grow.

---

## 9. Vendored-tree write protection

Verified by snapshotting before and after a session that ran every ingest —
including a full `dbt deps` + `dbt parse` build of all 18 vendored dbt
directories, three WikiDBs ingests, two SchemaPile index rebuilds and the
then-current 12-manifest dlt regeneration (historical pre-expansion run):

* `DATA/fivetran` + `DATA/dlt` — 1,997 entries of `path|size|mtime`, identical.
* `DATA/schemapile/schemapile-perm.json` — sha256
  `05173df706394dbbee2c69d4a6c2c8a40bc05b0fdbd944da7587cc0bdbb962d0`, identical.
* `DATA/SynSQL-2.5M/tables.json` — sha256
  `68158df82b0f89f3330e42bd7d56088c1428360d51b9c235c0877ea20306da9c`, identical.
* The WikiDBs database that was ingested — 20 files, identical.
* Build residue (`target/`, `dbt_packages/`, `logs/`, `package-lock.yml`)
  anywhere under `DATA/fivetran`: 0 matches.

The dbt builder is the only component that could write there, and it does not:
it mirrors the package into `<workspace>/dbt_builds/<pkg>/package/` and ends
every build with `assert_vendored_untouched()` (sha256 snapshot before/after).

Reproduce with:

```bash
R=$ELT_TASKGEN_DATA_ROOT/curation/packages_raw
find "$R/fivetran" "$R/dlt" -print0 | xargs -0 stat -f '%N|%z|%m' | sort > /tmp/before.txt
# ... run ingests ...
find "$R/fivetran" "$R/dlt" -print0 | xargs -0 stat -f '%N|%z|%m' | sort > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt
find "$R/fivetran" \( -name target -o -name dbt_packages -o -name logs -o -name package-lock.yml \) | wc -l
```

---

## 10. Typed five-source intake

The production coordinator accepts two backward-compatible contracts.
`five-source-ingest-v1` consumes exactly one versioned entry for each of `dbt`,
`dlt`, `synsql`, `schemapile`, and `wikidbs`. `five-source-ingest-v2` is the
exact-count batch form: it consumes a non-empty list for every one of those five
keys. Both complete every local hash/build/contamination preflight before
opening the Engine and never create a provider or make a model/API call. A dry
run is workspace-free; either manifest can be handed directly to `pipeline` so
intake occurs before roster discovery and provider construction:

```bash
WS=/tmp/verify/ws
MANIFEST=/secure/run/five-source-ingest.yaml

.venv/bin/elt-taskgen ingest-five --workspace "$WS" \
  --manifest "$MANIFEST" --dry-run
.venv/bin/elt-taskgen pipeline --workspace "$WS" \
  --ingest-manifest "$MANIFEST" --workers 4
```

The command names and flags are identical for v1 and v2; `ingest-five` names
the five source types, not a limit of five TaskIRs. Use a copied
`config/five_source_ingest.example.yaml` for v1. Relative generated artifacts
live beside that copy, `package://` addresses wheel resources, and `pool://`
addresses the selected catalog root. WikiDBs verification of all five part
listings is mandatory here. If publication is interrupted, rerun the same
manifest: registrations and per-task provenance are idempotent, and the
full-manifest completion receipt is written only after the entire exact roster
succeeds.

For v2, each `sources.<pool>` list must contain at least one full entry and be
sorted by `expected_task_id`. `expected_task_count` must exactly equal the sum
of the five list lengths, all expected and built task ids must be unique, and
`require_unique_independence_units` must be the literal `true`. After building,
the coordinator also requires every TaskIR's `family_id` and `cluster_id` to be
unique across the complete batch. This is the same independence boundary used
by corpus selection and prevents an apparently exact batch from shrinking
during family/cluster deduplication. V2 additionally requires a `generator` pin
calculated by `elt_taskgen.ingest_manifest.current_generator_pin()` and the
literal `require_embedded_provenance: true` on every dlt entry. The latter makes
the connector manifest's intrinsic upstream URL and commit mandatory and binds
task attribution to the outer pinned selector, URL, and revision.

```yaml
# Structural sketch only; every list item must be expanded to the complete
# source-specific typed mapping used by v1 before loading it.
schema_version: five-source-ingest-v2
expected_task_count: 50
require_unique_independence_units: true
generator:
  name: elt-taskgen-five-source
  version: "<installed package version>"
  digest_kind: sha256-generator-tree-v1
  sha256: "<64 hex>"
  lock_sha256: "<64 hex>"
catalog: {path: package://config/sources.yaml, digest_kind: sha256-file, sha256: "<64 hex>"}
sources:
  dbt:        [{expected_task_id: "<first dbt id>"}]
  dlt:        [{expected_task_id: "<first dlt id>", require_embedded_provenance: true}]
  synsql:     [{expected_task_id: "<first SynSQL id>"}]
  schemapile: [{expected_task_id: "<first SchemaPile id>"}]
  wikidbs:    [{expected_task_id: "<first WikiDBs id>"}]
```

`sha256-generator-tree-v1` is canonical JSON over sorted records of normalized
logical path, byte size, and SHA-256 for every regular non-symlink `*.py` under
the installed `elt_taskgen` package, excluding `_resources`, plus the
resource-resolved `tools/wikidbs_family_map.py`. `lock_sha256` binds the
resource-resolved `uv.lock`. This boundary does not cover the interpreter,
native/system libraries, or services; those are runtime-attestation concerns.
The immediate preflight check and repeat under the coordinator lock catch
ordinary drift, not a privileged malicious local process racing executable
files on the same host.

Intake registers provenance-bound candidate TaskIRs and writes an exact-count,
per-origin completion receipt. It does not perform provider-backed review,
empirical difficulty measurement, release freezing, or real Airbyte/warehouse/
dbt execution. A successful v1 or v2 intake therefore makes no runtime-
certification claim.

The example's upstream pins were rechecked on 2026-09-07. dbt Twitter v1.3.1
is a formal release whose tag and current `main` both resolve to `b610ac…`.
The dlt pin `395750…` and SynSQL pin `cca2c8…` are immutable commits at their
current default-branch heads, not tagged releases. SchemaPile record 12682521
is the latest formal Zenodo version, v0.2. WikiDBs record 11559814 is its only
and current Zenodo version but declares no semantic version. The pinned dbt
compiled manifest is an operator-built derivative, not an upstream release
asset. SchemaPile's selected-member `APACHE-2.0` evidence must likewise not be
confused with the Zenodo record-level CC-BY-4.0 license.

The commands below remain useful as individual, exploratory adapter controls;
they are not the release-verifiable all-five intake contract.

```bash
WS=/tmp/verify/ws
DATA=$ELT_TASKGEN_DATA_ROOT/curation/packages_raw

# 1. the REFERENCE benchmark FIRST — not a source; it arms the contamination
#    index every real source is then checked against (§R)
.venv/bin/elt-taskgen measure-target --workspace $WS \
  --bench-root ../ELT-Bench

# 2. fivetran / dbt   (first run needs network for `dbt deps`)
.venv/bin/elt-taskgen ingest-dbt --workspace $WS --package dbt_servicenow

# 3. synsql
.venv/bin/elt-taskgen ingest-synsql --workspace $WS \
  --tables $DATA/SynSQL-2.5M/tables.json --db-id fisheries_data_and_management

# 4. dlt (use an admitted catalog entry; freshdesk is intentionally quarantined)
.venv/bin/elt-taskgen ingest-dlt --workspace $WS --connector workable

# 5. schemapile   (build the index once, ~5s)
.venv/bin/elt-taskgen-schemapile-index --out /tmp/verify/schemapile_index.json --top 15
.venv/bin/elt-taskgen ingest-schemapile --workspace $WS \
  --index /tmp/verify/schemapile_index.json \
  --cluster github_com_kykrueger_openbis_c106a4e57b99

# 6. wikidbs (c00050 currently violates the DECIMAL(38,9) portability bound)
.venv/bin/elt-taskgen ingest-wikidbs --workspace $WS \
  --db-dir "$DATA/WikiDBs/part-0/00012 METEC_SOLARWATT_TEAM_MEMBERS_DB"
```

The result is five candidate TaskIRs under `$WS/tasks/`, with the 100
measurement-only anchors stored separately under `$WS/reference/anchors/`.
All round-trip through `models.TaskIR`. A registered candidate becomes a task
only after completing all required pipeline stages. The five retained drives
did so only under the historical schema-2 contract; their census-1 releases do
not pass current verification.
See the legacy release table at the top of this document.

```
dbt__servicenow__servicenow__change_request_enhanced_92b0aa42 hash 0618953a0422aa66
dlt__workable                                                 hash 802302b6dca654ed
schemapile__github_com_kykrueger_openbis_c106a4e57b99__0454…  hash 69ca8663d9d2eda4
synsql__fisheries_data_and_management__vessels_landings_dis…  hash ac3ae08bfff664a3
wikidbs__c00012__00012_metec_solarwatt_team_members_db        hash 708c4eb0bffae608
```

---

## 11. Determinism measurements

Every ingest was run a second time into an independent fresh workspace and the
resulting `TaskIR.content_hash()` compared:

| Pool | content_hash | across two fresh workspaces |
|---|---|---|
| dlt (workable) | `802302b6dca654ed…` | MATCH |
| synsql (fisheries) | `ac3ae08bfff664a3…` | MATCH |
| schemapile (openbis) | `69ca8663d9d2eda4…` | MATCH |
| wikidbs (c00012) | `708c4eb0bffae608…` | MATCH |
| dbt (servicenow) | task_id `…92b0aa42` | MATCH across two independent `dbt parse` builds |

Derived artifacts are reproducible too:

* `elt-taskgen-schemapile-index` (implemented by the packaged
  `tools/schemapile_index.py` resource) — two rebuilds are byte-identical
  (sha256 `01b94b433d98711c3f5908a39ba799dec2de32f647e33bc2f05529225c4920dc`).
* `tools/audit_dlt_inventory.py` — audits all 29 vendored dlt sources by AST;
  repeated output is byte-identical.
* `tools/extract_dlt_manifest.py --all` — regenerates the three currently
  admitted task manifests; `--include-excluded` is an explicit maintenance
  path for clean review manifests and never bypasses contamination.
* dbt `target/manifest.json` is the exception because dbt
  stamps `generated_at` / `invocation_id` into it. It is a build intermediate,
  never a task artifact; idempotence is asserted on the clock-free
  `manifest_fingerprint()` instead.

---

## 12. Test-suite records

Before this verification pass, the baseline was 938 tests, 0 skips, 1 failure,
and 1 error. Both defects were pre-existing, were in the demo/council area, and
did not affect adapters. Both were fixed:

1. `tests/test_cli.py::TestDemoEndToEnd::test_second_run_resumes_without_rework`
   — a product defect in `cmd_demo`'s resume predicate. See §1's requirements
   and notes. It was fixed in `src/elt_taskgen/cli.py`
   (`_demo_task_is_resumable`).
2. `tests/test_council_efficacy.py::LiveMetrologyTest` — a wrong skip guard.
   `providers.transcripts_present()` is true as soon as any transcript exists,
   which it does (the demo's, seeded by `record-transcripts`). But the metrology
   mix is its own 10 blind specimens × 5 critic roles, and a prompt edit re-keys
   every one of them, so the harness failed closed on a missing prompt and the
   test failed. It now converts that specific `TranscriptMissingError` into a
   visible skip naming the missing role/prompt and its remedy, and only
   when there is no API key, so a keyed run still fails loudly.

After both fixes, on a machine without competing test runs:

```
$ .venv/bin/python -m unittest discover -s tests
Ran 938 tests in 1041.500s

OK (skipped=1)
```

This run produced 938 tests, 0 failures, 0 errors, 1 skip, and exit 0. The
metrology skip prints its full reason and remedy.

After the resume, SynSQL gate, dbt keys, and anchor relocation changes, the
suite was measured again with no concurrent tree changes:

```
$ .venv/bin/python -m unittest discover -s tests
Ran 980 tests in 1035.605s

OK (skipped=1)
```

This run produced 980 tests, 0 failures, 0 errors, 1 skip, and exit 0. The same
metrology test was skipped with a visible reason.

These counts are historical. The suite later grew past 1000 tests. At the time
of this documentation pass it was reported as 1044 tests passing with 1 visible
skip, or 2 visible skips when credentials were sourced. This was reported rather
than re-measured here: the verification run launched for this pass was still
executing against a machine running eight concurrent suites when the pass
closed, so no independent count is asserted. Do not treat this document as the
source of the current test count. The stable claims are 0 failures, 0 errors,
every skip reported with a reason naming the required environment variable and
the avoided cost, and the fact that
`ELT_TASKGEN_LIVE_TESTS` is referenced by exactly one test module
(`tests/test_council_efficacy.py`; verified by grep over `tests/`).

Run the suite with nothing else writing to `src/`: several
tests `ast.parse(inspect.getsource(cli.<fn>))`, and a concurrent edit to
`cli.py` mid-run makes `linecache` return a mis-sliced span and raises
`IndentationError` in `tests/test_filters_batch_triage.py`. That error is an
artifact of concurrent editing, not a defect.

The anchor-relocation changes (§R) added 7 tests in
`tests/test_adapters_dlt_anchor.py` — three pinning the command naming
(`measure-target` primary, `ingest-anchor` alias, help text stating the
never-trainable contract) and four pinning the store location and the legacy
migration (migrated-not-ignored, save-side merge, conflict fails closed,
no-op without a legacy store). `tests/test_adapters_dlt_anchor.py` alone had
31 passing tests.

The run took 1041 s (~17 min) on an idle machine and 2143 s (~36 min) for the
same baseline earlier in the session while 12 other agent suites were running.
Do not run this suite concurrently. `TestDemoEndToEnd` runs the full demo
pipeline twice without intermediate output, so a long silent period is
expected.

---

## 13. Cross-cutting notes

* `schemapile` and `wikidbs` emit all five populations at
  ingest (synthetic and provided-rows respectively). `dbt`, `dlt` and `synsql`
  emit candidates with no populations; the `generate` stage creates them.
* The counterfactual population is still the generation-module placeholder,
  so `generate` will fail coverage until literal rows are authored. This affects
  every pool equally; it is not a per-adapter regression.
* `models.Origin` participates in `content_hash`, so
  re-stamping an existing task with a different Origin produces a new task
  identity. `Origin` now has 8 members: `dbt, synsql, dlt, schemapile, wikidbs,
  eltbench_anchor, synthetic, demo`.
* Family-id namespacing is enforced at construction. `PoolSelection` rejects
  a blank license and requires `family_id` to be namespaced and inside the
  pool's own namespace.
* `slugify_family` is not injective (two names can slug to one segment) and
  it truncates at 64 chars with a `sha256[:12]` suffix. Anything doing bulk
  selection must check for collisions itself; the helper sees one name at a time.
* The counterfactual placeholder causes the `schemapile` failure at `generate`.
  The no-populations-at-ingest design causes the `dlt` failure at `generate`
  and contributes to the `synsql` failure. Neither condition is a property of
  the source corpus. Changing both conditions would change four source statuses
  together.

---

## 14. Maintaining this document

Counts, hashes, and command names in this document can often be derived from the
code. If a prose value can be computed from code, a test should compute it. See
README §8, which lists the
derivable claims (gate count, stage count, council/routed role lists, subcommand
list, `Origin` members, the demo lineage-root hash) with their authoritative
code definitions and the tests that should pin them.

Specific to this document:

* The historical release table is not a current source census. Its immutable
  release ids are evidence of schema-2 runs; the `docs/runs/*.md` reports are
  dated run snapshots. Current status requires a fresh run and a release that
  passes the current verifier.
* The verified ingest command blocks are confirmed to run. They are not
  evidence that the resulting candidate survives the pipeline. A dated run
  snapshot says only what happened in that run; current status requires fresh
  evidence under the current contract.
* The historical demo identity chain (§1) can be recalculated from the fixture
  plus preserved legacy author response text, but there is no current committed
  `semantic_author` replay fixture. The migration test proves the legacy record
  is not re-keyed or replayed; a recalculated old content hash is not current
  provider evidence.
* Test-suite counts (§12) change frequently. Prefer citing the result structure
  result (0 failures, N visible skips, each naming its reason) over the exact
  test count.

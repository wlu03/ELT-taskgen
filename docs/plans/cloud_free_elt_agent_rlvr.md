# Plan: cloud-free artifact-level RLVR for ELT agents

Status: L1 operational; first deterministic L2 protocol/workflow slice operational; interactive sandbox and real-runtime certification remain
Last reviewed: 2026-09-03

## L2 vertical-slice checkpoint (2026-09-03)

The following are implemented on top of `workspace-v1` without changing its
reward authority:

- immutable `warehouse-behavior-v1` profiles for Snowflake, Databricks, and
  Redshift, calibrated from `runs/runtime_canary_20260902/ADVERSARIAL_REPORT.md`;
- deterministic attempt-local workspace/source/destination/connection/job IDs,
  logical clock ticks, validation, polling, success, failure, cancellation,
  and exact second-sync append behavior;
- protocol state stored only in `.workspace-runtime/airbyte/` and populated
  from the existing normalized Terraform intent graph;
- data movement delegated to the existing trusted `local_sync` executor, so
  EL and dbt retain the same isolated DuckDB state and the existing strict EL,
  raw immutability, mart comparison, and minimum reward rule;
- `elt-taskgen training install|seal|score|inspect`, with authenticated seal or
  trusted candidate-workspace input and deterministic JSON output; and
- focused local tests for calibrated representation rules, stable resources
  and transitions, invalid connections, isolation, no network calls, and
  append duplication.

The profiles cover only namespace/database/catalog projection, identifier
folding and quoting, business versus admitted metadata columns,
DECIMAL(38,9), UTC timestamps, strict booleans, canonical JSON/VARIANT scalar
representation, empty-string/NULL behavior, append, and stable errors.
Unsupported behavior fails closed. This is a deterministic protocol/workflow
proxy, not an Airbyte clone or evidence of Snowflake, Databricks, or Redshift
runtime compatibility.

Provision the pinned transform runtime before training:

```bash
UV_OFFLINE=1 uv sync --project runtime-images/dbt-duckdb --locked
```

Scoring example (substitute the release's destination personality):

```bash
elt-taskgen training score \
  --release runs/<release>/release --task-id <task> \
  --seal /tmp/sealed --seal-sha256 <digest> \
  --destination snowflake --attempts-dir /tmp/rlvr-attempts \
  --dbt-python runtime-images/dbt-duckdb/.venv/bin/python \
  --dbt-manifest runtime-images/dbt-duckdb/runtime.json
```

## Implementation checkpoint (2026-09-01)

Operational now:

- a separately versioned `workspace-v1` submission/result and failure-taxonomy
  contract under `src/elt_taskgen/training/`;
- verified loading of schema-3 combined releases, including the exact
  `documentation/README.md`, TaskIR, gold, source roots, destination, and
  private Airbyte connector contract;
- fresh public-task installation with `elt/` writable, static public bytes
  pinned, and any outside-`elt/` mutation rejected at sealing;
- deterministic authenticated candidate-only sealing and clean replay without
  Terraform, dbt, DuckDB, credential, log, or target state;
- strict symlink, non-regular-file, case-collision, size, static-public-byte,
  and high-confidence secret-literal checks;
- a real `python-hcl2` Terraform intent compiler with semantic graph
  comparison, exact five-backend routing, destination contracts, stable error
  codes, and unsafe-HCL rejection;
- deterministic Snowflake, Databricks, and Redshift namespace projection plus
  trusted selected-stream loading for Postgres, MongoDB, REST, S3, and files;
- upstream row-count, strict business-schema, and typed-content EL evidence on
  one fresh DuckDB file per population;
- a separately locked CPython 3.9, dbt Core 1.10.22, dbt-duckdb 1.10.0, and
  DuckDB 1.4.5 runtime that performs real `dbt parse`, `dbt compile`, and
  `dbt run --full-refresh`;
- a closed `portable-dbt-sql-v3` destination parser/rewrite boundary (v2, 2026-09-15:
  temporal unit arguments, `TimestampTrunc`, `||`, text-operand `MD5`, Redshift
  `VARCHAR(MAX)`; v3, 2026-09-16: first measured against live Snowflake and
  Databricks, see tests/fixtures/warehouse_differential), dbt graph
  checks, persistent-mart checks, private-gold/evaluator preflight, and raw
  physical-state immutability checks;
- the `workspace-v1` same-state scorer, with strict-EL gating, all-hidden-
  population minimum aggregation, and no-label harness/task failures;
- real all-population acceptance runs for both a generated files-plus-Postgres
  pilot and the five-backend mixed fixture; and
- alignment of the legacy transform-only verifier on
  `documentation/README.md`, while keeping that standalone-SQL mode explicitly
  separate from `workspace-v1`.

Not yet implemented: the interactive agent/tool sandbox, hard OS-level network
isolation, offline Terraform provider/init/apply compatibility, an HTTP action
surface for the new connection/job state machine, or sparse real
Snowflake/Databricks/Redshift certification. The scorer parses Terraform but
does not run the Terraform binary or Airbyte. No cloud service is
invoked by this implementation.

This plan defines how to train an agent on the original ELT-Bench workflow
without running Snowflake, Databricks, or Amazon Redshift during ordinary RLVR
rollouts. The agent must still author the artifacts used by the real benchmark:

- Terraform/Airbyte configuration for Extract + Load (EL); and
- a dbt project with persistent mart models for Transform (T).

DuckDB provides deterministic local execution and private gold. It does not
represent any of the three warehouses. Real Airbyte and warehouse runs remain
a separate, sparse certification path outside the training loop.

This plan complements:

- [`../EXECUTION_MODEL.md`](../EXECUTION_MODEL.md), the architecture and claim
  authority;
- [`../TWO_STAGE_RLVR_CONTRACT.md`](../TWO_STAGE_RLVR_CONTRACT.md), the public
  one-task/two-phase runtime contract;
- [`duckdb_rlvr_cloud_runtime_migration.md`](duckdb_rlvr_cloud_runtime_migration.md),
  the semantic and cloud-runtime migration plan;
- `semantic_runtime_parity.md` (maintainer-local; not published in this repository), the exact
  versus real-only evidence policy; and
- [`tinker_elt_rl_environment.md`](tinker_elt_rl_environment.md), the Tinker
  coordinator and rollout integration design.

## Decision

Add a new artifact-level training path rather than changing the current
`semantic-v1` JSON scorer.

```text
Current low-cost semantic scorer
  load_plan JSON + sql_by_mart JSON
  -> trusted DuckDB readers and standalone queries
  -> semantic diagnostic and curriculum signal

New artifact-level scorer
  candidate elt/main.tf + candidate dbt project
  -> normalized Airbyte intent and local sync execution
  -> one attempt-local DuckDB warehouse
  -> dbt-duckdb builds persistent marts in that same warehouse
  -> hidden typed raw-table and mart comparison
  -> EL-gated end-to-end RLVR reward

Sparse certification
  same candidate artifact shape
  -> real Airbyte -> selected real warehouse -> matching dbt adapter
  -> destination-scoped evidence
```

The current path supports low-cost semantic curricula. The artifact-level path
is required for training agents to write Terraform and dbt. Only sparse
certification supports a real-runtime compatibility claim.

## Goals

1. Train the same skills the public benchmark requests: reading the task,
   editing `elt/main.tf`, selecting streams, applying an EL configuration,
   creating a dbt project, running dbt, inspecting tables, and correcting
   errors.
2. Keep ordinary rollouts free of Airbyte control-plane, Snowflake,
   Databricks, and Redshift compute costs.
3. Preserve one task identity and one mutable attempt state across EL and T.
4. Give deterministic, executable reward for both artifact correctness and
   resulting data correctness.
5. Replay one candidate solution on hidden populations without exposing gold,
   private TaskIR, or hidden source rows.
6. Separate policy failures from task, harness, and infrastructure failures so
   bad infrastructure never becomes a negative training label.
7. Make destination-specific limitations explicit and validate them locally
   where possible, then sparsely on the real target.

## Non-goals

- Reimplementing all of Airbyte.
- The local path does not emulate warehouse authentication, networking,
  billing, permissions, concurrency, query planning, or service-specific
  operational failures.
- Treating DuckDB execution as evidence of Snowflake, Databricks, or Redshift
  compatibility.
- Shipping DuckDB files, hidden populations, reference SQL, or answer keys in
  the public task.
- Replacing the real benchmark submission with `load_plan` or
  `sql_by_mart` JSON.
- Silently transpiling arbitrary destination SQL and assuming it is equivalent.

## Required workflow

The original ELT-Bench agent receives one task and performs two phases in the
same attempt:

```text
config.yaml + schemas + source documentation
                 |
                 v
agent edits elt/main.tf
                 |
terraform init/apply -> Airbyte resources -> sync jobs
                 |
                 v
raw tables in the destination warehouse             Stage 1 evaluation
                 |
data_model.yaml + inspection of those raw tables
                 |
                 v
agent authors and runs a dbt project
                 |
                 v
persistent mart tables                               Stage 2 evaluation
```

Stage 2 is not a standalone SQL-response task. The original prompt tells the
agent to initialize dbt, write one model per required output, run `dbt run`,
inspect the resulting warehouse tables, and fix errors. The evaluator reads
materialized marts or executes private evaluation SQL against that warehouse.

The local training environment must therefore grade a candidate workspace and
its resulting database state rather than only a final JSON object.

## Current implementation and exact gaps

| Area | Operational L1 behavior | Remaining work |
| --- | --- | --- |
| Public task | Schema-3 export plus `training.package` verifies and installs one combined, original-shaped task | Interactive action tooling and the L2 protocol proxy (not an emulation) |
| EL submission | `workspace-v1` seals candidate `main.tf`; `python-hcl2` compiles it to a normalized graph and enforces the admitted provider/resource surface | Terraform CLI/provider execution and Airbyte job APIs at L2 |
| T submission | `workspace-v1` seals the candidate dbt project; the scorer injects its own profile and runs pinned dbt Core/dbt-duckdb | Interactive dbt actions and sparse execution with each real adapter |
| EL/T state | Selected streams are loaded into one fresh DuckDB file, deterministic Airbyte-shaped resources/jobs are recorded attempt-locally, and candidate dbt runs against that exact file | Interactive HTTP/tool surface, offline Terraform state, and real destination state |
| EL correctness | Row counts, strict business schema, and strict typed-content fingerprints are calculated; metadata columns are excluded only from business comparison | Real connector metadata/type parity evidence |
| Cloud runtime | `runtime/execution.py` remains a separate sparse real-runtime path | Execute and sign the Snowflake, Databricks, and Redshift certification matrix |
| Local dbt evidence | A separately locked runtime runs `dbt parse`, `compile`, and `run --full-refresh`, validates the graph/marts, and preflights private evidence | Build the committed lock into isolated training workers |
| Agent environment | Fresh install, authenticated seal, clean replay, bounded subprocess output, and scorer-owned state exist | Hard no-network, filesystem, CPU, memory, PID, and process-tree isolation |
| Verifier integration | Combined EL+T uses `workspace-v1`; the legacy transform-only verifier remains standalone-SQL | Optional isolated-T curriculum head, if still desired |
| Legacy scorer | `semantic-v1` remains unchanged and has differential regression coverage | None; it remains L0 rather than becoming the combined scorer |

The state split below remains only in the retained `semantic-v1` diagnostic
path:

```text
trusted source loader -> DuckDB A -> candidate transformation SQL
candidate load plan   -> DuckDB B -> Stage 1 count check
```

This supports independent diagnostic heads but does not measure an end-to-end
candidate pipeline. The operational `workspace-v1` scorer instead uses:

```text
candidate EL artifacts -> attempt DuckDB -> candidate dbt -> marts
                              ^                 |
                              +-----------------+
                                  same state
```

An optional transformation-only diagnostic may continue to run candidate dbt
against trusted raw data. It must be named and logged as an isolated T
diagnostic and must not be confused with end-to-end reward.

## Fidelity levels

Implement four explicit levels. Do not label a lower level as a higher one.

| Level | Agent output | Execution | Valid claim |
| --- | --- | --- | --- |
| L0: semantic JSON | `load_plan` and `sql_by_mart` | trusted readers plus standalone DuckDB queries | semantic curriculum only |
| L1: artifact proxy | `main.tf` and dbt project | normalized Terraform intent, trusted local sync, dbt-duckdb | artifact and semantic workflow proxy |
| L2: protocol proxy | same workspace and commands as the real benchmark | offline Terraform provider/control-plane shim, job API, DuckDB destination, dbt-duckdb | local workflow/protocol proxy |
| L3: real runtime | same public task and destination-specific workspace | real Airbyte, selected warehouse, and matching dbt adapter | destination- and matrix-scoped agent score or certification |

L1 is the initial artifact-execution milestone. L2 lets the agent perform
`terraform init`, `terraform apply`, job triggering, status polling, SQL
inspection, and `dbt run` through the same action vocabulary as the real
workflow. L3 is sparse and never part of ordinary training.

## Target local architecture

```text
verified combined release
  public/<task_id>/                  solver-visible specification
  private/<task_id>/                 trusted package, never mounted for solver
             |
             v
fresh attempt workspace
  task/config.yaml
  task/data_model.yaml
  task/schemas/
  task/documentation/
  task/elt/main.tf                   candidate-editable
  task/elt/dbt_project.yml           candidate-created
  task/elt/models/*.sql              candidate-created
  task/elt/models/sources.yml        candidate-created
  .workspace-runtime/profiles/       harness-created, never candidate-authored
             |
             v
Terraform intent compiler or protocol proxy
             |
normalized Airbyte resource graph
             |
trusted connector executors read the selected population
             |
attempt.duckdb / projected raw namespace
             |
Stage 1 contract + raw-state verifier
             |
dbt-duckdb parse -> compile -> run --full-refresh
             |
persistent required marts in the same attempt.duckdb
             |
raw-state immutability check + Stage 2 comparator
             |
per-population metrics -> minimum hidden-population reward
```

The solver sees only the public task, its own workspace, tool output, and the
development environment. The scorer alone sees source mappings, hidden
populations, expected resource intent, typed raw fingerprints, mart gold, and
mutation evidence.

## Contract 1: one task, two phases, one state

Use the versioned `workspace-v1` submission contract. At L1 the submission is
an authenticated sealed snapshot of the solver-authored `elt/` tree, not a
JSON representation of the answer. Bounded action traces are an L2/interactive
environment concern and are not fabricated by the artifact scorer.

Required candidate artifacts:

```text
elt/
  main.tf
  dbt_project.yml
  models/
    sources.yml
    <one or more model .sql files>
```

The dbt profile is always harness-owned and attempt-local. Candidate
`profiles.yml`, packages, hooks, macros, and Python models are rejected by the
current closed L1 policy rather than trusted as credential or execution input.

Attempt-local artifacts that are never reusable across populations:

```text
.terraform/
terraform.tfstate
target/
logs/
attempt.duckdb
generated local credentials
proxy-generated Airbyte resource and job IDs (not emulated)
```

The grader must reject a submission that references files outside `elt/`,
contains symlinks, embeds real credentials, or relies on state not created in
the current attempt.

## Contract 2: normalized EL intent

Terraform syntax alone is not enough. Convert accepted Airbyte resources into
a canonical semantic graph:

```text
sources:
  - logical source identity
  - connector kind
  - endpoint/database/bucket/prefix fields
  - declared stream set

destination:
  - selected destination kind
  - logical database/catalog and schema
  - namespace mode

connections:
  - source identity
  - destination identity
  - exact selected streams
  - sync mode
  - destination namespace behavior
```

Compare meaning, not arbitrary Terraform resource labels or formatting.
Equivalent resource names and ordering must receive the same result. Reject:

- missing or extra source connectors;
- the wrong connector type;
- missing, misspelled, or extra streams;
- an incorrect destination or namespace;
- an unsupported sync mode;
- connections that do not join the required source to the destination;
- duplicate connections that would duplicate rows;
- non-Airbyte resources, provisioners, `local-exec`, remote modules, and
  unapproved providers; and
- secret literals where the contract requires injected variables.

The expected normalized graph is compiled privately from TaskIR and the
destination contract. Do not compare candidate HCL to one canonical text file.

## Contract 3: local EL execution

The EL proxy must execute the candidate-selected graph, not load every source
unconditionally.

For each normalized connection:

1. Resolve its declared backend and streams through a private mapping bound to
   the frozen task and population.
2. Read records through a trusted connector-specific executor.
3. Apply the declared full-refresh-append behavior to a fresh attempt
   namespace.
4. Add only the connector metadata columns allowed by the pinned local
   contract.
5. Materialize the selected raw tables in DuckDB.
6. Record stable local-sync lifecycle states and error codes.

L1 does not claim to have triggered an Airbyte job: `sync_lifecycle=1` means
that every connection in the accepted normalized graph completed through the
trusted connector executor. Attempt-local Airbyte-shaped connection and job
IDs belong to L2.

Trusted executors are required for:

- PostgreSQL seed SQL and tables;
- MongoDB JSONL collections;
- all REST pages, including termination and pagination behavior;
- every selected S3 `part-*.jsonl` object under the exact bucket/prefix; and
- file-backed CSV/JSONL URLs.

L1 may resolve these through private fixture mappings without starting source
services. L2 starts the local source services and exposes an Airbyte-compatible
control-plane subset so the same agent command sequence has observable jobs.
Both levels must produce the same canonical raw business rows.

### Direct Terraform signal

Decompose EL reward into four measurable facts:

1. `terraform_safety`: the workspace uses only the admitted provider/resource
   surface.
2. `airbyte_intent`: the normalized source, destination, connection, stream,
   sync, and namespace graph is correct.
3. `sync_lifecycle`: all required attempt-local jobs were triggered and reached
   success without fabricated IDs or stale state.
4. `raw_state`: every required business table has the expected schema and
   typed row multiset.

These facts measure Terraform behavior. Loading tables through a different
mechanism cannot satisfy `terraform_safety`, `airbyte_intent`, or
`sync_lifecycle`.

### Upstream-compatible and strict EL heads

Keep two distinct Stage 1 measurements:

- `upstream_stage1`: exact table existence and row counts, for comparison with
  original ELT-Bench; and
- `strict_el`: normalized resource intent plus typed business-column schema
  and content equality.

The artifact-level training gate uses `strict_el`. Count-only checks do not
detect count-preserving corruption, duplicate replacement, wrong values, or
direct writes.

Airbyte-added metadata is handled with a connector-version-specific allowlist.
It is excluded only from the raw business-row fingerprint. Unexpected business
columns, missing columns, duplicate columns after case folding, and type drift
remain failures.

## Contract 4: local dbt transformation

Use a pinned dbt Core plus `dbt-duckdb` runner. Do not reduce candidate models
to extracted standalone query strings.

For every population, the runner must:

1. inject only the attempt-local DuckDB path and namespace variables;
2. run `dbt parse` and retain a bounded `manifest.json`;
3. run `dbt compile` and safety-check compiled SQL and hooks;
4. run `dbt run --full-refresh` with a fresh `target/` directory;
5. retain bounded `run_results.json` and stable error codes;
6. confirm that each required mart exists in the declared namespace and has
   the required materialization;
7. query only the declared output columns;
8. sort by private stable keys or compare as a typed multiset; and
9. compare with the immutable DuckDB gold using the existing semantic
   comparator.

The model graph must be inspected as well as the final rows:

- every required mart has a dbt node with the correct final alias;
- raw relations are referenced through declared dbt sources or an approved
  compatibility macro;
- model schema and database projection are correct;
- all required models were selected and executed;
- no model reads files, external databases, hidden paths, or the private
  oracle; and
- hooks and macros do not mutate raw tables or evaluator state.

Take typed raw-table fingerprints immediately after EL and again after dbt.
Any raw-state change invalidates the attempt. This blocks a model from
rewriting its inputs to make a mart appear correct.

## Contract 5: same-state and isolated-T rewards

Record both, but never substitute one for the other.

### Official end-to-end head

```text
candidate Terraform
  -> candidate-selected local sync
  -> candidate-produced raw DuckDB state
  -> candidate dbt project
  -> candidate marts
```

This head determines the combined training reward.

### Optional isolated transformation head

```text
trusted source loader
  -> known-correct raw DuckDB state
  -> candidate dbt project
  -> candidate marts
```

This head helps attribute whether a failure is in EL or T and supports a
transformation curriculum. It is diagnostic or a separately named curriculum
reward. It cannot rescue the official reward when candidate EL fails.

## Reward model

For population `p`, compute:

```text
terraform_contract_p in [0, 1]
sync_lifecycle_p     in {0, 1}
upstream_stage1_p    in {0, 1}
strict_raw_tables_p  in [0, 1]
strict_el_pass_p     in {0, 1}
dbt_project_p        in [0, 1]
mart_reward_p        in [0, 1]
raw_immutable_p      in {0, 1}

strict_el_pass_p = 1 only if:
  terraform contract is exact
  and sync lifecycle succeeded
  and every required raw table exactly matches

end_to_end_p = mart_reward_p only if:
  strict_el_pass_p == 1
  and raw_immutable_p == 1
otherwise 0
```

The final combined reward is the minimum `end_to_end_p` over the hidden graded
populations. Development remains visible and diagnostic. Report the population
mean and individual private diagnostics to operators, but do not weaken the
minimum reward without a versioned contract change.

Use component heads for curricula and advantage diagnostics; do not blend them
into an undocumented weighted score. Use this curriculum:

1. Terraform resource-graph construction on development tasks.
2. EL execution with hidden stream-selection and content mutations.
3. Isolated dbt transformation on trusted raw state.
4. Combined same-state EL -> T episodes.
5. Hidden multi-population robustness.

The final held-out evaluation always uses the combined same-state head.

## Hidden population replay

One solver-authored workspace is replayed unchanged across:

- `development`, visible and ungraded;
- `primary`;
- `resampled`;
- `counterfactual`; and
- `stress`.

Every L1 replay receives fresh:

- source fixture/service state;
- DuckDB file and logical destination namespace;
- normalized Terraform/proxy state (without executing Terraform);
- dbt `target/` and logs; and
- a generated harness-owned local profile.

L2 will additionally create fresh Terraform state, provider cache,
Airbyte-compatible resource IDs, connection IDs, jobs, and generated local
credentials. Those objects do not exist in L1 and are therefore neither
fabricated nor reused by the current scorer.

Do not copy `terraform.tfstate`, `.terraform/`, connection IDs, DuckDB files,
`target/`, or generated profiles between populations. Reusing state would hide
idempotency errors and can duplicate full-refresh-append loads.

The candidate may inspect and debug only development. Hidden populations run
after the workspace is sealed and return only bounded reward heads and stable
error categories.

## Destination-specific behavior

The public task is generated for one selected destination. Local execution has
two responsibilities:

1. verify destination-specific artifact intent; and
2. execute the destination-neutral business semantics in DuckDB.

These are related but not identical.

| Target | Local artifact checks | Local semantic projection | Real-only checks |
| --- | --- | --- | --- |
| Snowflake | destination resource shape, database/schema/warehouse fields, namespace and identifier policy, dbt profile/adapter contract | project raw tables into a stable DuckDB catalog/schema; normalize NUMBER, UTC timestamps, and JSON access through admitted macros or rewrites | authentication, role grants, warehouse lifecycle, real Airbyte normalization, VARIANT/OBJECT/ARRAY and timestamp-type behavior |
| Databricks | destination resource shape, workspace/HTTP-path/catalog/schema and connector-owned staging-cleanup flag, dbt profile contract; pinned Airbyte 4.0.2 exposes no candidate Volume selector | project catalog/schema locally; treat connector JSON strings and admitted Spark-style expressions through explicit compatibility rules | service-principal auth, harness-granted Unity Catalog and `CREATE VOLUME` privileges, connector-internal Volume staging/cleanup, SQL warehouse lifecycle, Delta writer behavior |
| Redshift | destination resource shape, database/schema/S3 staging-prefix intent, dbt profile contract | project connected database/schema locally; normalize DECIMAL(38,9) and admitted SUPER access through explicit rules | IAM/VPC/auth, S3 staging and COPY lifecycle, SUPER limits, real identifier sanitization and cleanup |

### SQL portability policy

Prefer one admitted semantic subset plus versioned compatibility macros for:

- JSON scalar extraction;
- decimal casts and rounding;
- UTC timestamp normalization;
- date formatting and truncation;
- null ordering; and
- logical source namespaces.

If candidate SQL uses target-specific syntax, apply this sequence:

1. Parse it in the declared target dialect.
2. Accept only constructs covered by the task's parity registry.
3. Rewrite through a small, versioned, tested rule set.
4. Parse the rewritten DuckDB SQL.
5. Differential-test the rule against the real warehouse before enabling it
   for reward.

Do not use unrestricted best-effort transpilation to determine correctness. A
task requiring a destination feature with no validated local interpretation is
not eligible for fully cloud-free semantic reward. It may receive static
artifact feedback and must be measured through the sparse real-runtime path.

## Local Airbyte/Terraform protocol proxy

L2 should preserve the observable portion of the original workflow while
implementing only the benchmark's admitted subset.

Required behavior:

- offline `terraform init` using a digest-pinned provider mirror;
- `terraform validate`, `plan`, and `apply` behavior for admitted Airbyte
  source, destination, and connection resources;
- deterministic attempt-local resource IDs and `terraform.tfstate`;
- a job-trigger endpoint;
- connection discovery and job status polling;
- terminal `succeeded`, `failed`, and timeout states; and
- a DuckDB destination adapter that invokes the trusted local source
  executors.

There are two implementation options:

1. Run the pinned upstream Airbyte Terraform provider against a narrow local
   Airbyte API shim.
2. Provide a benchmark-owned offline Terraform provider implementing only the
   frozen resource schemas used by generated tasks.

Prototype both with one task before committing. Prefer the upstream provider
when the required API surface is stable and small enough to model faithfully;
otherwise own a narrow provider and explicitly label it as a protocol proxy.
In both cases, the normalized resource graph is the reward authority, not the
provider's state-file spelling.

Reuse the original ELT-Bench public documentation shape,
`check_job_status.py`, source-service conventions, and action sequence where
they are correct. Do not copy stale-state behavior, unpinned downloads,
credential leakage, or evaluator defects from the historical scripts.

## Agent tool surface

The local environment should expose the smallest action set needed to perform
the original workflow:

- list/read allowed task and workspace files;
- write files only beneath `elt/`;
- initialize, validate, plan, and apply Terraform;
- inspect attempt-local Terraform outputs/state through a bounded view;
- trigger and poll local sync jobs;
- list raw relations and inspect bounded sample rows in development;
- initialize/parse/compile/run dbt;
- execute bounded read-only SQL against the development attempt database; and
- terminate the attempt.

Do not expose direct fixture readers, private paths, arbitrary host shell,
Docker socket, cloud credentials, hidden-population databases, expected
counts, gold rows, or reference SQL.

Tool observations must be deterministic and bounded. Replace raw exceptions
with stable public error codes plus safe, actionable messages. Store full
trusted diagnostics outside the trajectory.

## Sandbox and reward-integrity requirements

Artifact-level episodes execute model-authored files and therefore require a
real sandbox boundary.

Minimum controls:

- fresh rootless container, microVM, or equivalently isolated worker per
  attempt;
- no network by default;
- read-only public task mount and private package outside the sandbox;
- one writable attempt directory;
- no Docker socket, host environment, SSH agent, or cloud metadata access;
- CPU, RAM, PID, filesystem, output, and wall-clock limits;
- pinned Terraform/provider/dbt/DuckDB binaries and image digest;
- no remote Terraform modules or unapproved providers;
- no `local-exec`, `remote-exec`, arbitrary provisioners, or unsafe data
  sources;
- no external dbt packages unless vendored and digest-pinned;
- audited dbt hooks and macros;
- no DuckDB `ATTACH` outside the attempt, external access, extension loading,
  or file readers in candidate SQL;
- read-only source fixtures or fresh protected copies;
- raw fingerprints before and after T; and
- unconditional cleanup after success, failure, cancellation, or timeout.

An attempt that compromises the sandbox or touches forbidden resources is a
policy failure with zero reward. A task-package corruption, scorer crash, or
host-capacity failure is a harness failure and produces no training label.

## Failure taxonomy

| Class | Examples | Reward treatment |
| --- | --- | --- |
| Valid policy failure | invalid HCL, wrong stream, failed local sync caused by candidate config, dbt compile error, wrong mart | measured zero or partial component reward |
| Policy violation | path escape, external access, forbidden provider/provisioner, source mutation, hidden-data access | zero, terminate, security metric |
| Task defect | public/private contract mismatch, impossible target projection, corrupt population or gold | no label; quarantine task |
| Harness defect | local-proxy bug, comparator failure, missing binary, private package mismatch | no label; halt or quarantine |
| Transient infrastructure | worker eviction, host disk exhaustion, coordinator transport failure | no label; clean retry with fresh state |
| Real-runtime failure | cloud auth/network/service outage during sparse measurement | no local training label; classify separately from agent correctness |

Never turn missing evidence or an infrastructure exception into `0.0`.

## Proposed private package and code layout

Public task shape remains unchanged. Add private training-only material:

```text
private/<task_id>/training/
  workspace_contract.json
  expected_airbyte_intent.json
  source_mapping.json
  namespace_projection.json
  allowed_metadata.json
  destination_feature_manifest.json
  profiles/
    duckdb.template.yml
  canonical_solution/                 # private tests only
    elt/main.tf
    elt/dbt_project.yml
    elt/models/
```

The files contain no reusable cloud credentials. Integrity digests belong in
the private release evidence; no solver-facing checksum file is required.

Suggested implementation modules:

```text
src/elt_taskgen/training/
  models.py                 workspace-v1 input/result and stable error models
  package.py                verified private package loader
  workspace.py              fresh install, seal, replay, and cleanup
  terraform_intent.py       HCL/resource graph normalization and safety
  airbyte_proxy.py          resources, connections, jobs, and protocol state
  local_sync.py             trusted backend-to-DuckDB execution
  namespace.py              destination-to-DuckDB relation projection
  dbt_runner.py             parse/compile/run and artifact collection
  scorer.py                 same-state population execution and reward
  sandbox.py                resource and capability boundary
  trace.py                  bounded action and evidence records
```

Keep target-specific rules in versioned data or focused modules rather than
scattered string conditionals. Reuse existing TaskIR, source renderers,
comparators, strict typed fingerprints, population models, and cloud runtime
contracts instead of creating new semantic authorities.

## Test plan

The executable L1 path is `make workspace-acceptance`. It performs a locked,
offline dbt-runtime sync and then runs the Terraform-intent, local-sync,
dbt-runner, and same-state scorer suites. It fails during provisioning rather
than silently skipping the real dbt pilots when the exact runtime is absent.
The 2026-09-01 checkpoint passes 79 tests, including two concurrent real
replays of one authenticated seal and all-population files-plus-PostgreSQL and
five-backend pilots. The repository-wide regression run also passes 2,828
tests with three expected skips; the semantic release gate separately passes
11 tests with one expected legacy skip.

### 1. Package and workspace tests

- combined task installs with the exact exporter shape;
- `documentation/README.md` is accepted as the authored specification;
- no top-level `documentation.md` dependency remains in the combined path;
- public/private trees and credentials cannot cross the boundary;
- symlinks and traversal are rejected;
- every population receives fresh state; and
- sealed candidate bytes replay identically.

### 2. Terraform intent tests

For every admitted source connector and destination:

- canonical correct configuration;
- formatting and resource-label equivalence;
- missing source, destination, or connection;
- wrong connector kind;
- missing, extra, or misspelled stream;
- wrong sync mode or namespace behavior;
- duplicate connection and duplicate-load risk;
- invalid variable reference;
- literal credential leakage;
- remote module, extra provider, data source, provisioner, and `local-exec`;
- dependency cycle and unresolved reference; and
- stale or forged Terraform state.

### 3. Local connector tests

- PostgreSQL types, schemas, multiple tables, nulls, and empty tables;
- MongoDB nested documents, missing keys, arrays, and JSON null;
- REST first/last page, empty page, duplicate page, missing page, and failure;
- S3 exact bucket/prefix, multipart selection, ordering, empty object, and
  malformed object;
- file URL, CSV quoting/newlines, JSONL, Unicode, empty string versus null;
- mixed five-backend task;
- content-preserving row order changes; and
- count-preserving content corruption that must fail strict EL.

### 4. dbt project tests

- missing or invalid `dbt_project.yml`;
- profile mismatch and incorrect namespace;
- missing required model;
- wrong alias, schema, database/catalog, or materialization;
- SQL or Jinja compile failure;
- broken `source()` or `ref()`;
- partial model selection;
- stale incremental state;
- unsafe package, hook, macro, external function, or file read;
- raw-table mutation;
- extra Airbyte metadata selected into marts;
- duplicate output keys and wrong grain; and
- rerun from clean state and `--full-refresh` determinism.

### 5. Transformation semantic fixtures

Cover at least:

- inner versus left joins and departments/entities with no children;
- one-to-many and many-to-many fanout;
- grouping grain and duplicate keys;
- null defaults and empty strings;
- counts, distinct counts, sums, averages, ratios, and percentages;
- decimal precision, rounding, negative values, and negative zero;
- UTC timestamps, date boundaries, and timezone-equivalent values;
- JSON null versus missing and nested extraction;
- ranking, ties, deterministic tie-breaks, and window frames;
- explicit null ordering;
- Unicode, case, whitespace, and identifier collisions; and
- empty inputs and stress-scale skew.

### 6. Same-state end-to-end mutations

These cases verify that the new scorer removes the present state split:

- correct Terraform plus correct dbt passes;
- correct counts but wrong raw values fails strict EL and final reward;
- wrong EL plus a dbt model that would pass on trusted raw data receives zero
  official reward;
- correct EL plus wrong dbt receives the mart fraction;
- dbt that repairs or replaces raw tables fails the post-T raw fingerprint;
- a solution overfit to development loses reward on counterfactual or
  resampled data;
- reuse of prior Terraform/dbt/DuckDB state is detected; and
- direct DuckDB writes without admitted Terraform/Airbyte intent cannot pass.

### 7. Destination rule tests

For Snowflake, Databricks, and Redshift:

- destination resource and profile snapshots;
- namespace projection and identifier casing;
- decimal, timestamp, JSON, and null-order compatibility rules;
- target-dialect parse rejection and admitted rewrite reference-output tests;
- physical type/name expectations as separate evidence from logical rows; and
- every locally admitted rewrite has a pinned real-warehouse differential
  fixture before it can affect reward.

### 8. Determinism, security, and capacity tests

- byte-identical result for repeated clean executions;
- concurrent sibling isolation;
- cancellation and timeout cleanup;
- bounded manifest, logs, SQL output, and sample rows;
- no-network enforcement;
- memory, CPU, PID, disk, and process-tree limits;
- no private path/value in agent observations or training logs;
- sandbox escape and malicious HCL/Jinja/SQL corpus; and
- resource soak establishing a safe rollout concurrency cap.

### 9. Sparse differential tests

Use two initial pilots:

1. files plus PostgreSQL with one join/rollup mart; and
2. all five source backends with JSON, decimal, timestamp, null, identifier,
   metadata, aggregation, and ranking cases.

For each destination, compare the same canonical candidate artifacts and
population through:

```text
local protocol proxy -> DuckDB -> dbt-duckdb
real Airbyte -> real destination -> matching dbt adapter
```

Logical business rows and marts must match after declared normalization.
Physical schema, connector metadata, job lifecycle, and cleanup are separate
destination evidence. Record and sign real evidence. Mock evidence cannot be
labelled live.

## Implementation phases

### Phase 0 — Operational: freeze contracts and repair integration boundaries

Deliverables:

- freeze correct and known-wrong workspace fixtures;
- version `workspace-v1`, reward heads, failure taxonomy, and population
  aggregation;
- align combined-task consumers on `documentation/README.md`;
- mark the existing verifier's transform-only standalone-SQL mode as legacy;
  and
- preserve existing `semantic-v1` behavior with differential tests.

Done when one combined task can be installed and inspected consistently by the
generator, training package, and verifier without changing current semantic
scores.

### Phase 1 — Operational: build the artifact-aware L1 package

Deliverables:

- private workspace contract and expected normalized Airbyte intent;
- fresh attempt installer/sealer;
- HCL parser, resource allowlist, graph normalizer, and safety checks;
- deterministic namespace projection; and
- versioned result model with stable codes.

Done when correct and mutated Terraform files receive deterministic structural
results without any cloud service.

### Phase 2 — Operational: execute candidate EL into DuckDB

Deliverables:

- trusted executors for all five backends;
- stream-selection-driven loading into a fresh DuckDB namespace;
- count-compatible and strict typed raw-state heads;
- pre-T raw fingerprints; and
- no direct model-controlled fixture paths.

Done when correct Terraform intent produces all required raw tables and every
declared EL mutation loses the appropriate signal.

### Phase 3 — Operational: run candidate dbt projects locally

Deliverables:

- separate exact-version, lock- and distribution-identity-pinned runtime with
  dbt Core and dbt-duckdb;
- profile injection and namespace mapping;
- parse/compile/run runner with bounded artifacts;
- persistent mart and dbt graph validation;
- post-T raw immutability check; and
- mart comparison through the existing comparator.

Done when the current standalone-SQL transformation interface can be replaced
in artifact-level episodes by an actual dbt project without changing gold.

### Phase 4 — Operational for the official same-state scorer

Deliverables:

- candidate T runs only after candidate EL in the same attempt database;
- official gated reward; the separately named isolated-T diagnostic remains
  optional future curriculum work;
- minimum aggregation over hidden populations;
- clean replay of sealed artifacts; and
- stable operator evidence and solver-visible metrics.

Done when a count-correct/wrong-content EL attempt cannot receive end-to-end
reward and all same-state mutation tests pass.

### Phase 5 — Next: add the interactive local agent environment

Deliverables:

- executable file, Terraform, Airbyte-job, dbt, and read-only SQL actions;
- bounded deterministic observations;
- transcript and artifact capture;
- cancellation-safe sandbox lifecycle; and
- rollout adapter that distinguishes measured failure from no-label failure.

Done when an agent can solve a development task by following the original
two-stage sequence and the sealed workspace replays on hidden populations.

### Phase 6 — Partly operational: implement the L2 protocol proxy

Deliverables:

- offline Terraform/provider strategy selected by prototype evidence;
- resource apply/state behavior;
- connection/job APIs and `check_job_status.py` compatibility;
- local source-service option; and
- equivalence tests between L1 normalized intent and L2 applied state.

Done when the pilot agent uses `terraform init/apply`, triggers jobs, polls
status, and observes raw tables without a real Airbyte or warehouse account.

### Phase 7 — Partly operational: add destination-aware local checks

Deliverables:

- versioned Snowflake, Databricks, and Redshift artifact contracts;
- admitted SQL subset and compatibility macros/rewrites;
- target parser/static checks;
- task admission gate for unsupported real-only semantics; and
- differential fixtures for every locally rewarded destination rule.

The structural contracts, target-dialect parsing, and closed compatibility
rewrites are operational. This phase is complete only after every locally
rewarded rule also has pinned real-destination differential evidence.

### Phase 8 — Planned: integrate training curricula

Deliverables:

- Terraform-only, EL, isolated-T, and combined episode builders;
- held-out family and contamination-safe splits;
- component-head telemetry without hidden-answer leakage;
- invalid-format, compile, sync, mart, timeout, and retry metrics; and
- a resource soak and reproducible lock/image identity.

Done when a small training run improves held-out combined same-state reward and
replays identically from its recorded task, model, prompt, and runtime IDs.

### Phase 9 — Planned: calibrate with sparse real runs

Deliverables:

- canonical private Terraform and dbt solution for each pilot/destination;
- real Snowflake, Databricks, and Redshift runs on pinned matrices;
- local-versus-real logical and physical evidence;
- signed cleanup-complete attestations; and
- a deterministic policy for when changes invalidate and rerun canaries.

Done when both pilots have destination-scoped evidence and ordinary RLVR still
performs zero cloud queries.

## Immediate implementation order

Completed:

1. Add `workspace-v1` models and freeze the reward/failure contract.
2. Resolve the combined-export versus verifier `documentation.md` mismatch.
3. Build the HCL-to-normalized-Airbyte-intent compiler and mutation suite.
4. Connect that intent to trusted local sync executors and one fresh DuckDB.
5. Add the pinned dbt-duckdb runner and persistent-mart verifier.
6. Make dbt consume the candidate-produced raw state and add raw pre/post
   fingerprints.
7. Replay sealed workspaces across the complete graded-population set with
   fresh state and minimum aggregation.

Next:

8. Put candidate execution inside a hard no-network, resource-bounded worker
   and expose the interactive artifact/dbt tool surface.
9. Add the offline Terraform provider/apply and interactive API surface around
   the operational attempt-local Airbyte resource/job state machine.
10. Run destination-specific differential fixtures and sparse real
    certification for Snowflake, Databricks, and Redshift.

A full Airbyte clone or cloud rollout is not required for the initial
artifact-level training signal. That signal requires normalized Terraform
intent, trusted local sync execution, dbt-duckdb, and the same-state invariant.

## First pilot acceptance status

The generated files-plus-PostgreSQL pilot now passes the L1 artifact scorer:

- it is one combined task with real `main.tf`, `dbt_project.yml`,
  `sources.yml`, and persistent model SQL;
- correct HCL produces the exact normalized Airbyte graph;
- selected source streams load into one fresh DuckDB namespace;
- strict raw business rows match private evidence;
- real `dbt parse`, `dbt compile`, and `dbt run --full-refresh` succeed;
- required marts are persistent, use the candidate EL state, and match private
  gold;
- raw physical state remains unchanged after dbt; and
- the same sealed artifacts score `1.0` on every graded population without
  requiring a cloud credential, cloud query, Airbyte instance, or network
  access. The tests do not intentionally invoke the network; hard OS-level
  denial remains a Phase 5 requirement.

Mutation tests verify that wrong stream selection, count-preserving raw-content
corruption, wrong marts, and raw mutation lose the gated reward. One acceptance
test runs two complete local scores concurrently and verifies deterministic
results, eight disjoint population attempts, and cleanup. Before using the
pilot for adversarial production RLVR, run a longer resource soak inside the
Phase 5 hard sandbox and complete its malicious-artifact corpus. The result is
always labelled `artifact_workflow_proxy`, never runtime-certified.

## Completion definition

The full system described in this document is **not complete**: L1 scoring is
operational, while Phases 5, 6, 8, and 9 and the real-differential portion of
Phase 7 remain.

The no-public-DuckDB guarantee is enforced for every schema-3 release. Five
historical schema-2 compatibility freezes under `runs/*/release/public/` still
contain the retired split `__t/warehouse/*.duckdb` layout. They are not
eligible for `workspace-v1` or new publication and are retained only because
the current task explicitly preserves legacy verification paths. A later
archive/re-freeze migration must move them out of public-shaped release paths
before the guarantee can be stated repository-wide without a schema-version
qualification.

The cloud-free training system is complete when generated tasks can be used to
train and evaluate an agent that performs the original Terraform/Airbyte and
dbt workflow locally, receives deterministic same-state EL-gated reward across
hidden populations, and cannot pass through direct loading, count-only
corruption, trusted-T bypass, stale state, or source mutation.

Parity with the real systems has two separate meanings:

- **semantic/workflow proxy parity** is established locally through the
  versioned artifact contracts, DuckDB execution, dbt project behavior, and
  differential fixtures; and
- **runtime parity** is established only through sparse pinned runs on the
  named Snowflake, Databricks, or Redshift matrix.

Train every rollout on local artifact execution. Use cloud compute only to
calibrate the proxy, certify releases, and measure selected checkpoints.

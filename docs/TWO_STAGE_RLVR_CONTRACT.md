# Combined ELT task and cloud-runtime contract

This document defines the one-task, two-phase contract for real ELT-Bench
execution. The distinction between private DuckDB RLVR and sparse cloud
certification is defined in
[`EXECUTION_MODEL.md`](EXECUTION_MODEL.md). A runtime code path or mock test is
not evidence that a destination has passed certification.

One curated `TaskIR` produces one real-runtime solver task. Extract + Load and
Transform are two phases of that task, not independent public units.

```text
seeded source services -> Airbyte -> selected warehouse -> dbt -> mart tables
                                  |                  |
                                  |                  +-- Stage 2 evaluation
                                  +--------------------- Stage 1 evaluation
```

The canonical public identifier is `<task_id>`. New releases must not create
public `<task_id>__el` or `<task_id>__t` directories.

## Runtime export contents

A real-runtime solver or trusted certification implementation receives one
combined task containing the EL and T specifications:

```text
public/<task_id>/
  config.yaml
  data_model.yaml
  schemas/
  documentation/
    README.md                 # authored task specification plus runtime index
    ...                       # Airbyte/source/destination/job guides
  check_job_status.py
  <destination>_credential.json
  elt/main.tf
```

`export/eltbench.py::export_task` produces this complete shape under
`tasks/<task_id>/task/`. Population fixtures remain on the harness side and
are exposed through the backends named by `config.yaml`; no public `sources/`
directory is emitted. The release freezer treats this combined task tree as
the sole public source.

Credentials, Airbyte workspace IDs, and custom connector IDs are installation
state. They must be injected by the benchmark harness and must not be frozen as
real secrets in a public release.

## Real-runtime workflow

The solver performs one end-to-end job in one environment.

### Phase 1: Extract + Load

The solver reads `config.yaml` and `schemas/`, configures Airbyte through
Terraform, triggers the required syncs, and loads the declared source streams
into the task's selected destination namespace: `<task>.AIRBYTE_SCHEMA` for
Snowflake, `<catalog>.<task>` for Databricks, or `<task>` in the connected
Redshift database.

Stage 1 parity remains upstream-compatible and count-only:

- every expected raw table must exist;
- every table must have the frozen expected row count;
- Stage 1 parity is `1` only when all expected tables pass, otherwise `0`.

The private warehouse diagnostics report missing, unexpected, and
count-mismatched tables separately even when an explicitly requested cloud
agent score remains binary. Solver-facing output does not expose expected
values.

### Phase 2: Transform

In the same warehouse environment, the solver reads `data_model.yaml`, creates
a dbt model for every declared mart, and runs dbt. The submission is the
solver's working project plus the resulting warehouse state—not JSON SQL and
not standalone queries.

The evaluator queries each required mart in the destination, orders results
using private `sort_key.json`, and compares them with private gold CSVs. Stage
2 parity is the number of correct marts divided by the number of required
marts. A mart is all-or-nothing under the evaluator's row, column, null, text,
and numeric comparison rules.

Stage 1 and Stage 2 may be recorded separately, but both belong to the same
task ID, source population, solver attempt, and destination state.
`verify-end-to-end` collects Stage 2 parity only after Stage 1 passes; the two
independent verifier commands exist for phase diagnosis. These observations
become a cloud agent score only when the run was explicitly scheduled for that
purpose. Trusted reference runs produce certification evidence instead.

Solver-authored Terraform and dbt run in content-addressed containers with
only the attempt directory mounted. The private release, host environment,
and Docker socket are outside that execution boundary. Warehouse administrator
credentials are harness-only. The solver receives destination-specific scoped
credentials: a Snowflake role/user/warehouse/database, a Databricks principal
restricted to one attempt-only Unity Catalog, or a Redshift user that owns one
fresh attempt database on a cluster/workgroup reserved for exactly that
attempt.

## Certification population profiles

Every population selected by a certification profile is a separate replay of
the same public specification:

```text
same public task + same submitted Terraform/dbt files
                         |
                         +-- development source environment
                         +-- primary source environment
                         +-- resampled source environment
                         +-- counterfactual source environment
                         +-- stress source environment
```

Each selected replay gets fresh source databases, buckets and endpoints; a fresh
warehouse namespace; fresh Airbyte connections; and fresh Terraform and
dbt runtime state. This is required because `full_refresh_append` can duplicate
rows when a destination is reused.

A smoke profile may run `primary` plus one declared adversarial population. A
full parity profile runs all independent graded populations. The resulting
status must name the exact population set; a smoke pass cannot be relabelled as
full-population certification. Ordinary semantic RLVR deploys none of these
cloud environments.

Do not carry `terraform.tfstate`, `.terraform/`, Airbyte connection IDs,
generated credentials, or `target/` between populations.

## Private oracle artifacts

DuckDB defines the private semantics and provides high-volume RLVR evaluation.
It generates and checks expected counts and marts, exercises counterfactual
populations, runs mutation tests, and measures semantic difficulty. Cloud
parity adds runtime evidence without replacing the gold.

Private artifacts may use this layout:

```text
private/<task_id>/
  answer_key/
    table.json
    sort_key.json
    evaluation/sql/
    gt/
    gold/<population>/
  populations/<population>/rendered/
  oracle/
    <population>.duckdb
private/<task_id>__el/reward.json
private/<task_id>__t/reward.json
```

DuckDB must never appear in the public task or submission contract. A public
release must fail if it contains any of the following:

- `*.duckdb`;
- `load_plan`;
- `sql_by_mart`;
- the standalone DuckDB `SELECT` contract.

Private DuckDB success is sufficient only for the scoped semantic claim. Decimal, timestamp,
identifier-casing, null-ordering, and SQL-function differences require a real
Airbyte -> selected warehouse -> dbt parity run before a task is described as
compatible with that destination and runtime matrix.

The compatibility command `score --duckdb` accepts only split releases. The
combined-task command is `semantic score`: it consumes a versioned private
semantic submission, evaluates hidden populations with DuckDB, and reports
independent EL, T, and EL-gated rewards. Cloud verification is not the default
RLVR evaluator.

## Private semantic evidence

The `TaskVariant.EXTRACT_LOAD` and `TaskVariant.TRANSFORM` batteries remain
private curation, RLVR, and calibration tools. Their workspace trees, DuckDB
warehouses, prompts, and reward manifests are evidence, not public task units,
and must not be copied into a public release.

Both batteries remain current and bound to the parent `TaskIR` content hash.
Cloud certification does not replace them. This keeps the contamination
checks, TaskIR validation, population generation, DuckDB gold generation,
attacks, and independent builds useful during and after the migration.

## Current implementation status

The implemented schema-3 release boundary is:

```text
public/<task_id>/          # one combined ELT-Bench task
private/<task_id>/         # oracle, gold, populations, evaluator data
  semantic/task_ir.json    # schema-3.2 private typed scorer input
private/<task_id>__el/     # private semantic Stage 1 evidence
private/<task_id>__t/      # private semantic Stage 2 evidence
release_manifest.json
checksums.sha256           # release integrity; not solver-facing
```

The repository provides these runtime components:

- `training/` implements the separately versioned `workspace-v1` L1 artifact
  proxy: hash-bound package loading, authenticated candidate-only `elt/`
  sealing, semantic HCL-to-Airbyte intent compilation, selected-stream trusted
  loading into a fresh DuckDB namespace, real pinned dbt-duckdb execution on
  that same state, strict raw immutability, private mart comparison, and
  minimum hidden-population reward;
- `export_task()` emits the combined runtime-shaped parent task;
- `emit_variant()` still emits legacy split workspaces for internal batteries;
- `freeze_release()` publishes one schema-3 `public/<task_id>/`, keeps stage
  evidence private, and stores DuckDB only in `private/<task_id>/oracle/`;
- `semantic/package.py` binds the private TaskIR, gold, source roots, and
  release manifest to one content hash; `semantic/scoring.py` runs combined
  attempts in a killable, resource-limited worker and reuses
  `upstream_eval` rather than defining another comparator;
- `export/serve.py` retains the schema-2 DuckDB compatibility scorer and still
  refuses schema-3 combined releases;
- `runtime/source_environment.py` starts and seeds one isolated generated
  population across Postgres, MongoDB, LocalStack S3, REST, and file services;
- `runtime/install.py` creates a fresh solver work directory and injects
  environment credentials without mutating the release;
- `runtime/bootstrap.py` installs an explicitly versioned Airbyte control
  plane, creates a fresh workspace, checks connector definitions, and
  publishes the private declarative REST connector;
- `runtime/snowflake.py` adapts upstream setup SQL to provision a fresh scoped
  principal and warehouse without exposing admin credentials;
- `runtime/databricks.py` first binds the solver credential to its reported
  `session_user()` and requires an explicit attempt-dedicated-principal
  assertion, then grants that principal one fresh or explicitly
  dedicated Unity Catalog, including the schema and volume privileges required
  by Airbyte's direct-load writer;
- `runtime/redshift.py` creates a fresh solver user/database only after an
  explicit assertion that the cluster/workgroup is dedicated to this one
  attempt, and carries an attempt-specific S3 staging prefix;
- `runtime/execution.py` runs submitted Terraform/Airbyte Stage 1 and submitted
  dbt Stage 2 independently inside digest-pinned containers;
- `runtime/evaluation.py` collects Stage 1 counts and Stage 2 evaluator rows
  from Snowflake, Databricks, or Redshift and delegates to the existing
  semantic comparators to produce cloud parity observations.

The schema-3 bundle may be labelled as a combined runtime export, but it must
not be called runtime-certified until the source-service, Airbyte, selected
warehouse, and matching dbt gates pass on a pinned compatibility matrix. The
code has local/mock coverage only so far. A schema-3.2 task can be called
`semantic_rlvr_ready` only after its private package verifies and the semantic
scorer passes replay, isolation, timeout, and mutation checks; this label still
does not imply runtime certification.

Schema-1/2 `__el` and `__t` releases remain readable as compatibility
artifacts. They are deprecated and must not be relabelled or used as evidence
that the Airbyte -> selected warehouse -> dbt contract passed.

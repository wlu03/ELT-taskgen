# Cloud warehouse runtime-certification connectors

ELT-taskgen provides destination-specific export, provisioning, execution, and
parity collection for three real warehouses. These paths support sparse
certification, not high-volume RLVR. The architecture is a semantic proxy
(DuckDB) plus real-runtime adapters. DuckDB applies the semantic proxy contract
and does not emulate a destination. TaskIR and private DuckDB gold define the
semantics; cloud runs do not create or replace gold. See the
[execution model](EXECUTION_MODEL.md) before using this runbook.

| Destination | Airbyte definition | Connector contract | Physical raw/mart namespace | dbt adapter | Live evidence in this checkout |
|---|---|---|---|---|---|
| Snowflake | `424892c4-daac-4491-b35d-c6688ba547ba` | `4.1.2` | `<task>.AIRBYTE_SCHEMA` | `dbt-snowflake` | none recorded |
| Databricks | `072d5540-f236-4294-ba7c-ade8fd918496` | `4.0.2` | `<attempt_catalog>.<task>` | `dbt-databricks` | none recorded |
| Redshift | `f7a7d195-377f-cf5b-70a5-be6b819019dc` | `4.0.7` | `<task>` in `<attempt_database>` | `dbt-redshift` | none recorded |

Snowflake remains the default export target. TaskIR and private DuckDB gold
stay destination-neutral; the runtime `config.yaml`, credential placeholder,
documentation, export bytes, connection, and collector bind the actual
destination. These image tags come from Airbyte's official connector metadata
snapshot on 2026-08-31. Bootstrap rejects a different image instead of using
the current registry value. A pin provides reproducibility; it does not show
that a live cloud run passed.

Pin sources: Airbyte's official, immutable registry snapshot
[`604ae703`](https://github.com/airbytehq/airbyte/tree/604ae703c2671da50439d00857828ea635ad5618),
including its Snowflake, Databricks, and Redshift destination metadata.

The built-in source connectors are pinned from that same snapshot: Files
`0.6.0`, MongoDB v2 `2.0.7`, Postgres `3.8.5`, and S3 `4.15.20`. Those exact
versions are harness-owned metadata, not public `config.yaml` keys. Bootstrap
derives the required connectors from the source sections that are present and
checks the live source and destination image tags before solver resources are
created. This preserves original ELT-Bench's public config shape without
giving up deterministic runtime validation.

## Sync-mode scope

`full_refresh_append` is the only supported Airbyte sync mode, declared in
code as `elt_taskgen.destinations.SUPPORTED_SYNC_MODES`. Every generated
connector stanza declares it, and both the export shape validator and task
bootstrap refuse a config declaring any other mode. Incremental state,
append-dedupe, overwrite, schema evolution, retry/interrupted-sync replay,
dbt tests, and catalog introspection are out of scope pending the pinned
real-runtime certification path. The trusted DuckDB loader's semantic
outcome specifications for this scope — replayed-append doubling, overwrite
as truncate-and-reload, append-dedupe preconditions, and fail-closed schema
drift — live in `tests/test_sync_semantics.py`; they are semantic outcome
contracts, not Airbyte connector certification evidence.

`runtime run-stage2` additionally runs a fail-closed dbt preflight before
`dbt run`: it verifies the pinned dbt-core and adapter versions
(`elt_taskgen.destinations.DBT_CORE_VERSION` and `DBT_ADAPTER_CONTRACTS`,
frozen with the `runtime-images/dbt` lockfile), requires the submitted
profile's output `type` and namespace fields to equal the installed attempt
`config.yaml` values (compared trimmed and case-insensitively), and refuses
session-override keys. Namespace fields written as Jinja templates cannot be
verified statically and are refused. Snowflake connection fields are the
deliberate exception: `account`, `user`, `password`, `role`, and `warehouse`
must use the fixed `ELT_TASKGEN_SNOWFLAKE_*` `env_var` bindings. Arbitrary
environment names and literal Snowflake connection values fail closed.

## Original public Terraform shape and private connector compilation

The solver-facing tree follows original ELT-Bench. `config.yaml` retains the
human-facing source and destination fields, while `elt/main.tf` contains only:

```hcl
terraform {
  required_providers {
    airbyte = {
      source  = "airbytehq/airbyte"
      version = "0.6.5"
    }
  }
}
```

There is no public `connector_config.auto.tfvars.json`, connector-version key,
or prewritten source, destination, or connection resource. The solver reads
the supplied documentation and authors those resources in `elt/main.tf`, as in
the original benchmark. `runtime prepare` injects Airbyte workspace values and
warehouse credentials only into a fresh attempt copy, and refuses a
`--work-dir` under any `runs/` directory or under a release root (usage error,
exit 2, before any credential is read): that copy holds live credentials, and
`runs/` is the tree every ledger, export and review tool reads.

For reproducibility, the exporter separately compiles the readable config into
`private/<task>/answer_key/runtime/airbyte_connector_contract.json`. That file
records the exact modern source/destination payload and connector pins used by
the harness for validation; it is never loaded automatically by Terraform and
is never exposed to the solver.

The private mapper is fail closed. Snowflake `4.1.2` translates the public `account`
field to connector `host`, nests password authentication as
`credentials: {auth_type: "Username and Password", password: ...}`, and
always sets `number_data_type` to `NUMBER(38,9)` instead of accepting the
connector's `FLOAT` default. Databricks `4.0.2` validates the exact OAuth/PAT
discriminator shape, string port, terms flag, catalog, and schema. Redshift
`4.0.7` validates the integer port and complete `S3 Staging` object. Unknown
authentication methods, extra nested fields, missing staging fields,
connector-pin drift, non-destination namespaces, and unsupported sync modes
are refused before Terraform runs.

Private source mappings use the pinned connector JSON contract: Files `0.6.0`
emits `provider.storage = "HTTPS"`; MongoDB v2
`2.0.7` emits `cluster_type = "SELF_MANAGED_REPLICA_SET"` plus a `databases`
array; Postgres `3.8.5` explicitly selects no tunnel, disabled TLS for the
benchmark-owned container, and Standard replication; S3 `4.15.20` declares
record transfer and per-table streams with `format.filetype = "jsonl"`.

The public provider seed is pinned to the same version as original ELT-Bench:
[`v0.6.5`](https://github.com/airbytehq/terraform-provider-airbyte/releases/tag/v0.6.5).
Connector payload versions remain separately pinned because provider version
and connector image version are different compatibility boundaries.

## Cross-warehouse projection contract

The semantic output—mart name, grain, keys, columns, null behavior, and
values—stays fixed. Loading, physical types, identifier rules, and dbt SQL are
destination projections. The expected differences below come from the current
connectors and generated configurations and still require live parity evidence.

| Area | DuckDB oracle | Snowflake runtime | Databricks runtime | Redshift runtime |
|---|---|---|---|---|
| Role | private gold and RLVR | real warehouse | real lakehouse/SQL warehouse | real AWS warehouse |
| Namespace | local table/schema | database.schema.table | catalog.schema.table | connected database + schema.table |
| Transform form | sandboxed semantic SQL | persistent `dbt-snowflake` models | persistent `dbt-databricks` models | persistent `dbt-redshift` models |
| Unquoted identifiers | permissive comparison | normally uppercase | normally lowercase; backticks delimit | normally lowercase; names may be sanitized |
| Nested data | JSON functions | `VARIANT`/`OBJECT`/`ARRAY` | commonly JSON serialized in `STRING` by the current Airbyte path | `SUPER` |
| Decimal target | oracle-defined | constrain to `NUMBER(38,9)` for portable tasks | constrain to `DECIMAL(38,9)` for cross-target parity | constrain to `DECIMAL(38,9)` |
| Isolation | fresh private database file | fresh database, role, user, warehouse | fresh attempt catalog/schema and principal boundary | fresh database/schema, user, deployment boundary, S3 prefix |

Equivalent country extraction illustrates why one semantic model needs
destination-specific SQL:

```sql
-- DuckDB JSON text
json_extract_string(profile, '$.country')

-- Snowflake OBJECT or VARIANT
profile:country::STRING

-- Databricks JSON text
get_json_object(profile, '$.country')

-- Redshift SUPER
profile.country::VARCHAR
```

Portable task admission should also normalize timestamps to UTC at microsecond
precision, cast JSON scalars before comparison, specify null ordering, avoid
unordered string/array aggregation, and use simple ASCII names. Evaluators
must select only declared business columns and ignore connector metadata such
as `_AIRBYTE_*`/`_airbyte_*`.

## Generate a destination runtime export

```bash
elt-taskgen export \
  --workspace /path/to/workspace \
  --task-id <task_id> \
  --destination databricks
```

Use `redshift` or `snowflake` for the other targets. The cloud public task
contains exactly one destination section and one matching Airbyte definition
ID. It is a derived runtime projection of the semantic task, not a replacement
for its private evaluator.

For the distinction between this one-target re-export and the remaining
portability admission, destination compiler, layered identity, and live
certification work, see the programmatic destination retargeting notes in
`DESTINATION_RETARGETING.md` (maintainer-local; not published in this repository). Export
success has status `runtime_exported:<destination>` only.

### Freeze the fixed 15-task example cohort

`tools/regenerate_example_cohort.py` supports `reference` and `release` modes.
The default `reference` mode builds inspectable DuckDB-backed examples and
records `runtime_namespace` in `catalog.json`; it does not create a release.

Reference generation defaults to the bounded `challenging` profile. It raises
evidence-backed mart budgets and retargets each synthetic task to at least
32,768 active PRIMARY rows after executable-lineage pruning, while leaving
WikiDBs' real literal rows untouched. For this fixed example cohort, three
existing WikiDBs selections were replaced by three independent licensed
records chosen for valid marts and larger retained PRIMARY data after that
pruning, inside the adapter's existing 1,000-row-per-table / 60,000-cell
envelope; generation never pads those rows. This is a fixed-cohort selection,
not a new general WikiDBs ranking policy.

The same profile requires at least six typed target columns per mart, rejects a
multi-mart task made from one repeated template, and caps any plan-template
signature at 25% of the fixed cohort. Its plan library adds
`status_cohort_union`, `latest_snapshot`, and `measure_state_distribution`.
Generation-policy v4 also constructs portable
adversarial values—nine-decimal numbers, safe `BIGINT` values above `2^53`,
non-empty Unicode/mixed-case/null-like text, leap/DST/year boundaries, nested
JSON null-versus-missing pairs, weighted multi-hot skew, exact tie runs, and a
NULL argmax winner when row A's controlled bridge label is nullable.
PRIMARY and RESAMPLED rotate the fixed cases rather than copying their row
order.

The validated 2026-09-01 fixed build records 404,618 frozen PRIMARY rows and
391,354 STRESS rows across 15 tasks. It retains 3,974 provided-real WikiDBs
PRIMARY rows, up from 252, and exposes 52 marts / 579 fully classified target
columns across 20 admitted plan-template signatures. The largest signature is
12/52 (23.1%); four marts use the timestamp-plus-unique-ID snapshot shape.

Select the profile explicitly for a reproducible build, or use `standard` for
its default budgets:

```bash
uv run --frozen python tools/regenerate_example_cohort.py \
  --mode reference \
  --difficulty-profile challenging \
  --workspace /private/tmp/elt-taskgen-test1-workspace \
  --output ../ELT-taskgen-test_1.new
```

The catalog records the profile contract and exact frozen scale. Challenging
reference readiness is still deterministic and local: it checks the normal
reference roster plus the six-column/typed-mart contract, but it does not run a
provider or waive later acceptance. This is a structural/data-scale profile,
not an empirical `hard` label or cloud-runtime certification.

Once those exact task identities have been accepted, freeze them with:

```bash
uv run --frozen python tools/regenerate_example_cohort.py \
  --mode release \
  --workspace /path/to/accepted-workspace \
  --catalog ../ELT-taskgen-test_1/catalog.json \
  --destination databricks \
  --output /path/to/databricks-release
```

Release mode creates a temporary hard-linked workspace, re-exports only the
destination-bound public files there, and calls the same `freeze_release()`
and `verify_release()` functions as the main pipeline. It does not modify the
accepted source workspace. Run the command once per destination. Each release
has one public projection per task, and its manifest binds those bytes and the
connector version. This operation performs no Airbyte, dbt, or cloud execution.

Release acceptance adds requirements beyond challenging reference readiness.
Full and transform acceptance require a current, hash-bound, cross-family
independent reconstruction with non-empty SQL for every mart and a score of
1.0 on all five populations. The transform required-mutants gate also requires
at least one executable semantic mutant to lose reward on a graded population.
A shortcut-only catalogue or a mutant killed by a SQL crash is not semantic
evidence. Release mode consumes existing reports and does not create them.

## Databricks

Install the local verification client with:

```bash
uv sync --frozen --extra runtime-databricks
```

An OAuth credential file uses this readable shape:

```json
{
  "hostname": "workspace.cloud.databricks.com",
  "http_path": "/sql/1.0/warehouses/abc123",
  "client_id": "service-principal-client-id",
  "secret": "service-principal-secret",
  "database": "benchmark_catalog"
}
```

A PAT may replace `client_id` and `secret` with `access_token` in the private
runtime credential. The installer translates that alias to Airbyte's
`auth_type: BASIC` plus `personal_access_token` connector shape. Provision one
fresh task schema for an already-created solver principal:

```bash
elt-taskgen runtime provision-databricks \
  --admin-databricks-credential /private/databricks-admin.json \
  --solver-databricks-credential /private/databricks-solver.json \
  --catalog <fresh_attempt_catalog> \
  --schema <task_schema> \
  --solver-principal <service-principal-id> \
  --attempt-dedicated-principal \
  --allow-create-catalog \
  --credential-out /private/databricks-attempt.json
```

Airbyte 4.0.2 executes schema creation during direct-load setup. The solver
therefore receives `USE CATALOG` and `CREATE SCHEMA` on an attempt-only catalog,
plus schema-scoped table, volume, selection, and modification privileges. Use
`--allow-create-catalog` to create it, or
`--existing-dedicated-catalog` only when the named catalog is already empty and
reserved for this attempt. `--attempt-dedicated-principal` asserts that the
principal and its group memberships are not reused by another attempt. The
destination stages through Unity Catalog Volumes and writes final stream tables
directly into the task schema. Before granting anything, the command connects
with the solver credential (without selecting the not-yet-created target) and
requires `session_user()` to equal `--solver-principal`; a
credential/principal mismatch fails before administrator DDL.

The pinned Databricks 4.0.2 Airbyte destination contract does not expose a
Volume name, path, or other Volume selector in candidate Terraform. Its public
configuration ends at catalog (`database`), schema, SQL-warehouse connection,
OAuth, port, terms acceptance, and `purge_staging_data`. Volume creation is an
internal connector staging operation. Consequently, the local Terraform
compiler rejects candidate-added Volume fields and does not report a
candidate-derived Volume intent. The harness-owned real-runtime provisioner
grants schema-scoped `CREATE VOLUME`; the sparse Databricks certification lane
must prove that grant and the connector's staging/cleanup behavior.

## Amazon Redshift

Install the local verification client with:

```bash
uv sync --frozen --extra runtime-redshift
```

Redshift requires both database credentials and S3 staging credentials:

```json
{
  "host": "cluster.example.us-west-2.redshift.amazonaws.com",
  "port": 5439,
  "database": "management_database",
  "username": "runtime_admin",
  "password": "admin-password",
  "s3_bucket_name": "benchmark-staging-bucket",
  "s3_bucket_region": "us-west-2",
  "access_key_id": "AKIA...",
  "secret_access_key": "..."
}
```

Provision a fresh database and cluster-global solver user:

```bash
elt-taskgen runtime provision-redshift \
  --admin-redshift-credential /private/redshift-admin.json \
  --database <fresh_attempt_database> \
  --schema <task_schema> \
  --s3-bucket-path elt-bench/<fresh_attempt_database> \
  --attempt-dedicated-deployment \
  --credential-out /private/redshift-attempt.json
```

Redshift users are cluster/workgroup-wide, and this provisioner performs
database creation from its management connection before the attempt reconnects
to the fresh database. The command therefore requires a cluster/workgroup
reserved for exactly one benchmark attempt. The required
`--attempt-dedicated-deployment` flag is an operator assertion of that boundary;
the command refuses to provision without it. It creates a fresh database owned
by the attempt user, reconnects with the generated solver credential, verifies
that identity and database, and creates the target schema before Airbyte's
connection check. This precreation is required because destination-redshift
4.0.7 selects the configured schema while initializing its JDBC pool.
The configured S3 prefix is carried through installation; its IAM keys must be
policy-restricted to that prefix. The cleartext solver password is kept out of
SQL query history. Recycle the deployment after the attempt instead of sharing
its cluster-global user namespace with another attempt.

## Run sparse certification

These commands consume real infrastructure and are not used for ordinary RLVR
samples. Install Airbyte once on the benchmark host. For each task and
population in a certification plan, provision its destination as shown above,
then run the following destination-neutral sequence.
`bootstrap-task` creates a fresh Airbyte
workspace, validates the required built-in source definitions and the exact
destination connector pin, and publishes the private REST definition when the
task needs one. It does not create sources, destinations, or connections for
the solver.

The harness triggers submitted connections serially. The pinned Databricks
destination runs a connection check at the beginning of each sync and reuses
temporary table names inside the shared destination schema; concurrent starts
can race on those objects and yield paired table-missing/table-exists errors.

```bash
elt-taskgen runtime bootstrap-task \
  --release /path/to/release \
  --task-id <task_id> \
  --airbyte-url http://localhost:8000/api/public/v1/ \
  --airbyte-credential /private/airbyte-base.json \
  --credential-out /private/airbyte-task.json

# `--airbyte-server-url` is installed into the solver's task config and is
# the URL the Terraform runner dials at stage 1. A host.docker.internal URL
# (local abctl) is reachable from the runner ONLY on the proxy-bridge lane,
# so this instruction requires `run-stage1 --sandbox-lane proxy-bridge` below.
elt-taskgen runtime prepare \
  --release /path/to/release \
  --task-id <task_id> \
  --population primary \
  --work-dir /tmp/attempt/task \
  --environment-dir /tmp/attempt/sources \
  --airbyte-credential /private/airbyte.json \
  --airbyte-server-url http://host.docker.internal:8000/api/public/v1/ \
  --destination-credential /private/destination-attempt.json

elt-taskgen runtime source-up \
  --release /path/to/release \
  --task-id <task_id> \
  --population primary \
  --environment-dir /tmp/attempt/sources

# This command returns only after the Airbyte ingress and the local kind API,
# scheduler, and controller manager have remained healthy for 30 consecutive
# seconds. Any failed sample resets the stability window. The default overall
# wait is 180 seconds with 5-second polling; a timeout fails closed, attempts to
# remove the just-started source stack, and reports if cleanup fails. Use
# `--airbyte-readiness-timeout`,
# `--airbyte-stability-window`, and `--airbyte-readiness-poll-interval` only
# when the host needs different bounded timings. The window must be shorter
# than the timeout, and polling cannot exceed half the window.

# Stage 1 runs `terraform apply` inside the runner, and the Airbyte provider
# must reach the control plane, so `run-stage1` needs `--sandbox-lane
# proxy-bridge`: the runner joins the pre-provisioned `elt-proxy-bridge`
# network and maps host.docker.internal to the host gateway. Provision the
# network once per host: `docker network create elt-proxy-bridge`. Without an
# explicit flag `run-stage1` derives `proxy-bridge` from its Airbyte URL
# (`--airbyte-url`, else the installed `Airbyte.config.server_url`); the
# runner's default lane, `--sandbox-lane none`, attaches no network and adds
# no host mapping, so the installed `--airbyte-server-url` is unreachable and
# the apply can only fail. Stage 2 defaults to `none` too. A real warehouse
# run must explicitly select the separate outbound bridge; provision it once:
# `docker network create elt-cloud-egress`.
elt-taskgen runtime run-stage1 \
  --work-dir /tmp/attempt/task \
  --runner-image <terraform-runner@sha256:digest> \
  --sandbox-lane proxy-bridge \
  --airbyte-url http://localhost:8000/api/public/v1/ \
  --airbyte-credential /private/airbyte-task.json

elt-taskgen runtime verify-stage1 \
  --release /path/to/release \
  --task-id <task_id> \
  --destination-credential /private/destination-attempt.json \
  --certification-strict

elt-taskgen runtime run-stage2 \
  --work-dir /tmp/attempt/task \
  --runner-image <matching-dbt-runner@sha256:digest> \
  --sandbox-lane cloud-egress \
  --destination-credential /private/destination-attempt.json

# --destination-credential is required on this command for Snowflake. Omit it
# for the current Databricks and Redshift profiles, whose scoped values are
# already installed literally.

elt-taskgen runtime verify-stage2 \
  --release /path/to/release \
  --task-id <task_id> \
  --destination-credential /private/destination-attempt.json \
  --certification-strict

# Run only after the normal end-to-end verdict. This deliberately appends a
# second full snapshot and leaves the raw namespace unsuitable for Stage 2.
elt-taskgen runtime resync-stage1 \
  --work-dir /tmp/attempt/task \
  --airbyte-url http://localhost:8000/api/public/v1/ \
  --airbyte-credential /private/airbyte-task.json

elt-taskgen runtime verify-stage1 \
  --release /path/to/release \
  --task-id <task_id> \
  --destination-credential /private/destination-attempt.json \
  --expected-repetitions 2 \
  --certification-strict

elt-taskgen runtime source-down \
  --release /path/to/release \
  --task-id <task_id> \
  --population primary \
  --environment-dir /tmp/attempt/sources
```

Use the exact Terraform and destination-specific dbt references recorded in
`runtime-images/manifest.json`; a different digest is development evidence,
not evidence for the frozen certification matrix. Stage 1 prints its UTC
execution window, canonical admitted-workspace aggregate digest, exact Airbyte
connection/job map, and Terraform-state digest, lineage, serial, and addressed
connection-resource roster. This aggregate covers every regular workspace file
except enumerated VCS/runtime caches and Terraform/dbt outputs; receipt output
contains no source bytes or per-file hashes. Stage 2 prints its UTC window
and canonical dbt-input-tree digest, binds the installed namespace and physical
container during preflight, and uses a new harness-owned target directory for
every invocation before reporting the dbt invocation id and exact
`run_results.json` digest. Keep those receipts private with the attempt. They
are inputs to the typed evidence API, not a sealed attestation by themselves.

The runtime infers the destination from the released task and rejects a CLI
destination assertion that disagrees. `--snowflake-credential` remains a
backward-compatible Snowflake-only alias. Databricks verification reads the
Unity Catalog from the credential; `--catalog`/`--physical-container` is an
explicit override and must agree with it.

The upstream-compatible EL reward is still count-only. For a certification
run, `--certification-strict` is required: it reconstructs exact typed raw
relations from the frozen rendered sources, reads at most the expected count
plus one row from each destination table, permits only the four metadata
columns emitted by the pinned Airbyte destination protocol, and requires exact
logical fingerprints. The mart side uses the same declared logical types, so
equivalent UTC timestamp/decimal/JSON driver representations compare equally
without numeric tolerances or text folding. The strict end-to-end command
gates dbt evaluation on this content verdict as well as the compatibility
count verdict.

Warehouse verifier connections are bounded rather than inheriting driver
defaults that can wait for many minutes: Databricks limits socket waits and
its retry window, Redshift makes at most three short-timeout connection
attempts with fixed two-second cold-start delays, and Snowflake bounds login,
socket, and network operations. Driver diagnostics are still replaced with
secret-free runtime errors.

`resync-stage1` is the executable `full_refresh_append` protocol probe. It
does not reapply Terraform: it rereads the existing state, proves those IDs
are still the complete connection set in the attempt workspace, triggers each
once, and follows the newly returned job IDs. Verification with
`--expected-repetitions 2` requires two exact copies of every source row, not
only an aggregate count that is twice as large. Run it last, then reset or
discard the isolated attempt
namespace before any other population or dbt run.

Connection-status inspection does not trust the jobs endpoint's array order.
Deployments have returned both oldest-first and newest-first even
when `orderBy=createdAt|DESC` was requested. The client selects each
connection's newest job by its parsed creation time and job id and fails closed
when the returned metadata cannot establish an unambiguous latest state.

Certification fixtures must themselves fit the portable logical contract.
In particular, a `decimal` source value with more than nine fractional digits
cannot certify against the fixed `DECIMAL(38,9)`/`NUMBER(38,9)` contract; the
strict reference build fails closed instead of silently accepting warehouse
rounding.

Airbyte bootstrap is fail-closed: it verifies every built-in source connector
used by the task plus Snowflake `4.1.2`, Databricks `4.0.2`, or Redshift
`4.0.7`. It does not mutate the Airbyte connector registry. The Airbyte
deployment must already expose every pinned connector image; a missing or
different version stops the attempt before solver execution.

Build definitions and immutable-digest instructions for the Terraform,
dbt-snowflake, dbt-databricks, and dbt-redshift execution images are in
[`runtime-images/README.md`](../runtime-images/README.md). Building or pushing
those images is a harness setup step, not task generation.

## Certification status

All connector and evaluator tests are local and mocked. They verify credential
handling, namespace qualification, SQL generation, privilege scoping, and
selected partial-provision cleanup paths. Complete attempt teardown and
post-cleanup resource accounting remain unimplemented. These tests do not
verify account networking, Airbyte connector availability, IAM policies,
Unity Catalog storage, Redshift COPY staging, or DuckDB-to-cloud SQL parity.
A task receives scoped runtime certification only after a real Airbyte → target
warehouse → matching dbt adapter run under a pinned matrix. Local or mock
success has status `runtime_unit_tested:<destination>`. A smoke run has status
`runtime_smoke_certified:<destination,matrix,population>`; full parity requires
the explicitly named population set. This checkout contains no such live
evidence.

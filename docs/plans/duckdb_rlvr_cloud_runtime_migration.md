# Migration plan: DuckDB RLVR with three real warehouse targets

This plan implements the execution model in
[`../EXECUTION_MODEL.md`](../EXECUTION_MODEL.md). It preserves the existing
curation and DuckDB oracle system, uses DuckDB as the permanent high-volume
RLVR environment, and uses Snowflake, Databricks, and Amazon Redshift as sparse
real execution targets.

The executable parity case registry, evidence classes, and test rollout are
specified in `semantic_runtime_parity.md` (maintainer-local; not published in this repository).
The artifact-level local training environment that evaluates candidate
Terraform and dbt files on one candidate-produced DuckDB state is specified in
[`cloud_free_elt_agent_rlvr.md`](cloud_free_elt_agent_rlvr.md).

A CLI path or mock test does not complete a phase. Real-runtime completion
requires a recorded, pinned cloud attestation.

## Baseline

Already present:

- destination-neutral TaskIR and deterministic populations;
- backend rendering for files, Postgres, MongoDB, REST, and S3;
- DuckDB gold, independent builds, mutations, and semantic comparators;
- a supported combined-task schema-3.2 DuckDB semantic scorer;
- one combined schema-3 public task export for a selected destination;
- source-service and Airbyte bootstrap modules;
- Snowflake, Databricks, and Redshift provisioning/evaluation adapters;
- separate Stage 1 and Stage 2 runtime execution and verification commands;
- reproducible Terraform and all three dbt runner-image definitions; and
- layered semantic-release, per-task runtime-bundle, and matrix certification
  identifiers;
- a non-certifying structural parity registry, typed logical fingerprinting,
  and fail-closed verifier tests; and
- local and mocked runtime tests.

Not yet present:

- private canonical Terraform and dbt solutions for all three destinations;
- physical-schema collectors for the TaskIR-derived required parity case sets;
- a certification identity that also binds task semantics, exact populations,
  probes, canonical solution, and evidence contract;
- one evidence-driven fail-closed certification state machine and signed
  immutable attestation;
- complete attempt cleanup and cost controls;
- current real-cloud parity evidence.

The current programmatic retargeting boundary and acceptance checklist are
maintained in `DESTINATION_RETARGETING.md` (maintainer-local; not published in this repository).

## Phase 0 — Freeze terminology and baseline evidence

Status: documentation complete; reference evidence still required.

Deliverables:

- adopt `oracle_validation`, `semantic_rlvr`, and
  `runtime_certification` across current docs;
- freeze representative current TaskIR, population, mutation, and reward
  snapshots before changing scorer APIs;
- record the current schema-2 scorer behavior for backward compatibility; and
- define the exact population aggregation rule and certification labels.

Done when the documentation distinguishes semantic reward, cloud
compatibility, and agent benchmark performance.

## Phase 1 — Provide DuckDB semantic scoring through a stable API

**Implemented 2026-08-31 (core path).** Release schema 3.2 now freezes a
private, content-hash-bound TaskIR; `semantic/{models,package,scoring}.py`
provides a versioned combined submission/result API; and
`elt-taskgen semantic score` evaluates independent EL and T heads across
hidden populations before applying the EL gate. Execution is isolated in a
killable child with timeout, DuckDB memory/thread/spill limits, external access
and extension loading disabled, single-query validation, duplicate-key and
payload limits, and bounded mart output. The legacy schema-2 scorer was not
relaxed.

Remaining Phase-1 certification work is broader differential validation:
replay archived attempts from each source family, run the complete local suite,
and exercise corpus-scale concurrency/resource envelopes. Those checks extend
implementation validation but do not require a different scoring contract.

Add a dedicated package, initially as a facade over the existing trusted
components:

```text
src/elt_taskgen/semantic/
  models.py
  package.py
  scoring.py
```

Work items:

1. Define versioned EL and T submission schemas.
2. Reuse `calibration.execute_load_plan`, the sandboxed DuckDB loader, and
   `upstream_eval`; do not fork comparison semantics.
3. Preserve count-only EL as `upstream_compat_reward`.
4. Add optional typed row-multiset digests as diagnostics, not as an
   unannounced reward change.
5. Run T submissions against hidden populations in a resource-limited DuckDB
   process.
6. Emit per-population EL, T, gated, timeout, and invalid-submission results.
7. Add a new combined-task scorer instead of relaxing the legacy schema-2
   scorer's fail-closed check.

Tests:

- differential results against all current calibration fixtures;
- malformed input, traversal, `ATTACH`, filesystem/network access, timeout,
  and memory-limit tests;
- mutation tests verifying that each declared wrong solution loses reward; and
- repeated deterministic execution in the pinned DuckDB environment.

Done when every archived semantic attempt can be reproduced through the new
API and one schema-3 task can be trained without a warehouse credential.

## Phase 2 — Separate semantic and runtime identities

**Implemented as a dependency chain in release schema 3.4.** The semantic ID
excludes destination-specific private runtime contracts, each runtime bundle
binds the semantic ID plus its public/private runtime bytes, and certification
binds that runtime bundle plus its execution matrix. Schema-3.3 artifacts are
still verified under their historical rules but cannot begin a new
certification until re-frozen. Exact live evidence collection and provenance
binding remain part of the certification-orchestrator work.

Introduce a new release schema with:

```text
semantic_release_id
runtime_bundle_id per destination
certification_id per destination and execution matrix
```

Update:

- `export/release.py` for layered manifests and backward readers;
- `export/eltbench.py` for three derived runtime projections;
- `semantic/package.py` and `semantic/scoring.py` for the permanent scorer,
  while `export/serve.py` remains the legacy schema-2 compatibility path;
- `cli.py` for explicit `semantic score` and `certify` interfaces; and
- release verification for cross-layer hashes and private/public leak checks.

Tests:

- schema-1/2/3 artifacts retain their original meaning;
- a new release round-trips and detects tampering;
- one semantic task creates three distinct runtime bundle IDs; and
- no cloud public bundle contains DuckDB, hidden populations, gold, private
  evaluator SQL, or semantic answer instructions.

Done when retargeting a task to another warehouse does not change its semantic
identity or gold.

## Phase 3 — Generate private canonical runtime solutions

Sparse certification must execute a trusted reference implementation. An
arbitrary agent attempt measures the agent, not whether the task is runnable.

Create private destination projections:

```text
private/<task_id>/certification_solution/
  snowflake/
  databricks/
  redshift/
```

Each projection contains complete source/connection Terraform, one dbt model
per mart, and an injectable destination profile. It contains no administrator
credentials or static attempt IDs.

Implement explicit destination compilers from structured TaskIR/MartPlan.
Maintain a feature matrix for identifiers, decimals, timestamps, JSON, null
ordering, dates, joins, aggregates, windows, and text limits. Reject a runtime
projection when the target cannot represent the task. Do not transpile
arbitrary oracle SQL without explicit rules.

Tests:

- Terraform validation and exact stream coverage;
- dbt parse/compile in digest-pinned images;
- reference SQL for high-risk types and functions on all destinations; and
- leak scans verifying that the canonical solution remains private.

Done when a small files/Postgres task and a mixed five-backend task compile for
all three destinations.

## Phase 4 — Harden each real runtime

Status: partially implemented. Source and destination connector pins plus
reproducible runner definitions exist; live matrix validation, full lifecycle
cleanup, and immutable attestations remain.

Apply shared runtime controls first:

- retain the pinned source/destination connectors, Terraform provider, dbt
  Core/adapters, and drivers; select fixed Airbyte and `abctl` versions; and
  record the actual published runner-image digests in one matrix;
- make namespace reset, source startup, sync completion, dbt execution,
  evaluation, and cleanup fail closed;
- inject one credential source into both host actions and the installed solver
  task;
- allocate fresh source services, Airbyte workspace/connections, Terraform
  state, dbt target, and destination namespace per attempt/population; and
- reject cached evaluator output and stale state.

Then apply destination-specific controls:

### Snowflake

- validate the pinned destination connector `4.1.2` on a real account;
- replace password-only attempt provisioning with key-pair or another
  non-interactive strong-authentication path;
- normalize quoted/unquoted database and schema identifiers;
- use a fresh database, role, user, and warehouse per attempt; and
- certify decimal, `VARIANT`, and timestamp behavior.

### Databricks

- verify service-principal identity before grants;
- reserve a fresh Unity Catalog or otherwise verify attempt-only isolation;
- grant Volume and schema privileges required by the pinned connector; and
- certify JSON-string, casing, timestamp, and Delta-table behavior.

### Redshift

- require an attempt-dedicated cluster/workgroup or an equivalently verified
  isolation mechanism;
- use a fresh database, solver user, and policy-restricted S3 prefix;
- certify `COPY`, `SUPER`, decimal scale, identifier sanitization, and large
  text limits; and
- remove the attempt user and staged S3 objects during cleanup.

Done when each provisioner has fail-injection and cleanup tests and can produce
an isolated credential without exposing administrator secrets.

## Phase 5 — Add one certification orchestrator

Wrap the existing runtime subcommands in one resumable, fail-closed state
machine:

```text
provision
-> bootstrap Airbyte
-> prepare and start sources
-> run canonical Stage 1
-> verify Stage 1
-> run canonical Stage 2
-> verify Stage 2
-> write attestation
-> cleanup
```

Suggested interface:

```text
elt-taskgen certify plan
elt-taskgen certify run
elt-taskgen certify verify
elt-taskgen certify cleanup
```

The attestation records hashes and versioned evidence listed in
`EXECUTION_MODEL.md`, redacts secrets, and signs or checksum-binds the complete
record. Stage 2 must never run after Stage 1 failure. Cleanup runs after
success, failure, timeout, or interruption.

Tests:

- mocked successful runs for all three destinations;
- failure injection at every state transition;
- idempotent resume and cleanup;
- credential redaction and attestation tamper detection; and
- one real pilot per destination.

Done when one command yields either a verifiable attestation or a classified
failure and always accounts for remaining resources.

## Phase 6 — Certify sparsely and deterministically

Use cloud compute only for evidence that local execution cannot provide.

Policy:

- every pull request runs the complete DuckDB suite and no cloud jobs;
- a runtime/compiler change runs two canaries on each affected destination;
- a release candidate uses deterministic set cover over source backends,
  source families, size tiers, SQL features, and risky data types;
- sampled cloud tasks run `primary` plus one adversarial population;
- at least one canary per destination runs every graded population;
- a failure expands testing to all tasks sharing the failing feature
  fingerprint; and
- unchanged certification IDs reuse prior evidence.

The plan generator must report uncovered feature buckets rather than silently
claiming full release coverage.

Done when the certification selection can be reproduced from release metadata
alone and its status label states exactly what was sampled.

## Phase 7 — Pilot and adopt

Pilot A:

- files and Postgres;
- one join/rollup mart; and
- small deterministic populations.

Pilot B:

- files, Postgres, MongoDB, REST, and S3;
- JSON, decimal, timestamp, null, and identifier risks; and
- multiple joins and aggregations.

For each pilot:

```text
DuckDB oracle and semantic reward pass
-> Snowflake Airbyte/dbt parity passes
-> Databricks Airbyte/dbt parity passes
-> Redshift Airbyte/dbt parity passes
-> attestations verify after cleanup
```

Adopt the new path only when:

- the new semantic scorer matches archived reward behavior;
- no private or DuckDB artifact leaks into a cloud solver bundle;
- both pilots pass Stage 1 and Stage 2 on all three destinations;
- connector/runtime changes invalidate the correct attestations;
- historical releases remain readable without being relabelled; and
- documentation contains no unscoped certification claims.

## Immediate implementation order

The core Phase-1 scorer, layered Phase-2 identities, and the first structural
parity-verifier slice are complete. Continue in this order:

1. freeze representative archived attempts from dbt, SynSQL, dlt,
   SchemaPile, and WikiDBs as differential fixtures;
2. run the complete local suite and a concurrent-attempt resource soak;
3. close any failures without changing `upstream_eval` semantics;
4. add physical-schema and static-contract collectors to the now-derived,
   digest-bound task-specific parity case manifests;
5. generate private destination-specific canonical Terraform and dbt
   solutions for sparse certification;
6. replace caller booleans with authenticated, signed, population-bound
   evidence; and
7. certify the two pilot tasks on pinned Snowflake, Databricks, and Redshift
   matrices before widening coverage.

This sequence uses local execution for high-volume RLVR and requires every
cloud claim to be sparse, versioned, and auditable.

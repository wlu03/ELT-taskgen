# Execution model: DuckDB RLVR and real warehouse runtime

This document defines the architecture for generated ELT-Bench tasks. It
distinguishes three separate functions:

1. private answer generation;
2. inexpensive RLVR scoring; and
3. real Airbyte, warehouse, and dbt execution.

DuckDB is the permanent private semantic runtime for generated tasks and
high-volume RLVR. Snowflake, Databricks, and Amazon Redshift are the real
execution targets for compatibility certification and final agent measurement.

Cloud results are evidence that a generated task can execute on a particular
runtime matrix. They do not replace or redefine the immutable DuckDB gold.

## Architecture classification

The architecture is a semantic proxy (DuckDB) plus real-runtime adapters. The
DuckDB scorer applies the declared semantic proxy contract; it does not emulate
Snowflake, Databricks, or Amazon Redshift. It also does not emulate their SQL
dialects, type systems, catalogs, transactions, wire protocols, or error
surfaces, and no vendor emulator endpoint exists in this repository. Runtime
adapters connect to the three real services for sparse certification. No cloud
certification attestation is currently recorded in this checkout, so any
compatibility claim must use the scoped status vocabulary below.

## Architecture

```text
                         one TaskIR and population set
                                      |
                 +--------------------+--------------------+
                 |                                         |
       private semantic path                       real runtime path
                 |                                         |
    generated records -> DuckDB                    source services
                 |                                         |
       gold, attacks, RLVR reward             Airbyte -> destination -> dbt
                 |                                         |
                 +-------------- compare ------------------+
                                      |
                         certification attestation
```

The same task has one semantic identity. Destination exports are projections
of that task, not independent tasks with independent meanings.

## The three execution modes

| Mode | Frequency | Executes | Purpose | What it proves |
|---|---:|---|---|---|
| `oracle_validation` | every generated task and population | trusted loaders and transforms in DuckDB | freeze raw counts, mart gold, attacks, and determinism evidence | the task has a coherent expected answer |
| `semantic_rlvr` | every training attempt | an untrusted submission in the private DuckDB semantic-proxy scorer | provide cheap EL and T learning signals | semantic agreement with the generated task under the declared proxy contract |
| `runtime_certification` | selected immutable task/runtime matrices | trusted source services, Airbyte, Snowflake/Databricks/Redshift, and the matching dbt adapter | establish real-system compatibility | that the certified task, populations, versions, and destination passed end to end |

An optional real-cloud agent run is an `agent_benchmark_score`. It measures an
agent on the actual benchmark and is neither oracle generation nor runtime
certification.

`runtime run-stage2` opens with a fail-closed dbt preflight: the pinned
dbt-core/adapter versions, the submitted profile's adapter type, and the
installed attempt namespace are verified before any dbt model runs (see the
"Sync-mode scope" section of
[WAREHOUSE_CONNECTORS.md](WAREHOUSE_CONNECTORS.md)). Containers default to the
no-network lane; a real dbt warehouse run must explicitly select the separate
`cloud-egress` bridge. Snowflake connection fields must use the fixed profile
environment bindings populated from an explicit, attempt-matching scoped
credential; secret values are not placed in Docker argv.

### What semantic RLVR does not prove

DuckDB scoring does not prove that an agent can:

- provision Airbyte resources with Terraform;
- authenticate to a cloud warehouse;
- survive connector, network, permission, or job failures;
- write SQL in every destination dialect;
- manage warehouse state and cleanup; or
- operate within a real end-to-end time and cost budget.

Sparse runtime certification and final benchmark runs measure those behaviors.
The RLVR reward must be named `semantic_*`; it must not be reported as a cloud
or end-to-end execution score.

## One task, two phases

Each generated task keeps the original ELT-Bench boundary: one solver task has
an Extract + Load phase followed by a Transform phase. EL and T may be scored
separately, but public cloud bundles must not become unrelated `__el` and
`__t` tasks.

| Phase | Semantic RLVR | Real execution |
|---|---|---|
| Stage 1: EL | validate the submitted normalized load intent against TaskIR and privately materialized raw-table expectations | execute the submitted Terraform/Airbyte workflow, then query raw warehouse tables |
| Stage 2: T | run the submitted mart logic in the sandboxed DuckDB semantic-proxy environment and compare it with private gold | execute the destination-specific dbt project and query persistent mart tables |

Stage 1 retains an upstream-compatible count-only reward: all required raw
tables must exist at the exact expected counts. Stronger row-content digests
may be used as private admission diagnostics, but must be reported separately
instead of silently changing the upstream reward.

Stage 2 scores the fraction of required marts that exactly match private gold
after applying the declared sort keys and canonical type comparison.

The gated semantic reward is:

```text
semantic_reward = 0                         if semantic_el_reward != 1
semantic_reward = semantic_t_reward         otherwise
```

Population aggregation must be declared in the release manifest. The
recommended conservative default is the minimum reward across the independent
graded populations, with declared byte-equivalent rearrangements deduplicated.

## Artifact and trust boundaries

The schema-3.5 release and its separate certification store have these
boundaries:

```text
release/
  public/<task_id>/                       # one solver-facing runtime task
  private/<task_id>/
    answer_key/
    populations/
    oracle/<population>.duckdb
    semantic/task_ir.json
    provenance/ingest_provenance.json      # exact upstream selector/revision/digest
    reports/difficulty.json                # required + empirical if certified
  private/<task_id>__el/reward.json
  private/<task_id>__t/reward.json
  sandbox_attestation.json                 # certified release mode only
  release_manifest.json
  checksums.sha256

external-certification-store/              # never frozen into the release
  <certification_id>/
    attestation.json
    ... sealed state-machine evidence
```

These boundaries are mandatory:

- DuckDB files, population fixtures, evaluator SQL, expected counts, and gold
  remain private.
- A cloud solver bundle contains no `.duckdb`, `load_plan`, `sql_by_mart`, or
  hidden expected output.
- A semantic RLVR evaluator may consume a private DuckDB database without
  exposing its bytes to the solver.
- Credentials and attempt identifiers are installed at runtime and are never
  frozen into a release.
- The public `config.yaml` and provider-only `elt/main.tf` mirror original
  ELT-Bench. `main.tf` pins Airbyte Terraform provider `0.6.5`; it contains no
  prewritten resources or auto-tfvars. Exact connector versions and the
  deterministic connector translation are private harness metadata under
  `answer_key/runtime/`, while only an isolated attempt copy receives secrets.
- A destination compiler may change physical SQL and configuration, but not
  the task's declared grain, keys, columns, null rules, or expected values.

## Identity and certification

One release identifier cannot represent every layer of a destination-neutral
task. Since release schema 3.3, the manifest records three identities; schema
3.4 places them in a dependency chain. They are computed at freeze and
recomputed by `verify` from manifest-recorded inputs only
(`export/release.py`; the state machine and immutable attestation record live
in `export/certification.py`):

```text
semantic_release_id
  = hash(TaskIR content hashes + destination-independent private population,
         gold/evaluator/provenance bytes + scorer versions + roster digest)

runtime_bundle_id
  = hash(semantic_release_id + task id + destination
         + exported public runtime bytes + private runtime contract bytes)

certification_id
  = hash(runtime_bundle_id + certification matrix: connector pins,
         runner-image digest, Airbyte/abctl pins, warehouse session shape)
```

Schema 3.5 also binds the typed ingest-provenance digest into `release_id`.
Certified publication requires one deterministic, lineage-bound record per
task. Legacy or development omission remains explicit and does not change
TaskIR semantics. Schema 3.3 manifests remain readable and use their original
verification algorithm. They must be re-frozen before certification because
their semantic ID included the destination-specific private connector contract
and their runtime ID did not depend on the semantic ID.

### Release modes

The `elt-taskgen release` CLI defaults to `certified`. The same is true when
`elt-taskgen pipeline` receives `--release-dir`. This mode refuses to freeze
unless every selected task has current empirical solver evidence for exactly
the EL and T variants and the operator supplies
`--sandbox-attestation <path>`. The freezer verifies and byte-pins that sealed
record, requires an attesting tier A or B, contamination enforcement, a real
clean-mount scan, freshness, and the correct release/run binding. A declared
tier B host also needs observed corroborating runtime and cgroup evidence.

Because the release id is computed by the freeze itself, the pre-freeze record
is minted without a run id and the freezer binds and reseals its immutable
copy. On the isolated Linux host, use the built-in bootstrap command (the
agents config must pin the sandbox that host actually reports):

```bash
elt-taskgen runtime certification attest-unbound \
  --agents-config /secure/config/agents-tier-a.yaml \
  --mount-root /secure/run/corpus \
  --out /secure/run/sandbox-attestation.json
elt-taskgen pipeline --workspace /secure/run/corpus \
  --agents-config /secure/config/agents-tier-a.yaml \
  --release-dir /secure/releases/current \
  --sandbox-attestation /secure/run/sandbox-attestation.json
```

`attest-unbound` applies the complete label-bearing tier, contamination,
mount, corroboration, freshness, seal, and configured-pin checks before it
creates output through the crash-safe exclusive publisher. It never
overwrites an existing output. The later `runtime certification attest
--release ...` command creates a new release-bound observation for a
post-freeze cloud-runtime certification attempt.

`--development-release` is the explicit unlabelled alternative. It carries no
sandbox-attestation digest and makes no isolation or real-runtime claim.
Selection and release still require empirical EL+T evidence unless the
operator also passes the development-only
`--allow-structural-difficulty` override. In either mode, any difficulty file
that is present must name the task, match its current content hash, parse under
the closed schema, and reproduce the digest recorded in the release manifest.

The certified release mode establishes that label-bearing release assembly ran
under an acceptable isolation boundary. It does not by itself prove that
Airbyte, a destination warehouse, or dbt ran successfully. That separate fact
comes only from a sealed `CERTIFIED` record in the external runtime
certification store.

The execution matrix must record at least:

- ELT-taskgen and ELT-Bench commits;
- Airbyte and `abctl` versions;
- every digest-pinned source-service image;
- every source and destination connector ID and image version;
- Terraform provider and runner-image digests;
- dbt Core and adapter versions;
- destination engine/version and relevant session settings;
- task and population hashes;
- physical isolation identifier;
- Stage 1 counts, Stage 2 result digests, and failure classification; and
- cleanup outcome.

It must not contain secrets or reusable account credentials.

### Status vocabulary

Use only scoped labels:

- `generated`
- `oracle_validated`
- `semantic_rlvr_ready`
- `runtime_exported:<destination>`
- `runtime_unit_tested:<destination>`
- `runtime_smoke_certified:<destination,matrix-id,population>`
- `runtime_parity_certified:<destination,matrix-id,population-set>`
- `release_sample_certified:<policy-id>`
- `agent_benchmark_score:<destination>`

Never use bare `supported`, `end-to-end certified`, or `runtime-certified`
without naming the destination, matrix, and population coverage.

### Empirical and runtime-certified difficulty

Structural difficulty remains useful for authoring, but it is not an observed
solver result. Release-quality empirical evidence is bound to the TaskIR
content hash and active solver-roster fingerprint, contains measured tiers,
and covers exactly the graded EL and T variants. Selection rejects missing,
stale, partial, empirically impossible, or empirically trivial evidence rather
than falling back silently to a structural band.

After live certification has completed, this command combines the semantic and
runtime evidence:

```bash
elt-taskgen runtime certify-difficulty \
  --release /path/to/release \
  --task-id <task_id> \
  --certification-store /secure/certifications \
  --out /secure/reports/<task_id>.runtime-certified-difficulty.json
```

It first verifies the frozen release and requires `release_mode: certified`, a
bound sandbox-attestation digest, and release-bound empirical difficulty at the
released content hash. It then resolves the manifest's exact
`certification_id`, requires its state to be `CERTIFIED`, verifies the sealed
runtime attestation, and checks its task, release, and runtime-bundle bindings.
Only then does it emit per-variant solver pass rates, an empirical
easy/medium/hard band, destination and population coverage, all layered
identities, the runtime-attestation digest, and a canonical evidence seal.
`--workspace` is a compatibility fallback for a release that predates embedded
difficulty evidence. The command validates and promotes existing evidence; it
does not execute a runtime or convert a development release or console output
into certified evidence.

## Cloud-compute policy

Normal RLVR rollouts perform zero cloud queries. Real warehouse execution is
reserved for changes or checkpoints that can invalidate compatibility:

| Event | Required execution |
|---|---|
| ordinary training attempt | semantic RLVR only |
| task generation or curation change | all declared populations in DuckDB |
| destination compiler, connector, or runtime change | feature-covering canaries on each affected destination |
| release candidate | deterministic feature-covering sample on all three destinations |
| final benchmark measurement | real agent execution on the selected destinations |

Certification evidence may be reused only when all inputs to
`certification_id` are unchanged. A connector, runner image, dbt adapter,
TaskIR, population, evaluator, or warehouse-setting change invalidates the
affected evidence.

Tinker may orchestrate sampling and training around this policy, but it does
not change any reward or certification meaning. The researched integration is
described in
[`plans/tinker_elt_rl_environment.md`](plans/tinker_elt_rl_environment.md).

## Reviewed ELT-Bench snapshot

The reviewed upstream checkout is
`fcf3129df49ce5ce63ff46b2bbbbba1eaf9ec055` on branch `elt_bench++`.

| Commit | Date | Change |
|---|---|---|
| `661c8ea` | 2026-08-02 | added Snowflake, Databricks, and Redshift benchmark configurations |
| `8740be4` | 2026-08-03 | updated data-model and ground-truth instructions |
| `fcf3129` | 2026-08-03 | unified the agent dispatch across warehouses |

The checkout contains 100 task directories for each destination. All three
reuse the shared schema set and equivalent source declarations. The physical
destination contract changes as follows:

| Destination | Airbyte definition ID | Runtime namespace | Transform adapter |
|---|---|---|---|
| Snowflake | `424892c4-daac-4491-b35d-c6688ba547ba` | `<task>.AIRBYTE_SCHEMA.<table>` | `dbt-snowflake` |
| Databricks | `072d5540-f236-4294-ba7c-ade8fd918496` | `<catalog>.<task>.<table>` | `dbt-databricks` |
| Redshift | `f7a7d195-377f-cf5b-70a5-be6b819019dc` | connected database, then `<task>.<table>` | `dbt-redshift` |

The upstream repository adds configuration and runner paths; it does not pin a
complete connector/runtime matrix or record a current real-cloud certification
run. ELT-taskgen pins Snowflake destination connector `4.1.2`, Databricks
`4.0.2`, and Redshift `4.0.7` from Airbyte's official connector metadata
snapshot on 2026-08-31. Therefore none of the three should be described as
currently certified solely because its code path exists: a pinned contract is
necessary for replay, but only a recorded live run supplies certification.

### Certification blockers and resolved upstream issues

- Resolved on 2026-08-31 in the ELT-Bench working tree:
  `world_development_indicators/data_model.yaml` contained an unquoted colon
  in all three destination copies; line 14 is now double-quoted, all three
  copies strict-parse, and `elt-taskgen bench-verify-corpus` plus
  `tests/test_bench_corpus_parse.py` gate the whole committed corpus;
- `donor/data_model.yaml` adds two-decimal `item_cost_most` rounding only in
  the Snowflake copy, so the three projections do not yet share one exact
  semantic specification;
- the legacy source bootstrap remains Snowflake-default rather than a complete
  three-destination certification command;
- Resolved on 2026-08-31 in the ELT-Bench working tree: the codex, claude, and
  openhands runners continued after namespace-reset failure; preparation
  failures now raise `DestinationPreparationError`, abort the attempt before
  any container or session, and flip the batch exit code (deliberate
  stale-state seeding remains deferred to the real-runtime attestation run);
- Resolved on 2026-08-31 in the ELT-Bench working tree: Snowflake reset quoted a
  lowercase database name while Airbyte and the evaluator use unquoted
  identifiers; reset now emits unquoted upper-cased identifiers, the
  `works_cycles` configs' destination namespace was corrected to the task
  identity, and a corpus test ties every config's namespace to the identity
  the evaluator addresses (real-Snowflake resolution attestation still
  pending);
- an explicit host reset credential can differ from credentials retained in
  the solver-visible `config.yaml`;
- attempts do not yet prove complete isolation of Airbyte resources,
  Terraform/dbt state, destination namespaces, or Redshift S3 prefixes;
- the immutable matrix now binds connector/source-service versions, exact
  published Terraform/dbt runner refs, their reviewed build inputs, and dbt
  Core/adapter versions, but a lifecycle orchestrator must still collect every
  required live observation; and
- strict evaluators now collect exact typed Stage 1 and Stage 2 fingerprints,
  and execution receipts retain Airbyte job ids, Terraform-state identity, and
  a fresh dbt invocation artifact, but the evaluator and runner are not yet one
  cleanup-complete orchestration transaction.

These runtime items do not invalidate DuckDB-generated semantic gold, but they
prevent an unscoped cloud-certification claim.

## Current implementation state

As of 2026-09-04:

The open implementation risks and the mandatory real-data DuckDB test gate are
tracked in `IMPLEMENTATION_REVIEW_2026-08-31.md` (maintainer-local; not published in this repository).

- deterministic population generation, private DuckDB oracles, attacks, and
  semantic comparison functions exist;
- schema-3.5 export emits one combined cloud-shaped public task, chains the
  semantic/runtime/certification identities, and binds any frozen difficulty
  evidence plus exact typed source provenance;
- the release CLI defaults to certified mode, requiring current empirical EL+T
  evidence and a fresh tier-A/B sandbox attestation; the explicit development
  mode remains unlabelled;
- source-service, install, execution, provisioning, and evaluator adapters
  exist for Snowflake, Databricks, and Redshift, with local/mock coverage;
- Stage 1 and Stage 2 cloud commands can be run and verified separately; and
- the execution layer preserves exact Airbyte job ids and Terraform-state
  digest, lineage, serial, and resource-address roster; revalidates the
  complete admitted workspace and state before sealing; forces dbt
  `run_results.json` into a fresh bounded target directory; and validates the
  dbt invocation, digest, and exact successful model roster before it can
  enter typed evidence;
- the certification state machine derives pass/fail from sealed strict result
  records, seals runner/preflight observations, verifies closed v10/v11
  matrix protocols under their recorded rules, and refuses missing runtime
  observations or runner-image drift;
- `runtime certification` exposes the release-bound sandbox, pending nonce,
  typed evidence, observation, cleanup, status, and completion states, while
  `runtime certify` performs the security-critical finish in one command and
  promotes difficulty only after its cleanup-bound lifecycle verifies;
- release schema 3.2 introduced the private, hash-bound semantic TaskIR, and
  `semantic score` replays versioned combined EL+T attempts across hidden
  populations in a resource-limited DuckDB worker.

The mandatory real-data DuckDB gate (`tests/test_semantic_gate.py`, run via
`make gate`) writes its citable per-population evidence report to
`$ELT_TASKGEN_GATE_REPORT` (default: `<tempdir>/semantic_gate_report.json`).

The top-level `score --duckdb` command remains the schema-1/2 split-task
compatibility scorer. Release schema 3.3 added layered identity, schema 3.4
made the identities a certification-safe chain, and schema 3.5 added
release-verifiable source provenance. No
tier-A/B real-runtime attestation is checked into this repository; operators
must execute the public lifecycle on the isolated Linux host, and the five
source drives still need re-freezing onto the semantic-package schema. Follow
[`plans/duckdb_rlvr_cloud_runtime_migration.md`](plans/duckdb_rlvr_cloud_runtime_migration.md)
for the ordered work.

Receipt revalidation is tamper detection, not an authentication boundary.
The legacy direct Docker runner still mounts the live solver workspace
read/write, so a mutate/use/restore race cannot be excluded by matching
pre/post hashes. Stage 1 also does not re-query sealed job ids from Airbyte at
attestation time, evaluator results carry no execution-session timestamp or
invocation id, and the public evidence seal is an unkeyed digest. The public
lifecycle therefore remains a trusted-harness boundary: its captured input
must be privilege-separated/read-only, its observation JSON must come from the
live control-plane observer, and its store/publication must be access
controlled. Low-level evidence objects or hand-written JSON alone are not
proof that warehouse execution occurred.

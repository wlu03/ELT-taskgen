# Tinker RL environment for generated ELT tasks

> Status: researched integration design; not implemented. As of
> 2026-08-31, no destination/matrix/population runtime certification has been
> recorded for this integration or for the current warehouse adapters.

This document defines:

1. what Tinker requires to run reinforcement learning; and
2. how Tinker should wrap this repository's current combined ELT semantic
   scorer without weakening its reward, privacy, or failure contracts.

The Tinker API does not require Docker or a local GPU. Tinker's model sampling
and training run on remote GPU workers. The dataset, environment, grader, and
training loop run in a user-controlled CPU Python process. The initial ELT
integration also does not require Docker: the policy emits one declarative JSON
submission and receives no shell or filesystem tool. Docker, a VM, Modal, or
another real sandbox is required only if a later environment executes
model-generated code, shell commands, dbt, or other mutable tools.

This integration plan does not change the architecture definitions. The
meanings of `oracle_validation`, `semantic_rlvr`, `runtime_certification`, and
`agent_benchmark_score` remain governed by
[`../EXECUTION_MODEL.md`](../EXECUTION_MODEL.md). The combined task and cloud
solver boundaries remain governed by
[`../TWO_STAGE_RLVR_CONTRACT.md`](../TWO_STAGE_RLVR_CONTRACT.md).
The separate plan for a sandboxed, artifact-level environment that executes
candidate Terraform and dbt files locally is
[`cloud_free_elt_agent_rlvr.md`](cloud_free_elt_agent_rlvr.md).

## Decision summary

| Workload | Where model compute runs | Local Docker required? | Isolation decision |
|---|---|---:|---|
| Tinker API, SFT, or ordinary text RL | Tinker remote GPU workers | no | normal Python virtual environment |
| Proposed one-turn ELT `semantic_rlvr` | Tinker remotely; `elt-taskgen` grader on a trusted worker | no | existing killable scorer process and hardened in-memory DuckDB connections |
| Tool-using ELT proxy with shell/dbt/filesystem writes | Tinker remotely; tools in a sandbox | not inherently | use a real local or remote container, VM, or microVM boundary; Docker is one option |
| Tinker Code RL with local SandboxFusion | Tinker remotely; SandboxFusion locally | yes by default | documented Docker sandbox service |
| Tinker Code RL, Harbor RL, or Agent RL with Modal | Tinker remotely; task container on Modal | no local Docker | remote container sandbox |
| Current local Airbyte -> warehouse -> dbt path | Tinker is optional; runtime services are external | yes, or a compatible container runtime | current implementation uses a Compose source stack and content-addressed Terraform/dbt runners |

Docker is an environment capability, not a Tinker API prerequisite. A future
remote runtime backend could avoid local Docker, but this repository does not
currently implement one.

`TINKER_SUBPROCESS_SAMPLING=1` is a performance option that moves `sample()`
and `compute_logprobs()` to a spawned Python subprocess. It does not run or
isolate the grader and is not a security boundary.

## Initial environment

Use the current schema-3.2 combined semantic scorer as a one-turn Tinker
environment:

```text
                         remote Tinker service
                   sampling + training on GPU workers
                                  ^
                                  |
                         native SamplingClient
                                  |
                                  v
                  trusted Tinker training coordinator
                    public prompt -> model completion
                                  |
                                  v
                     one single-use ELT Env instance
                                  |
                  exact semantic-v1 JSON submission
                                  |
                                  v
             trusted parent: load_semantic_package once
                                  |
                                  v
             killable child: bounded EL + T DuckDB replay
                                  |
                                  v
            private parent comparator: hidden gold + minimum
                                  |
                                  v
     semantic_el_reward, semantic_t_reward, gated reward, metrics
```

This path is `semantic_rlvr`. It performs no Airbyte, dbt, or cloud-warehouse
operation and must never be reported as cloud compatibility or end-to-end
execution.

### Episode contract

One `Env` instance represents one model completion for one parent task. The
model must produce the existing exact semantic-v1 envelope:

```json
{
  "schema_version": "1.0",
  "task_id": "<parent task id>",
  "load_plan": {
    "<every source table>": {
      "format": "<allowed trusted reader>",
      "path": "<source-root-relative path>"
    }
  },
  "sql_by_mart": {
    "<every required mart>": "SELECT ..."
  }
}
```

The top-level keys are exact. Every declared source table and mart is required.
The allowed readers are `postgres_sql`, `jsonl`, `rest_pages`, `s3_jsonl`, and
`csv`. A load step selects only a confined relative path and trusted reader; it
does not contain executable extraction code. Each mart contains exactly one
read-only DuckDB query.

Use this environment flow:

1. `initial_observation()` renders a leak-checked solver view from approved
   public task fields, the development source-tree **path listing**, and the
   exact response schema. Derive the listing from the verified frozen release,
   not a mutable workspace, and say that hidden populations share the layout
   while page/part counts may differ. Never serialize the private `TaskIR`.
2. Preflight the exact prompt and a known-correct complete response using the
   selected model tokenizer, renderer, stop conditions, context window, and
   output-token limit. If either cannot fit, exclude the task as a harness/
   configuration problem. `InitialObservationOverflow` is no label, not zero.
3. Tinker's native `SamplingClient` produces one completion. Pass its decoded
   assistant content directly to the strict parser; do not extract, repair, or
   normalize JSON.
4. `step()` invokes `score_semantic_text()`, not the legacy top-level
   `score --duckdb` interface. The scorer evaluates all hidden graded
   populations by default.
5. `step()` ends the episode and returns `SemanticScoreResult.reward`. It logs
   the independent reward heads and stable error metrics without returning
   private grader details to the policy.
6. The Cookbook computes advantages and constructs Tinker training data from
   sampled tokens, sampling log probabilities, and reward.

The beta OpenAI-compatible endpoint is intended for testing; Tinker's
documentation recommends the native sampling client for inference inside RL
training.

There is not yet a dedicated schema-3 semantic prompt renderer. The existing
calibration prompt code contains public-view and leak checks, but its
`TaskVariant.FULL` spelling is legacy diagnostic vocabulary. The adapter should
add a versioned combined-task renderer rather than expose `FULL` as the new
training task type.

### Combined task and population requirement

A schema-3 task has one public identity and two scored phases. Do not create
Tinker tasks named `<task_id>__el` and `<task_id>__t`; those suffixes survive
only as private phase evidence. Return these values as metrics from the same
episode:

```text
semantic_el_reward = 1 only when all required raw counts match
semantic_t_reward  = fraction of fully matching required marts
reward             = 0 if semantic_el_reward != 1 else semantic_t_reward
```

Transform is evaluated from a trusted source load even when the submitted EL
plan fails. This preserves the diagnostic T head; only the final training
reward is EL-gated.

Likewise, do not make populations separate Tinker environments. One completion
is evaluated against all four hidden graded populations. The current scorer
contract hard-codes minimum aggregation in `SemanticScoreResult`; schema-3.2
does not yet declare the aggregation in `ReleaseManifest`. Manifest-declared
aggregation remains target architecture. `development` is solver-visible and
diagnostic only and never contributes to the aggregate.

## Mapping to Tinker Cookbook abstractions

Tinker Cookbook uses its own environment protocol, not Gym or Gymnasium. The
[RL overview](https://tinker-docs.thinkingmachines.ai/cookbook/rl/) defines
`Env`, `EnvGroupBuilder`, and `RLDataset` as the core types.

| Tinker abstraction | ELT responsibility |
|---|---|
| `RLDataset` | Select immutable release/task identities and yield batches of group builders. Keep train and held-out task families separated. |
| `EnvGroupBuilder` | Hold only serializable configuration such as release identity/path, task ID, group size, and scorer limits. Construct live resources lazily in `make_envs()`. |
| `Env` | Present one combined prompt, accept one completion, call the semantic scorer once, return metrics, and terminate. Each instance is single-use. |
| rollout group | Create several independent completions for the same task when using GRPO-style group-relative advantages. Every sibling receives the same prompt and immutable package but its own scorer child. |
| `compute_group_rewards()` | Leave at zero initially because the canonical per-attempt reward is returned by `Env.step()`. Do not average or redefine it across completions. |
| `cleanup()` | Idempotently stop scorer workers and remove attempt-local state on success, failure, cancellation, or timeout. |
| `RetryOnFailure` | Retry only genuinely transient transport or provisioning faults. Never replace a measured zero, and never loop on deterministic package/gold/source corruption. |

Tinker requires builders and rollout strategies to be pickleable for
distributed execution. Store paths, IDs, numbers, and renderer configuration
on the builder; do not store open files, API connections, running processes, or
other live clients. The official
[`EnvGroupBuilder` reference](https://tinker-docs.thinkingmachines.ai/cookbook/api-reference/rl/envgroupbuilder/)
also requires cleanup to be safe after partial failure.

If rollout execution leaves the coordinator host, a path in the builder does
not distribute the release. Either keep scoring on trusted client-controlled
hosts or stage the exact content-bound release read-only through an
integrity-checked, access-controlled channel. Verify it before accepting a
rollout and pass only its path and identity through the builder. Never embed
private package bytes in Tinker-bound configuration, a prompt, or a
model-visible tool result.

The initial adapter should use a custom single-step `Env` rather than
`ProblemEnv`, because it needs structured reward heads, stable error metrics,
and an explicit distinction between measured policy failure and missing labels. A
Use `MessageEnv` only if a future version introduces a multi-turn tool
protocol.

### Async grading boundary

`score_semantic_text()` is synchronous, can wait for the full attempt timeout,
and creates a spawned child process. Calling it directly from async
`Env.step()` would block the Cookbook event loop and can serialize sibling
rollouts. Put scoring behind a bounded thread/worker queue or a versioned
scorer service, with cancellation-safe child cleanup and explicit backpressure.
Test Python's `spawn` behavior in the actual rollout topology. Subprocess
sampling does not make the grader non-blocking.

## Reward and failure semantics

The training label exists only when the current scorer successfully measures
an attempt. Preserve these distinctions:

| Event | Training reward | Retry/disposition |
|---|---:|---|
| correct valid submission | measured value in `[0, 1]` | record |
| wrong load plan or wrong mart result | measured zero or partial gated value | record; this is learnable policy behavior |
| malformed envelope | structured all-zero result | record |
| invalid query or mart output limit | affected mart fails; T/final reward may remain partial | record |
| scorer `execution_timeout` or untrusted `worker_failed` | structured all-zero result under the current contract | record only when capacity testing shows the worker was not starved by infrastructure |
| `SemanticPackageError` or `SemanticHarnessError` | **no label** | quarantine/halt the task until the deterministic release or grader environment is repaired; retry only a proven transient cause |
| Tinker authentication, transport, rate-limit, or sampling failure | **no label** | bounded API retry; never grade a fabricated completion |
| sandbox or cloud provisioning failure in a future tool environment | **no label** | discard the affected group, clean up, and retry only with fresh state |

Raw exceptions can contain private paths or values. Store only stable
solver-visible error codes in training metrics. Trusted failures may be
recorded in access-controlled operator logs, but must never become `0.0` or be
shown to the policy as task feedback.

If one rollout in a group loses its label because of infrastructure failure,
discard and rerun the group with fresh environments. Mixing replacement
samples into a partially committed group can change group-relative comparison
and silently bias training.

## Trust boundary and sandboxing

### One-turn semantic-environment sandbox requirements

The policy receives tokens and returns tokens. It cannot directly open a file,
run a command, connect to a service, or mutate a database. Its only executable
payload is mart SQL, which the existing scorer treats as untrusted data:

- at most 1 MiB for the complete submission and 256 KiB per mart SQL string;
- exactly one parsed read-only DuckDB query per mart;
- submitted paths must resolve beneath the selected population source root;
- source trees containing symlinks are rejected;
- no DuckDB external access, extension loading, persistent secrets, or temp
  spill;
- one DuckDB thread and bounded DuckDB memory;
- bounded rows and bytes returned per mart;
- a killable spawned child with one wall-clock deadline; and
- private mart gold retained in the trusted parent comparator.

The default `SemanticLimits` are a 60-second total attempt timeout, 512 MiB of
DuckDB memory, one thread, 100,000 result rows per mart, and 16 MiB of result
data per mart. Those limits do not cap total Python/process memory. The spawned
child is hardened at the DuckDB level, not OS-sandboxed; it inherits the scorer
worker's filesystem permissions and environment variables.

The current declarative environment does not require a container. The trusted
scorer should still run as a low-privilege worker with a minimal environment
and no cloud or administrative secrets. Use a sanitized worker, container, or
VM as additional isolation for hostile production traffic. This isolation must
not change inputs, comparator logic, aggregation, or failure classification.

### Sandbox requirements for tool-capable environments

A Python virtual environment and `TINKER_SUBPROCESS_SAMPLING=1` isolate
dependencies and scheduling, not malicious code. Use a fresh real sandbox per
rollout when the policy receives any of these capabilities:

- `BASH`, Python, arbitrary binaries, or unrestricted SQL;
- writing dbt models or Terraform files;
- reading or mutating a task workspace;
- starting services or making network requests; or
- operating a real Airbyte or warehouse task.

In that design, mount only the attempt directory. Keep the private release,
gold, hidden population fixtures, host environment, administrative credentials,
and Docker socket outside the policy sandbox. Give every rollout—including
siblings in one reward group—fresh mutable state, explicit CPU/RAM/PID/wall/
token/output limits, no network by default, and idempotent destruction.

Tinker's official
[`Code RL` recipe](https://tinker-docs.thinkingmachines.ai/cookbook/recipes/code-rl/)
uses local Docker-backed SandboxFusion by default and offers Modal for remote
sandboxing without local Docker. The
[`Harbor RL` recipe](https://tinker-docs.thinkingmachines.ai/cookbook/recipes/harbor-rl/)
also builds task Dockerfiles remotely through Modal. The Agent RL recipe uses
a user Dockerfile on Modal. These are architectural examples for a future
interactive ELT environment, not requirements for the current scorer. The
recipe pages reference moving `nightly` code or, for Agent RL, an older tag, so
they are not evidence that stable Cookbook `0.5.5` exposes identical code.

### Private data and Tinker projects

Tinker necessarily receives the approved prompt projection, generated
completion, and token-level training inputs used by its remote sampling and
training service. It must not receive private TaskIR bytes, answer keys,
expected counts, reference SQL, hidden fixture contents, oracle DuckDB files,
raw grader exceptions, or cloud credentials.

Set an explicit `TINKER_PROJECT_ID`. According to Tinker's
[`Data Model & Permissions`](https://tinker-docs.thinkingmachines.ai/tinker/data-model/),
a session without a project ID uses the organization's Default project, which
initially grants organization-wide Project Member access. A dedicated project
provides an access boundary for runs and checkpoints.

## Dependencies and reproducible installation

### Audited release snapshot

The following are research pins audited on 2026-08-31, not repository pins:

| Distribution | Audited stable version | Python | Why it is needed |
|---|---:|---:|---|
| `tinker` | `0.26.1` | `>=3.11` | native service, training, and sampling clients |
| `tinker_cookbook` (installed as `tinker-cookbook`) | `0.5.5` | `>=3.11` | `Env`, grouping, renderers, rollout processing, and production training loop |
| `elt-taskgen` | repository `0.1.0` | `>=3.11` | semantic package loader, scorer, and canonical comparator |

The Tinker versions were verified from current PyPI metadata and the exact
source manifests at SDK commit
[`f3dc391`](https://github.com/thinking-machines-lab/tinker/blob/f3dc3911950e98f0d07d34b602b8efbfc0285b40/pyproject.toml)
and Cookbook commit
[`9dfcc3a`](https://github.com/thinking-machines-lab/tinker-cookbook/blob/9dfcc3a2cf44432d621a01005073dc6d76a1e87a/pyproject.toml).
Re-audit these pins before upgrading because Tinker releases change
frequently.

Use Python 3.12 for the initial integration. It is inside all declared support
ranges and reduces the risk of missing wheels for newer Python versions.
The SDK does not require Docker, CUDA, a local GPU, NCCL, DeepSpeed, vLLM, Ray,
Gym, or Gymnasium. Some dependencies contain compiled components, so the
selected OS and architecture still need compatible wheels.

### Exact declared runtime dependencies

`tinker==0.26.1` declares:

```text
httpx[http2]>=0.23,<1
pyqwest>=0.4.1
pydantic>=1.9,<3
typing-extensions>=4.10,<5
anyio>=3.5,<5
distro>=1.7,<2
sniffio
numpy
protobuf>=4.21
transformers
rich>=13
click>=8
orjson>=3.10
zstandard>=0.24
```

Its optional `aiohttp` extra adds `aiohttp` and `httpx-aiohttp>=0.1.8`; its
optional `torch` extra adds PyTorch. Neither is needed for ordinary SDK use
with built-in remote losses. Torch is needed for custom local-loss/Torch helper
paths, and is already present when the Cookbook is installed.

`tinker-cookbook==0.5.5` declares:

```text
aiohttp>=3.9
anyio>=4
blobfile>=3
chz>=0.4
cloudpickle>=3
datasets>=2.14
huggingface_hub>=0.20
numpy>=1.24
pillow>=10
pydantic>=2
rich>=13
safetensors>=0.4
termcolor>=2
tiktoken>=0.12
tinker>=0.23
tml-renderers>=0.0.1
torch>=2.10
tqdm>=4.60
transformers>=4.57.6,!=5.4.*,!=5.5.0,!=5.5.1,!=5.5.2,!=5.5.3,<=5.5.4
```

The Cookbook installs a local PyTorch/Transformers dependency stack even though
Tinker's GPU compute is remote. Let the resolver honor the Transformers
exclusions; do not upgrade it independently beyond the declared range.

This repository's core dependencies—`pydantic>=2.9,<3`, `duckdb>=1.2,<2`,
`pyyaml>=6,<7`, and `sqlglot>=25.34,<31`—have no apparent declared conflict
with the audited Tinker bounds. That is not a substitute for generating and
testing one exact lock on every supported runner platform.

### Extras relevant to environment choice

| Extra | Adds | ELT recommendation |
|---|---|---|
| `tinker[aiohttp]` | alternate HTTP transport | omit initially |
| `tinker[torch]` | custom local-loss/Torch helper support | redundant when the Cookbook is installed |
| `tinker-cookbook[modal]` | Modal client | install only for remote container sandboxes |
| `tinker-cookbook[cloud]` | `fsspec` plus GCS/S3/Azure backends | install only for remote trajectory/checkpoint storage |
| `tinker-cookbook[wandb]`, `[neptune-scale]`, `[trackio]` | experiment logging | choose only the logger actually used |
| `[verifiers]`, `[math-rl]`, `[audio]`, `[multiplayer-rl]`, `[vector-search]`, evaluation extras | unrelated environment families | omit from the ELT adapter |

Do not install the `[all]` extra. Install only required extras to limit lock
contents, security scope, and platform wheel requirements.

### Repository layout and lock policy

Do not add the Cookbook and its PyTorch stack to `elt-taskgen`'s core
`pyproject.toml` or install it into the repository's existing `.venv`. Keep the
offline generator/scorer lock authoritative and create a separate integration
project, proposed as `integrations/tinker/`, with its own committed lock.

When implementing the adapter, bootstrap the development environment with:

```bash
uv init --python 3.12 integrations/tinker
cd integrations/tinker
uv add "tinker==0.26.1" "tinker-cookbook==0.5.5"
uv add --editable ../..
uv lock
```

An editable path and the nested lock do **not** freeze local source bytes or
automatically inherit `elt-taskgen`'s parent `uv.lock` as constraints.
Production must additionally pin a clean commit, source-tree digest, or built
wheel digest and constrain DuckDB, sqlglot, Pydantic, and PyYAML to the exact
versions recorded in the release. Certify the resolved integration lock before
training.

If one environment cannot preserve those pins, run the scorer as a long-lived,
versioned worker in the frozen `elt-taskgen` environment and communicate over
a narrow JSON protocol. A container may make that worker reproducible, but it
is still not required by Tinker.

Use released PyPI packages and generate a consumer lock. Do not copy the
upstream SDK repository's development lock into this project: in the audited
commit it did not describe the same version and dependency set as the current
package manifest.

### Required configuration

| Setting | Requirement |
|---|---|
| Tinker credential | authentication is required; this headless design chooses `TINKER_API_KEY`, while SDK `0.26.1` also supports an explicit `api_key`, `TINKER_CREDENTIAL_CMD`, or the key stored by `tinker auth login` |
| `TINKER_PROJECT_ID` | required by this design to avoid accidental use of the org-wide Default project |
| `TINKER_SUBPROCESS_SAMPLING=1` | optional GIL/performance isolation for `sample()` and `compute_logprobs()`; it does not move the grader |
| `HF_TOKEN` | only for gated Hugging Face datasets/models/tokenizers or publishing |
| Modal authentication | only for the optional remote sandbox backend |
| cloud warehouse and Airbyte credentials | forbidden in normal semantic RLVR; inject only into sparse attempt-scoped runtime evaluation |

Create the Tinker `ServiceClient`, `TrainingClient`, and `SamplingClient` in
the coordinator. Do not place service or training clients inside pickleable
environment builders. Tinker's sampling client is the supported API for RL
sampling; do not build the loop on a beta compatible inference endpoint.

## Concurrency, capacity, and determinism

The semantic package is immutable and may be shared read-only by siblings in a
rollout group. Each scorer call creates a new spawned process; within it,
selected populations run in a stable sequence with new in-memory EL and T
connections. No mutable DuckDB file should be shared across completions.

Start with two to four simultaneous scoring processes and increase concurrency
only after a resource soak. Capacity must include the configured 512 MiB
DuckDB limit **plus** Python, source parsing, result, and process overhead for
every rollout. Reserve enough capacity that host starvation does not turn an
infrastructure overload into systematic `execution_timeout` or `worker_failed`
zeros. Tinker async sampling does not remove local CPU/RAM limits.

For every attempt, record at least:

- Tinker project, session, training run, and sampler checkpoint identities;
- release ID, task ID, task content hash, and split/family identity;
- semantic submission, result, scorer, comparator, and aggregation versions;
- selected populations, EL/T/gated rewards, and stable error codes;
- prompt hash, renderer/tokenizer identity, stop conditions, context length,
  maximum output tokens, and sampling parameters;
- token counts, wall time, retry reason, and worker-capacity state; and
- adapter commit/source digest and fully locked environment identity.

Keep private rows, gold, expected counts, reference SQL, reusable credentials,
and raw exceptions out of trajectory stores and general training logs.

## Relationship to real ELT execution

The semantic submission is a private training proxy. It is not the public
cloud solver contract. A real solver authors Terraform/Airbyte and dbt assets,
then produces warehouse state measured through a destination-specific
collector.

Ordinary Tinker rollouts should call only the semantic scorer. Sparse runtime
evaluation remains a separate operator-triggered path:

```text
training attempt -> semantic_rlvr -> local DuckDB reward

selected checkpoint + immutable runtime bundle
    -> fresh source services
    -> Airbyte
    -> Snowflake, Databricks, or Redshift
    -> dbt
    -> runtime certification evidence or agent benchmark score
```

For every runtime population, create fresh source services, Airbyte connections,
destination namespace, Terraform state, and dbt state. Reusing state can
duplicate append-mode loads and invalidate the measurement.

A containerized local DuckDB/dbt agent is still a semantic proxy. It does not
qualify for `runtime_smoke_certified:<destination,matrix-id,population>` or
`runtime_parity_certified:<destination,matrix-id,population-set>`. Only pinned
live execution against the named destination, connector, population set, and
version matrix can make those claims. Runtime commands and credentials belong
in [`../WAREHOUSE_CONNECTORS.md`](../WAREHOUSE_CONNECTORS.md).

The layered `semantic_release_id`, `runtime_bundle_id`, and `certification_id`
remain target architecture; they are not implemented schema-3.2 identity
fields yet.

## Current repository readiness

| Component | Current state on 2026-08-31 |
|---|---|
| combined schema-3.2 semantic package/scorer | implemented in `semantic/{models,package,scoring}.py` and exposed as `semantic score` |
| resource limits and stable result model | implemented |
| Tinker imports, dependency group, adapter, dataset, or training config | absent |
| dedicated combined semantic prompt renderer | absent |
| on-disk schema-3.2 release usable by the scorer | absent; all five on-disk canonical manifests are schema 2.0/profile `el_t` and pin `duckdb-census/1`, which the census-v2 verifier refuses |
| live Snowflake/Databricks/Redshift certification | absent; local/mock adapter coverage is not certification |

The existing top-level `score --duckdb`, split public `__el`/`__t` units,
`ELT-training-data/run_rollout.sh`, and its tool action-space document are
schema-1/2 compatibility references. There is no runnable legacy fallback, and
they must not become the permanent Tinker API. The combined scorer is the
target.

A runnable pilot requires a fresh, verified schema-3.2 release. Freezing a
bundle alone is insufficient for a
`semantic_rlvr_ready` claim: package verification, known-answer replay,
isolation, timeout, and mutation checks must also pass. No cloud credential or
live warehouse is needed for that semantic pilot.

## Implementation sequence

1. Freeze at least one schema-3.2 combined release and complete the
   `semantic_rlvr_ready` checks across all hidden graded populations.
2. Add a versioned, leak-checked combined semantic prompt renderer. Add a
   regression test verifying that it contains no answer-side material and
   includes only development path names, never fixture contents.
3. Create the separate `integrations/tinker/` project and committed Python 3.12
   lock, plus a clean source or wheel digest, using the audited versions or
   freshly re-audited replacements.
4. Implement a pickle-safe dataset/group builder and one-shot custom `Env`.
   Load and verify each immutable semantic package once per worker or group,
   then share it read-only.
5. Put synchronous scoring behind a bounded, cancellation-safe worker boundary.
   Preserve exception taxonomy: measured failures return the scorer's result;
   package/harness/API failures abort the label, and deterministic corruption
   quarantines the task.
6. Run a two-to-four-task inference-only pilot before any Tinker training
   operation. Save prompts, completions, results, timing, and resource metrics
   under restricted experiment storage.
7. Run a small training pilot, held-out family evaluation, and repeated clean
   replay. Confirm that constant-reward groups, retry rates, invalid-format
   rates, capacity, and context/output headroom are visible.
8. Design a separate sandboxed multi-turn environment only if the research goal
   requires shell/dbt behavior rather than the semantic JSON contract. Keep
   real-runtime certification sparse and independent.

## Acceptance checklist

The adapter is not ready for training until all of these pass:

- a known-correct semantic submission receives the frozen expected reward;
- each declared wrong solution loses reward on at least one graded population;
- malformed JSON, traversal, mutating SQL, per-mart output limits, and timeout
  cases preserve their current result and error semantics;
- corrupted or tampered package/gold/source evidence produces no reward and
  quarantines the task;
- exact prompt and known-correct response fit the chosen renderer/token limits;
- repeated clean runs are identical under the same release and lock;
- parallel siblings cannot observe or mutate one another's state;
- async scoring remains responsive and cleanup succeeds after success,
  exception, cancellation, and forced timeout;
- prompts and trajectory logs contain no private TaskIR, gold, expected count,
  hidden fixture, reference SQL, credential, or raw exception data;
- a resource soak establishes a safe local concurrency cap without overload
  zeros; and
- held-out task families remain excluded from training and are evaluated before
  expanding training.

## Primary Tinker sources

- [Tinker architecture](https://tinker-docs.thinkingmachines.ai/tinker/)
  explains the user-controlled CPU loop and remote GPU service boundary.
- [SDK quick start](https://tinker-docs.thinkingmachines.ai/tinker/quickstart/)
  documents installation, native RL calls, and subprocess sampling.
- [Cookbook overview](https://tinker-docs.thinkingmachines.ai/cookbook/) and
  [RL architecture](https://tinker-docs.thinkingmachines.ai/cookbook/rl/)
  define the high-level training stack.
- [`Env` reference](https://tinker-docs.thinkingmachines.ai/cookbook/api-reference/rl/env/)
  and
  [`EnvGroupBuilder` reference](https://tinker-docs.thinkingmachines.ai/cookbook/api-reference/rl/envgroupbuilder/)
  define episode lifecycle, grouping, serialization, and cleanup.
- [Custom environment tutorial](https://tinker-docs.thinkingmachines.ai/tutorials/cookbook-abstractions/custom-environment/)
  shows the intended adapter pattern.
- [Code RL](https://tinker-docs.thinkingmachines.ai/cookbook/recipes/code-rl/),
  [Harbor RL](https://tinker-docs.thinkingmachines.ai/cookbook/recipes/harbor-rl/),
  and [Agent RL](https://tinker-docs.thinkingmachines.ai/cookbook/recipes/agent-rl/)
  show when local or remote container sandboxes enter the design.
- [OpenAI-compatible inference](https://tinker-docs.thinkingmachines.ai/tinker/compatible-apis/openai/)
  explains why the native sampling client is preferred inside RL training.
- [Data Model & Permissions](https://tinker-docs.thinkingmachines.ai/tinker/data-model/)
  documents project isolation and the Default-project behavior.
- [Tinker SDK package metadata](https://pypi.org/project/tinker/) and
  [Cookbook package metadata](https://pypi.org/project/tinker-cookbook/)
  are the release-version authorities used for this dependency snapshot.

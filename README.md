# ELT-taskgen

ELT-taskgen generates and validates tasks for ELT-Bench broken into two stage: EL and T. Agents write and review task specifications; executable checks determine
acceptance.

Each task covers extract/load (EL) and transform (T), with separate scores.
DuckDB evaluates local submissions. Snowflake, Databricks, and Amazon Redshift
are supported through separate real-runtime adapters.

## Environment setup

Requires Python >= 3.11 and `uv`.

```bash
uv sync --frozen
```

Install from `uv.lock`. A standalone `pip install -e .` ignores the lock, and
release creation rejects unapproved dependency drift.

For model-backed generation, configure credentials using `env.example`:

```bash
cp env.example .env
chmod 600 .env
set -a && . ./.env && set +a
```

Keep credentials outside `runs/` and release directories. Independent reference
validation requires models from different families.

## Generating tasks

Supported sources are `dbt`, `dlt`, `synsql`, `schemapile`, and `wikidbs`.
Prepare a pinned intake manifest and provide its referenced artifacts before
running the pipeline. Start with `config/five_source_ingest.example.yaml`.

A `five-source-ingest-v1` manifest contains one candidate from each source.
Use `five-source-ingest-v2` for multiple candidates per source.

### Prepare the workspace

Set the source-data root, manifest, workspace, and agent configuration:

```bash
ET=.venv/bin/elt-taskgen
WS=/path/to/workspace
MANIFEST=/path/to/five-source-ingest.yaml
AGENTS=/path/to/agents.yaml
export ELT_TASKGEN_DATA_ROOT=/path/to/elt-training-data

$ET measure-target --workspace "$WS" --bench-root ../ELT-Bench
$ET ingest-five --manifest "$MANIFEST" --workspace "$WS" --dry-run
```

`measure-target` loads benchmark fingerprints for contamination checks.
The dry run validates the intake without registering tasks.

### Validate the review agents

Run live metrology before starting a production campaign. It tests the review
agents and creates the admission record required by `review`.

```bash
$ET metrology --workspace council --agents-config "$AGENTS" \
  --workers 4 --budget-per-task 60 --budget-total 60
export ELT_TASKGEN_ADMISSION="$PWD/council/state/council.live_admitted"
```

Use the same agent configuration for metrology and generation. Admission is
bound to its original path and active configuration; replays cannot create it.

Metrology and generation can incur model API charges. Review the example
budgets before running. A final API call can exceed its budget allocation
before the worker stops.

### Run the pipeline

```bash
$ET pipeline --workspace "$WS" --ingest-manifest "$MANIFEST" \
  --agents-config "$AGENTS" --workers 4 \
  --budget-per-task 7 --budget-total 35
```

The pipeline ingests candidates, generates data and reference results, runs
agent review and executable validation, measures solver difficulty, and
selects a corpus.

Validation includes independent reference reconstruction, contamination checks,
and tests that distinguish incorrect implementations from the reference.
Each task has five populations: development, primary, resampled,
counterfactual, and stress. Development data is available to the solver;
graded populations and reference answers remain private.

Task defects produce permanent rejections within a workspace. Infrastructure,
credential, admission, and budget failures stop without rejecting the task.
Rerun the command after resolving those failures.

For fixed candidate counts, source allocations, and run reports, see the
[configurable pipeline guide](docs/CONFIGURABLE_PIPELINE.md). Candidate-count
runs do not replace rejected or blocked candidates.

## Creating a release

By default, releases require current empirical EL and T difficulty measurements
and a valid isolation attestation from a Linux tier A/B host. The agent
configuration must match the host's sandbox.

Create the pre-release attestation, then freeze the selected corpus:

```bash
RELEASE=/path/to/release
ATTESTATION=/secure/run/sandbox_attestation.json

$ET runtime certification attest-unbound \
  --agents-config "$AGENTS" --mount-root "$WS" \
  --out "$ATTESTATION"

$ET pipeline --workspace "$WS" --agents-config "$AGENTS" \
  --workers 4 --budget-per-task 7 --budget-total 35 \
  --release-dir "$RELEASE" --sandbox-attestation "$ATTESTATION"
```

For an explicitly unlabelled local release, skip the attestation command and
replace `--sandbox-attestation` and its value with `--development-release`.
Empirical calibration still runs.
An isolation attestation does not establish warehouse compatibility.

Releases are immutable. Keep solver inputs under `public/` and evaluation
evidence under `private/`:

```text
release/
├── public/<task_id>/          # Instructions, schemas, config, Terraform seed
├── private/<task_id>/         # Reference answers, populations, provenance
├── private/<task_id>__el/     # EL scoring evidence
├── private/<task_id>__t/      # T scoring evidence
├── sandbox_attestation.json  # Certified releases only
├── release_manifest.json
└── checksums.sha256
```

There is one public task per project, not separate public EL and T tasks.
Private evidence and release checksums are not solver inputs.

## Local artifact scoring

Install the pinned dbt-duckdb runtime before starting workers:

```bash
uv sync --project runtime-images/dbt-duckdb --locked
```

Install a task, edit its Terraform and dbt files under `task/elt/`, then seal
and score the submission:

```bash
TASK=my_task_id
DESTINATION=snowflake

$ET training install --release "$RELEASE" --task-id "$TASK" \
  --attempt /tmp/authoring

# Edit /tmp/authoring/task/elt/.

$ET training seal --release "$RELEASE" --task-id "$TASK" \
  --attempt /tmp/authoring --output /tmp/sealed

$ET training score --release "$RELEASE" --task-id "$TASK" \
  --seal /tmp/sealed --seal-sha256 <digest> \
  --destination "$DESTINATION" --attempts-dir /tmp/training-attempts \
  --dbt-python runtime-images/dbt-duckdb/.venv/bin/python \
  --dbt-manifest runtime-images/dbt-duckdb/runtime.json
```

Replace `<digest>` with the sealed artifact's SHA-256 digest. The destination
must match the release. For trusted single-command scoring,
`--candidate-workspace /tmp/authoring` can replace the seal arguments.

The scorer compiles Terraform into a normalized Airbyte intent graph, loads the
selected streams into DuckDB, and runs the submitted dbt models against that
same state. Strict raw-table schema, typed-content, and immutability checks
gate the mart reward.

The training-signal interface returns no label (`None`) for task, harness, or
infrastructure failures. Model policy violations receive zero reward.
The current training environment accepts one artifact submission per episode;
interactive tool use is deferred, and there is no `train` subcommand.

`elt-taskgen semantic score` is a separate interface for JSON load plans and SQL.
Its upstream-compatible EL score checks row counts only, unlike the strict
raw-data checks in the artifact workflow above.

## Warehouse evaluation

Local scoring uses a DuckDB proxy, not a warehouse emulator. Warehouse
certification requires a pinned run through real Airbyte, the selected
warehouse, and the matching dbt adapter.

Use the [warehouse runbook](docs/WAREHOUSE_CONNECTORS.md) for Snowflake,
Databricks, and Redshift setup and execution. Cloud results use the same
private comparator as local evaluation. These runs are separate from ordinary
RLVR scoring.

## Tests

Run the offline test suite without paid model calls:

```bash
.venv/bin/python -m unittest discover -s tests
```


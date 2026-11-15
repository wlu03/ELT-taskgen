# ELT-taskgen

Generate ELT tasks and datasets from five source pools.

Source files -> adapters -> task definitions -> generated data.

## 1. Setup

Use Python 3.11 or newer and `uv`. Run these commands from `ELT-taskgen/`:

```bash
uv sync --frozen

export ELT_TASKGEN_DATA_ROOT="$PWD/../ELT-training-data"
TASKGEN_WORKSPACE="$PWD/runs/source-generation"
```

Change the data-root path if your source files are stored elsewhere. Keep an
`ELT-Bench` checkout available for the source-overlap checks used during intake.
The local generation steps below do not require model API credentials or a
cloud warehouse.

## 2. Load the sources

Download or copy the source pools into
`$ELT_TASKGEN_DATA_ROOT/curation/packages_raw/`. The directory mappings are in
[config/sources.yaml](config/sources.yaml).

- [dbt](https://github.com/fivetran): Fivetran transformation packages; load a compiled `manifest.json` using the [dbt adapter](src/elt_taskgen/adapters/dbt.py).
- [dlt](https://github.com/dlt-hub/verified-sources): Verified connector sources; load a connector manifest YAML and pinned package using the [dlt adapter](src/elt_taskgen/adapters/dlt.py), currently supporting Personio, Pipedrive, and Workable.
- [SynSQL](https://huggingface.co/datasets/seeklhy/SynSQL-2.5M): Database schemas in `tables.json`, loaded using the [SynSQL adapter](src/elt_taskgen/adapters/synsql.py).
- [SchemaPile](https://zenodo.org/records/12682521): Relational schemas in `schemapile-perm.json`; load them with a prepared schema index using the [SchemaPile adapter](src/elt_taskgen/adapters/schemapile.py).
- [WikiDBs](https://zenodo.org/records/11559814): Wikidata-derived databases with `schema.json` and CSV table rows; load them with the referenced family map using the [WikiDBs adapter](src/elt_taskgen/adapters/wikidbs.py).

These adapters are installed with the project. WikiDBs supplies existing table
rows; the other sources primarily provide schemas and metadata for synthetic
data generation.

The default [candidate pool](config/candidate_pool.ingest.yaml) lists the source
entries, prepared artifacts, versions, and file hashes to load. Make sure those
files are available before running:

```bash
.venv/bin/elt-taskgen pipeline \
  --workspace "$TASKGEN_WORKSPACE" \
  --bench-root ../ELT-Bench \
  --candidate-count 6 \
  --readiness-profile draft \
  --seed 0
```

This selects six candidates across the source pools, checks their inputs, and
registers supported tasks. A compatible semantic recipe is selected
automatically. Six is the number attempted; some candidates may be refused.

To use your own source selection, add
`--ingest-manifest /path/to/pool.yaml` with a `five-source-ingest-v2` manifest.
The `draft` step creates task definitions; the next step writes the data rows.

## 3. Start data generation

Generate the datasets for every registered task:

```bash
for task_file in "$TASKGEN_WORKSPACE"/tasks/*/task_ir.json; do
  task_dir="${task_file%/task_ir.json}"
  .venv/bin/elt-taskgen generate \
    --workspace "$TASKGEN_WORKSPACE" \
    --task-id "${task_dir##*/}" \
    --replay-only \
    --no-repair-proposer || break
done
```

Each task gets five datasets: `development`, `primary`, `resampled`,
`counterfactual`, and `stress`. These commands make no LLM calls.

Find the output under `$TASKGEN_WORKSPACE/tasks/<task_id>/`:

- `task_ir.json`: source schema, relationships, output definitions, and recipe.
- `populations/<population>/rows/`: table data in JSONL format.
- `populations/<population>/rendered/`: data formatted for its assigned source backend.

These steps produce local task definitions and source data. The public
`config.yaml` and `data_model.yaml` are produced during the later task export.

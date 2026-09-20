# batch50_20260919 — 50 generated ELT tasks (public bundles)

The solver-facing half of 50 tasks produced by this pipeline on 2026-09-19 and
frozen into releases on 2026-09-20. Sources: dbt 1, dlt 3, synsql 18,
schemapile 18, wikidbs 10.

## What is here

One directory per task, holding exactly what a solver is given:

- `data_model.yaml` — the mart contract: columns, types and the rule each one obeys
- `config.yaml` — the task's source and destination configuration
- `documentation/` — Airbyte connector and destination guides
- `schemas/` — the source tables' headers
- `check_job_status.py` — helper for polling Airbyte sync jobs
- `index.json` — task id -> release id, split and task content hash

The releases also carry an empty `<destination>_credential.json` per task,
which this repository does not track (`.gitignore:15` refuses any
`*_credential.json`). `elt-taskgen training install` writes it into the
attempt directory, so nothing is missing at run time.

## What is NOT here

The answer key. Each task's frozen gold, its five populations, the reference
SQL and the attack cases live in the release's `private/` side, which stays
out of this repository. Scoring a submission requires the full release:

```
elt-taskgen training install --release <release-dir> --task-id <task> --attempt <dir>
elt-taskgen training score   --release <release-dir> --task-id <task> \
    --candidate-workspace <dir> --destination snowflake --attempts-dir <dir>/scored \
    --dbt-python runtime-images/dbt-duckdb/.venv/bin/python \
    --dbt-manifest runtime-images/dbt-duckdb/installed-distributions.json
```

A solver's job is to write `elt/main.tf`, `elt/dbt_project.yml`,
`elt/models/sources.yml` and at least one `elt/models/*.sql`.

## Properties

- Releases are `release_mode: development`: structural difficulty only, no
  tier A/B sandbox attestation.
- Every task is labelled `train`; a split is computed within a release and each
  release holds one task, so there is no validation split.
- Column names are the pre-rename template names (`parent_key`, `link_count`,
  `total_amount`). The dataset-specific naming landed after these were frozen.

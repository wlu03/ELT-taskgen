# Local dbt-duckdb training runtime

This lock describes the cloud-free `workspace-v1` transformation worker. It
is separate from the Snowflake, Databricks, and Redshift certification
images. Candidate projects run with dbt Core against the DuckDB database
created by the trusted local-sync harness; no cloud adapter or credential is
needed.

`uv.lock` freezes resolution, artifact URLs, and hashes for CPython 3.9. The
companion `installed-distributions.json` is the exact no-project distribution
set produced by `uv sync --locked`; the runner binds both files by SHA-256 and
rejects a worker with a missing, changed, duplicate, or extra distribution
before invoking `dbt parse`, `dbt compile`, or `dbt run --full-refresh`.

Provision ahead of training with `uv sync --project runtime-images/dbt-duckdb
--locked`. The generated `.venv` is not committed. Scorer execution does not
invoke `uv`, Airbyte, Terraform, a cloud adapter, or a network API; all
required packages must already be provisioned from this lock. Candidate
`packages.yml`, hooks, macros, profiles, file readers, extension loading, and
export/attach operations are rejected.

The current runner's invalid proxy configuration and static policy checks do
not provide a network boundary. Production RLVR workers must place this runtime
inside the no-network, resource-bounded container or microVM described in the
cloud-free plan.

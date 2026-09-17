# Reproducible cloud-certification runner images

These images provide the binaries that `runtime run-stage1` and
`runtime run-stage2` execute in the restricted `DockerRunner`. They contain no
credentials or task data and are not used by the high-volume DuckDB RLVR loop.
Building an image is implementation evidence, not a live certification. See
[`../docs/EXECUTION_MODEL.md`](../docs/EXECUTION_MODEL.md).

Every available base image, direct Python dependency, and transitive Python
dependency is immutable:

| Image | Tool pins | Status |
| --- | --- | --- |
| `terraform` | Terraform `1.15.8`, Airbyte provider `0.6.5` | available |
| `dbt-snowflake` | Python `3.12.14`, uv `0.11.16`, dbt-core `1.12.0`, dbt-snowflake `1.12.0` | available |
| `dbt-databricks` | Python `3.12.14`, uv `0.11.16`, dbt-core `1.12.0`, dbt-databricks `1.12.4` | available |
| `dbt-redshift` | Python `3.12.14`, uv `0.11.16`, dbt-core `1.12.0`, dbt-redshift `1.11.1` | available |

The dbt dependency graph and PyPI artifact hashes are recorded in
`dbt/uv.lock`. dbt-core's experimental parser is distributed as a hashed PyPI
bootstrap sdist; its embedded manifest separately verifies the platform wheel
downloaded from the dbt GitHub release. The Terraform image vendors the exact
Airbyte provider selected by generated tasks (`airbytehq/airbyte` `0.6.5`) in a
filesystem mirror. Its Dockerfile verifies the official amd64 or arm64 archive
checksum, and `terraform.rc` provides no registry fallback.

`manifest.json` is the reviewed allowlist used for certification identity and
for wheel packaging. Its `images` mapping pins the exact published Terraform
and destination-specific dbt repository-plus-digest references; its `files`
list pins their reviewed build inputs. Files outside that list—including the
locally provisioned `dbt-duckdb/.venv`—cannot affect `runner_images_digest` or
enter a wheel. When a runner is rebuilt intentionally, update its immutable
reference and any changed relative inputs in the sorted manifest, update
`MANIFEST.in` for newly added files, and review the resulting certification-id
rotation. The distribution smoke test fails if the sdist cannot rebuild the
self-contained wheel.

## Build and push

Run these commands from the `ELT-taskgen` repository root. Use a unique release
tag. Certification uses one platform per image because a
multi-platform digest selects different binaries on amd64 and arm64 hosts. The
default below is the benchmark's reproducible linux/amd64 platform.

```sh
export ELT_RUNNER_REGISTRY="registry.example.com/kang-lab"
export ELT_RUNNER_TAG="2026-08-28.1"
export ELT_RUNNER_PLATFORM="linux/amd64"

docker buildx build \
  --platform "$ELT_RUNNER_PLATFORM" \
  --file runtime-images/terraform/Dockerfile \
  --tag "$ELT_RUNNER_REGISTRY/elt-taskgen-terraform:$ELT_RUNNER_TAG" \
  --push .

docker buildx build \
  --platform "$ELT_RUNNER_PLATFORM" \
  --file runtime-images/dbt-snowflake/Dockerfile \
  --tag "$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-snowflake:$ELT_RUNNER_TAG" \
  --push .

docker buildx build \
  --platform "$ELT_RUNNER_PLATFORM" \
  --file runtime-images/dbt-databricks/Dockerfile \
  --tag "$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-databricks:$ELT_RUNNER_TAG" \
  --push .

docker buildx build \
  --platform "$ELT_RUNNER_PLATFORM" \
  --file runtime-images/dbt-redshift/Dockerfile \
  --tag "$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-redshift:$ELT_RUNNER_TAG" \
  --push .
```

Build from the repository root. Both dbt Dockerfiles use it as their build
context so they share the same reviewed
lockfile. `.dockerignore` restricts that context to these Dockerfiles and their
pin files; task outputs, local environments, and credentials are not sent to
the builder. To certify arm64, publish it under a distinct tag and retain its
own three digests rather than creating a combined manifest.

## Record immutable digests

Inspect each pushed single-platform tag and copy its `Digest: sha256:...`
value:

```sh
docker buildx imagetools inspect \
  "$ELT_RUNNER_REGISTRY/elt-taskgen-terraform:$ELT_RUNNER_TAG"
docker buildx imagetools inspect \
  "$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-snowflake:$ELT_RUNNER_TAG"
docker buildx imagetools inspect \
  "$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-databricks:$ELT_RUNNER_TAG"
docker buildx imagetools inspect \
  "$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-redshift:$ELT_RUNNER_TAG"
```

Configure the harness with repository-plus-digest references:

```sh
export TERRAFORM_RUNNER_IMAGE="$ELT_RUNNER_REGISTRY/elt-taskgen-terraform@sha256:<manifest-digest>"
export DBT_SNOWFLAKE_RUNNER_IMAGE="$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-snowflake@sha256:<manifest-digest>"
export DBT_DATABRICKS_RUNNER_IMAGE="$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-databricks@sha256:<manifest-digest>"
export DBT_REDSHIFT_RUNNER_IMAGE="$ELT_RUNNER_REGISTRY/elt-taskgen-dbt-redshift@sha256:<manifest-digest>"
```

Before freezing a release, replace the corresponding entry in
`runtime-images/manifest.json`; the certification matrix is generated from
that reviewed mapping. A tag, a locally built image id, or a different digest
can be used for development but does not satisfy the release-bound runtime
identity.

`DockerRunner` rejects tag-only references. Pass the Terraform image to
`runtime run-stage1 --runner-image`; pass the adapter image matching the
installed task destination to `runtime run-stage2 --runner-image`. A cloud dbt
run must also explicitly select `--sandbox-lane cloud-egress` after the
operator creates the dedicated bridge once with `docker network create
elt-cloud-egress`; the default remains the no-network lane. Snowflake also
requires `--destination-credential` so its fixed profile environment bindings
are populated without putting secret values in `docker run` argv. Registry
credentials belong on the benchmark host. Do not add them to an image, task
bundle, or release manifest.

When intentionally updating dbt pins, edit `dbt/pyproject.toml`, run
`uv lock --project runtime-images/dbt --python 3.12`, review the complete lock
diff, rebuild all affected images, and record their new manifest digests.

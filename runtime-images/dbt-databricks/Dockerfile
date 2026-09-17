FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv
FROM python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS build

ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /opt/dbt
COPY --from=uv /uv /usr/local/bin/uv
COPY runtime-images/dbt/pyproject.toml runtime-images/dbt/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra databricks

FROM python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

ENV DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    HOME=/tmp \
    PATH=/opt/dbt/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/dbt/.venv /opt/dbt/.venv
WORKDIR /workspace
ENTRYPOINT []
CMD ["dbt", "--version"]

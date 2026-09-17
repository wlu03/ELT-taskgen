FROM hashicorp/terraform:1.15.8@sha256:7ae513256f7ce67879e218ae8593d6fbe216ec9e123abe6c94e4e10704857963 AS provider-mirror

ARG TARGETARCH
COPY runtime-images/terraform/provider.tf /tmp/provider/main.tf
RUN set -eu; \
    terraform -chdir=/tmp/provider providers mirror \
      -platform="linux_${TARGETARCH}" /opt/terraform/providers; \
    case "${TARGETARCH}" in \
      amd64) provider_sha256="ed520c606345c89886c55baf4b10f32e470265f6eaa67d809df37f1af860fec5" ;; \
      arm64) provider_sha256="1da387051f1006bdab2286b59a1236790ff126e81b15146b4cdc4b6a61f99fd1" ;; \
      *) echo "unsupported provider architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    provider_archive="/opt/terraform/providers/registry.terraform.io/airbytehq/airbyte/terraform-provider-airbyte_0.6.5_linux_${TARGETARCH}.zip"; \
    echo "${provider_sha256}  ${provider_archive}" | sha256sum -c -

FROM hashicorp/terraform:1.15.8@sha256:7ae513256f7ce67879e218ae8593d6fbe216ec9e123abe6c94e4e10704857963

COPY runtime-images/terraform/terraform.rc /etc/terraform.rc
COPY --from=provider-mirror /opt/terraform/providers /opt/terraform/providers

ENV CHECKPOINT_DISABLE=1 \
    TF_IN_AUTOMATION=1 \
    TF_CLI_CONFIG_FILE=/etc/terraform.rc \
    HOME=/tmp

# The upstream image starts with `terraform`. DockerRunner supplies the full
# argv, so retaining that entrypoint would execute `terraform terraform ...`.
ENTRYPOINT []
CMD ["terraform", "version"]

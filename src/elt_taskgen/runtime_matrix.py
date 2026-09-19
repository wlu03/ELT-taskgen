"""Define the secret-free execution matrix used for certification identity.

The matrix binds control-plane, connector, runner-image, warehouse-session,
parity, and canonical-fingerprint contracts. Connector pins also affect the
private runtime bundle; execution-only pins affect certification only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from pathlib import PurePosixPath

from elt_taskgen.destinations import (
    CUSTOM_API_MANIFEST_VERSION,
    DBT_ADAPTER_CONTRACTS,
    DBT_CORE_VERSION,
    SOURCE_CONNECTOR_CONTRACTS,
    Destination,
    destination_contract,
)
from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.package_resources import resource_path
from elt_taskgen.runtime.source_images import SOURCE_SERVICE_IMAGES
from elt_taskgen.verification.canonical_fingerprint import (
    CANONICAL_FINGERPRINT_VERSION,
)
from elt_taskgen.verification.parity import (
    PARITY_CONTRACT_VERSION,
    PARITY_OBSERVATION_SCHEMA_VERSION,
    PARITY_REGISTRY_DIGEST,
)
from elt_taskgen.verification.parity_scope import PARITY_SCOPE_POLICY_VERSION

#: Version of the matrix RECIPE itself (which keys exist and how values are
#: spelled). Bumping it deliberately rotates every certification identity.
CERTIFICATION_MATRIX_VERSION = "12"

#: Sealed-record schemas affect certification identity.
CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION = "1.2"
CERTIFICATION_ATTESTATION_SCHEMA_VERSION = "1.3"
CERTIFICATION_PENDING_SCHEMA_VERSION = "1.0"

# Preserve v10 validation for frozen releases; writers use the current version.
_V10_STAGE_EVIDENCE_SCHEMA_VERSION = "1.1"
_V10_ATTESTATION_SCHEMA_VERSION = "1.2"
_V10_PENDING_SCHEMA_VERSION = "1.0"
# v11 differs from v10 only in these schema versions. Its matrices record
# canonical fingerprint version 1; v12 records version 2.
_V11_STAGE_EVIDENCE_SCHEMA_VERSION = "1.2"
_V11_ATTESTATION_SCHEMA_VERSION = "1.3"
_V11_PENDING_SCHEMA_VERSION = "1.0"
_V10_SEMANTIC_FIELDS = {
    "canonical_fingerprint_version": "1",
    "parity_contract_version": "1",
    "parity_observation_schema_version": "1.1",
    "parity_registry_digest": (
        "77280be93a73a5b22fb1798f1744dcda533478bb7be7e91a39ce7243e188d0eb"
    ),
    "parity_scope_policy_version": "1",
}
_V10_DBT_FIELDS: dict[Destination, dict[str, str]] = {
    Destination.SNOWFLAKE: {
        "dbt_adapter": "snowflake",
        "dbt_adapter_version": "1.12.0",
        "dbt_core_version": "1.12.0",
    },
    Destination.DATABRICKS: {
        "dbt_adapter": "databricks",
        "dbt_adapter_version": "1.12.4",
        "dbt_core_version": "1.12.0",
    },
    Destination.REDSHIFT: {
        "dbt_adapter": "redshift",
        "dbt_adapter_version": "1.11.1",
        "dbt_core_version": "1.12.0",
    },
}

#: Identity-bearing Airbyte control-plane pins; live certification is still required.
AIRBYTE_ABCTL_VERSION = "v0.30.4"
AIRBYTE_CHART_VERSION = "2.2.0"

RUNTIME_IMAGES_DIRNAME = "runtime-images"
RUNNER_IMAGES_MANIFEST = "manifest.json"
RUNNER_IMAGES_MANIFEST_SCHEMA = "runner-images-manifest-v2"
RUNNER_IMAGE_KEYS = (
    "dbt:databricks",
    "dbt:redshift",
    "dbt:snowflake",
    "terraform",
)
_IMAGE_COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_PINNED_IMAGE = re.compile(
    rf"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?/)?"
    rf"{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*"
    rf"(?::[a-z0-9_][a-z0-9_.-]{{0,127}})?"
    r"@sha256:[0-9a-f]{64}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNPINNED_VALUES = {"legacy", "legacy-unpinned", "latest", "unpinned"}


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting ambiguous duplicate names."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _default_runtime_images_root() -> Path:
    """Shipped ``runtime-images/`` in a checkout or installed distribution."""
    return resource_path(RUNTIME_IMAGES_DIRNAME)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runner_manifest(
    root: Path | None = None,
) -> tuple[Path, list[str], dict[str, str]]:
    """Parse the closed runner manifest and return its two reviewed sets."""

    images_root = Path(root) if root is not None else _default_runtime_images_root()
    if not images_root.is_dir():
        raise ValueError(
            f"runner image tree is missing at {images_root}; the certification "
            "matrix requires packaged runtime-images/ (fail closed)"
        )
    manifest_path = images_root / RUNNER_IMAGES_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(
            f"runner image manifest is missing or invalid at {manifest_path} "
            "(fail closed)"
        )
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError(
            f"runner image manifest is missing or invalid at {manifest_path} "
            "(fail closed)"
        ) from None
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "files",
        "images",
    }:
        raise ValueError("runner image manifest has unexpected keys (fail closed)")
    if manifest.get("schema_version") != RUNNER_IMAGES_MANIFEST_SCHEMA:
        raise ValueError(
            "runner image manifest has an unsupported schema (fail closed)"
        )
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("runner image manifest has no files (fail closed)")
    if any(not isinstance(value, str) for value in raw_files):
        raise ValueError("runner image manifest paths must be strings (fail closed)")
    if raw_files != sorted(raw_files) or len(raw_files) != len(set(raw_files)):
        raise ValueError(
            "runner image manifest paths must be sorted and unique (fail closed)"
        )
    raw_images = manifest.get("images")
    if (
        not isinstance(raw_images, dict)
        or tuple(raw_images) != RUNNER_IMAGE_KEYS
        or any(
            not isinstance(value, str) or _PINNED_IMAGE.fullmatch(value) is None
            for value in raw_images.values()
        )
    ):
        raise ValueError(
            "runner image manifest must pin the complete sorted image set "
            "(fail closed)"
        )
    return images_root, raw_files, dict(raw_images)


def runner_image_refs(root: Path | None = None) -> dict[str, str]:
    """Return the reviewed, content-addressed Terraform and dbt image refs."""

    _, _, images = _runner_manifest(root)
    return images


def runner_images_digest(root: Path | None = None) -> str:
    """Digest published image references and all listed build inputs."""

    images_root, raw_files, images = _runner_manifest(root)
    checksums: dict[str, str] = {}
    resolved_root = images_root.resolve()
    for raw in raw_files:
        relative = PurePosixPath(raw)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError(
                "runner image manifest contains an unsafe path (fail closed)"
            )
        path = images_root.joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            raise ValueError(
                f"runner image manifest file is missing or unsafe: {raw} "
                "(fail closed)"
            ) from None
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                f"runner image manifest file is missing: {raw} (fail closed)"
            )
        checksums[raw] = _file_sha256(path)
    return sha256_hex(
        canonical_json(
            {
                "schema_version": RUNNER_IMAGES_MANIFEST_SCHEMA,
                "files": checksums,
                "images": images,
            }
        )
    )


def _certification_matrix_keys() -> frozenset[str]:
    """The exact field closure defined by the current matrix recipe."""

    keys = {
        "matrix_version",
        "certification_stage_evidence_schema_version",
        "certification_attestation_schema_version",
        "certification_pending_schema_version",
        "canonical_fingerprint_version",
        "parity_contract_version",
        "parity_observation_schema_version",
        "parity_registry_digest",
        "parity_scope_policy_version",
        "airbyte_abctl",
        "airbyte_chart",
        "destination",
        "destination_definition_id",
        "destination_connector",
        "dbt_adapter",
        "dbt_adapter_version",
        "dbt_core_version",
        "warehouse:logical_namespace_field",
        "warehouse:physical_container_field",
        "warehouse:fixed_schema",
        "runner_images_digest",
        "runner_image:dbt",
        "runner_image:terraform",
        "source_connector:custom_api",
    }
    keys.update(
        f"source_connector:{section}" for section in SOURCE_CONNECTOR_CONTRACTS
    )
    keys.update(
        f"source_service_image:{service}" for service in SOURCE_SERVICE_IMAGES
    )
    return frozenset(keys)


_V10_CERTIFICATION_MATRIX_KEYS = frozenset(
    {
        "matrix_version",
        "certification_stage_evidence_schema_version",
        "certification_attestation_schema_version",
        "certification_pending_schema_version",
        "canonical_fingerprint_version",
        "parity_contract_version",
        "parity_observation_schema_version",
        "parity_registry_digest",
        "parity_scope_policy_version",
        "airbyte_abctl",
        "airbyte_chart",
        "destination",
        "destination_definition_id",
        "destination_connector",
        "dbt_adapter",
        "dbt_adapter_version",
        "dbt_core_version",
        "warehouse:logical_namespace_field",
        "warehouse:physical_container_field",
        "warehouse:fixed_schema",
        "runner_images_digest",
        "runner_image:dbt",
        "runner_image:terraform",
        "source_connector:aws_s3",
        "source_connector:custom_api",
        "source_connector:flat_files",
        "source_connector:mongodb",
        "source_connector:postgres",
        "source_service_image:elt-api",
        "source_service_image:elt-files",
        "source_service_image:elt-localstack",
        "source_service_image:elt-mongodb",
        "source_service_image:elt-postgres",
    }
)

# Keep v10 destination values literal so later contracts cannot change them.
_V10_WAREHOUSE_FIELDS: dict[Destination, dict[str, str]] = {
    Destination.SNOWFLAKE: {
        "warehouse:logical_namespace_field": "database",
        "warehouse:physical_container_field": "",
        "warehouse:fixed_schema": "AIRBYTE_SCHEMA",
    },
    Destination.DATABRICKS: {
        "warehouse:logical_namespace_field": "schema",
        "warehouse:physical_container_field": "database",
        "warehouse:fixed_schema": "",
    },
    Destination.REDSHIFT: {
        "warehouse:logical_namespace_field": "schema",
        "warehouse:physical_container_field": "database",
        "warehouse:fixed_schema": "",
    },
}


def _validate_certification_matrix_recipe(
    matrix: Mapping[str, str],
    destination: Destination | str,
    *,
    matrix_version: str,
    stage_evidence_schema_version: str,
    attestation_schema_version: str,
    pending_schema_version: str,
    expected_keys: frozenset[str],
    warehouse_fields: Mapping[str, str] | None = None,
    semantic_fields: Mapping[str, str] | None = None,
    dbt_fields: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if not isinstance(matrix, Mapping):
        raise ValueError("certification matrix must be a mapping (fail closed)")
    normalized: dict[str, str] = {}
    for key, value in matrix.items():
        if not isinstance(key, str) or not key:
            raise ValueError("certification matrix has an invalid key (fail closed)")
        if not isinstance(value, str):
            raise ValueError(
                f"certification matrix value for {key!r} is not text "
                "(fail closed)"
            )
        normalized[key] = value

    missing = sorted(expected_keys - set(normalized))
    extra = sorted(set(normalized) - expected_keys)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append(f"missing keys: {missing}")
        if extra:
            detail.append(f"unexpected keys: {extra}")
        raise ValueError(
            f"certification matrix does not match the closed v{matrix_version} "
            "schema ("
            + "; ".join(detail)
            + ")"
        )

    contract = destination_contract(destination)
    expected_warehouse_fields = dict(warehouse_fields or {})
    if not expected_warehouse_fields:
        expected_warehouse_fields = {
            "warehouse:logical_namespace_field": contract.logical_namespace_field,
            "warehouse:physical_container_field": (
                contract.physical_container_field or ""
            ),
            "warehouse:fixed_schema": contract.fixed_schema or "",
        }
    expected_semantic_fields = dict(semantic_fields or {})
    if not expected_semantic_fields:
        expected_semantic_fields = {
            "canonical_fingerprint_version": CANONICAL_FINGERPRINT_VERSION,
            "parity_contract_version": PARITY_CONTRACT_VERSION,
            "parity_observation_schema_version": PARITY_OBSERVATION_SCHEMA_VERSION,
            "parity_registry_digest": PARITY_REGISTRY_DIGEST,
            "parity_scope_policy_version": PARITY_SCOPE_POLICY_VERSION,
        }
    expected_dbt_fields = dict(dbt_fields or {})
    if not expected_dbt_fields:
        adapter_name, adapter_version = DBT_ADAPTER_CONTRACTS[contract.destination]
        expected_dbt_fields = {
            "dbt_adapter": adapter_name,
            "dbt_adapter_version": adapter_version,
            "dbt_core_version": DBT_CORE_VERSION,
        }
    exact_values = {
        "matrix_version": matrix_version,
        "certification_stage_evidence_schema_version": stage_evidence_schema_version,
        "certification_attestation_schema_version": attestation_schema_version,
        "certification_pending_schema_version": pending_schema_version,
        "destination": contract.destination.value,
        **expected_semantic_fields,
        **expected_dbt_fields,
        **expected_warehouse_fields,
    }
    mismatched = sorted(
        key for key, expected in exact_values.items() if normalized[key] != expected
    )
    if mismatched:
        raise ValueError(
            f"certification matrix fields do not match the v{matrix_version} "
            "verifier contract: "
            f"{mismatched}"
        )

    allowed_empty = {
        "warehouse:physical_container_field",
        "warehouse:fixed_schema",
    }
    empty = sorted(
        key
        for key, value in normalized.items()
        if not value and key not in allowed_empty
    )
    if empty:
        raise ValueError(
            f"certification matrix has empty required pins: {empty}"
        )
    unpinned = sorted(
        key
        for key, value in normalized.items()
        if value.casefold() in _UNPINNED_VALUES or "unpinned" in value.casefold()
    )
    if unpinned:
        raise ValueError(
            f"certification matrix contains unpinned values: {unpinned}"
        )
    if _SHA256.fullmatch(normalized["runner_images_digest"]) is None:
        raise ValueError(
            "certification matrix runner_images_digest is not a sha256"
        )
    image_fields = {
        "runner_image:dbt",
        "runner_image:terraform",
        *(key for key in normalized if key.startswith("source_service_image:")),
    }
    invalid_images = sorted(
        key
        for key in image_fields
        if _PINNED_IMAGE.fullmatch(normalized[key]) is None
    )
    if invalid_images:
        raise ValueError(
            "certification matrix image references are not exact @sha256 pins: "
            f"{invalid_images}"
        )
    return dict(sorted(normalized.items()))


def validate_certification_matrix(
    matrix: Mapping[str, str],
    destination: Destination | str,
) -> dict[str, str]:
    """Validate the current closed matrix against the expected destination."""

    return _validate_certification_matrix_recipe(
        matrix,
        destination,
        matrix_version=CERTIFICATION_MATRIX_VERSION,
        stage_evidence_schema_version=(
            CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION
        ),
        attestation_schema_version=CERTIFICATION_ATTESTATION_SCHEMA_VERSION,
        pending_schema_version=CERTIFICATION_PENDING_SCHEMA_VERSION,
        expected_keys=_certification_matrix_keys(),
    )


def validate_recorded_certification_matrix(
    matrix: Mapping[str, str],
    destination: Destination | str,
) -> dict[str, str]:
    """Validate a frozen matrix using its recorded version, starting at v10.

    v10 and v11 share their semantic, warehouse and dbt fields literally.
    """

    if not isinstance(matrix, Mapping):
        raise ValueError("certification matrix must be a mapping (fail closed)")
    version = matrix.get("matrix_version")
    if version == CERTIFICATION_MATRIX_VERSION:
        return validate_certification_matrix(matrix, destination)
    if version == "10":
        contract = destination_contract(destination)
        return _validate_certification_matrix_recipe(
            matrix,
            contract.destination,
            matrix_version="10",
            stage_evidence_schema_version=_V10_STAGE_EVIDENCE_SCHEMA_VERSION,
            attestation_schema_version=_V10_ATTESTATION_SCHEMA_VERSION,
            pending_schema_version=_V10_PENDING_SCHEMA_VERSION,
            expected_keys=_V10_CERTIFICATION_MATRIX_KEYS,
            warehouse_fields=_V10_WAREHOUSE_FIELDS[contract.destination],
            semantic_fields=_V10_SEMANTIC_FIELDS,
            dbt_fields=_V10_DBT_FIELDS[contract.destination],
        )
    if version == "11":
        contract = destination_contract(destination)
        return _validate_certification_matrix_recipe(
            matrix,
            contract.destination,
            matrix_version="11",
            stage_evidence_schema_version=_V11_STAGE_EVIDENCE_SCHEMA_VERSION,
            attestation_schema_version=_V11_ATTESTATION_SCHEMA_VERSION,
            pending_schema_version=_V11_PENDING_SCHEMA_VERSION,
            expected_keys=_V10_CERTIFICATION_MATRIX_KEYS,
            warehouse_fields=_V10_WAREHOUSE_FIELDS[contract.destination],
            semantic_fields=_V10_SEMANTIC_FIELDS,
            dbt_fields=_V10_DBT_FIELDS[contract.destination],
        )
    raise ValueError(
        "certification matrix records unsupported closed recipe version "
        f"{version!r} (fail closed)"
    )


def certification_matrix(destination: Destination | str) -> dict[str, str]:
    """Return the sorted, secret-free execution matrix for one destination."""
    contract = destination_contract(destination)
    images = runner_image_refs()
    adapter_name, adapter_version = DBT_ADAPTER_CONTRACTS[contract.destination]
    matrix: dict[str, str] = {
        "matrix_version": CERTIFICATION_MATRIX_VERSION,
        "certification_stage_evidence_schema_version": (
            CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION
        ),
        "certification_attestation_schema_version": (
            CERTIFICATION_ATTESTATION_SCHEMA_VERSION
        ),
        "certification_pending_schema_version": CERTIFICATION_PENDING_SCHEMA_VERSION,
        "canonical_fingerprint_version": CANONICAL_FINGERPRINT_VERSION,
        "parity_contract_version": PARITY_CONTRACT_VERSION,
        "parity_observation_schema_version": PARITY_OBSERVATION_SCHEMA_VERSION,
        "parity_registry_digest": PARITY_REGISTRY_DIGEST,
        "parity_scope_policy_version": PARITY_SCOPE_POLICY_VERSION,
        "airbyte_abctl": AIRBYTE_ABCTL_VERSION,
        "airbyte_chart": AIRBYTE_CHART_VERSION,
        "destination": contract.destination.value,
        "destination_definition_id": contract.definition_id,
        "destination_connector": contract.connector_version or "legacy-unpinned",
        "dbt_adapter": adapter_name,
        "dbt_adapter_version": adapter_version,
        "dbt_core_version": DBT_CORE_VERSION,
        "warehouse:logical_namespace_field": contract.logical_namespace_field,
        "warehouse:physical_container_field": (
            contract.physical_container_field or ""
        ),
        "warehouse:fixed_schema": contract.fixed_schema or "",
        "runner_images_digest": runner_images_digest(),
        "runner_image:dbt": images[f"dbt:{contract.destination.value}"],
        "runner_image:terraform": images["terraform"],
    }
    for section in sorted(SOURCE_CONNECTOR_CONTRACTS):
        matrix[f"source_connector:{section}"] = SOURCE_CONNECTOR_CONTRACTS[
            section
        ].connector_version
    matrix["source_connector:custom_api"] = CUSTOM_API_MANIFEST_VERSION
    for service, image in sorted(SOURCE_SERVICE_IMAGES.items()):
        matrix[f"source_service_image:{service}"] = image
    return validate_certification_matrix(matrix, contract.destination)


__all__ = [
    "AIRBYTE_ABCTL_VERSION",
    "AIRBYTE_CHART_VERSION",
    "CERTIFICATION_MATRIX_VERSION",
    "CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION",
    "CERTIFICATION_ATTESTATION_SCHEMA_VERSION",
    "CERTIFICATION_PENDING_SCHEMA_VERSION",
    "RUNTIME_IMAGES_DIRNAME",
    "RUNNER_IMAGES_MANIFEST",
    "RUNNER_IMAGES_MANIFEST_SCHEMA",
    "RUNNER_IMAGE_KEYS",
    "certification_matrix",
    "runner_image_refs",
    "runner_images_digest",
    "validate_certification_matrix",
    "validate_recorded_certification_matrix",
]

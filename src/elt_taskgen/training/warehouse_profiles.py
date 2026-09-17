"""Define versioned proxy representation profiles without runtime compatibility claims."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from elt_taskgen.destinations import Destination


WAREHOUSE_PROFILE_SCHEMA_VERSION = "warehouse-behavior-v1"


class IdentifierFolding(str, Enum):
    UPPER = "upper"
    LOWER = "lower"
    PRESERVE = "preserve"


class JsonScalarMode(str, Enum):
    LOGICAL = "logical"
    JSON_TEXT = "json_text"


class WarehouseBehaviorProfile(BaseModel):
    """Closed, immutable behavior envelope for one destination personality."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = WAREHOUSE_PROFILE_SCHEMA_VERSION
    profile_id: str
    destination: Destination
    database_role: str
    identifier_folding: IdentifierFolding
    quoted_identifiers_preserve_case: bool = True
    decimal_type: str = "DECIMAL(38,9)"
    timestamp_normalization: str = "utc_microseconds"
    boolean_normalization: str = "strict_boolean"
    json_normalization: str = "canonical_json"
    json_scalar_mode: JsonScalarMode = JsonScalarMode.LOGICAL
    empty_string_is_null: bool
    append_mode: str = "full_refresh_append"
    metadata_columns: tuple[str, ...] = (
        "_airbyte_raw_id",
        "_airbyte_extracted_at",
        "_airbyte_meta",
        "_airbyte_generation_id",
    )
    stable_errors: Mapping[str, str]

    def fold_identifier(self, value: str, *, quoted: bool = False) -> str:
        if not value or "\x00" in value:
            raise ValueError(self.error("unsupported_identifier"))
        if quoted and self.quoted_identifiers_preserve_case:
            return value
        if self.identifier_folding is IdentifierFolding.UPPER:
            return value.upper()
        if self.identifier_folding is IdentifierFolding.LOWER:
            return value.lower()
        return value

    def normalize_scalar(self, value: Any, *, logical_type: str | None = None) -> Any:
        """Normalize the explicitly admitted scalar representation subset."""

        if value == "" and self.empty_string_is_null:
            return None
        kind = (logical_type or "").casefold()
        if value is None:
            return None
        if kind in {"decimal", "numeric", "number", "decimal(38,9)"}:
            try:
                return Decimal(str(value)).quantize(
                    Decimal("0.000000001"), rounding=ROUND_HALF_EVEN
                )
            except Exception as error:
                raise ValueError(self.error("invalid_decimal")) from error
        if kind in {"boolean", "bool"}:
            if isinstance(value, bool):
                return value
            raise ValueError(self.error("invalid_boolean"))
        if kind in {"json", "variant", "super"}:
            parsed = value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError as error:
                    raise ValueError(self.error("invalid_json")) from error
            if self.json_scalar_mode is JsonScalarMode.JSON_TEXT and not isinstance(
                parsed, (dict, list)
            ):
                return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            return json.dumps(
                parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        return value

    def error(self, condition: str) -> str:
        try:
            return self.stable_errors[condition]
        except KeyError as error:
            raise ValueError("warehouse_profile_unsupported_behavior") from error


def _errors(prefix: str) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "unsupported_identifier": f"{prefix}_identifier_unsupported",
            "invalid_decimal": f"{prefix}_decimal_invalid",
            "invalid_boolean": f"{prefix}_boolean_invalid",
            "invalid_json": f"{prefix}_json_invalid",
            "unsupported_behavior": f"{prefix}_behavior_unsupported",
        }
    )


_PROFILES: Mapping[Destination, WarehouseBehaviorProfile] = MappingProxyType(
    {
        Destination.SNOWFLAKE: WarehouseBehaviorProfile(
            profile_id="snowflake-airbyte-2026-09-02-v1",
            destination=Destination.SNOWFLAKE,
            database_role="database",
            identifier_folding=IdentifierFolding.UPPER,
            empty_string_is_null=True,
            stable_errors=_errors("snowflake"),
        ),
        Destination.DATABRICKS: WarehouseBehaviorProfile(
            profile_id="databricks-airbyte-4.0.2-2026-09-02-v1",
            destination=Destination.DATABRICKS,
            database_role="catalog",
            identifier_folding=IdentifierFolding.PRESERVE,
            json_scalar_mode=JsonScalarMode.JSON_TEXT,
            empty_string_is_null=False,
            stable_errors=_errors("databricks"),
        ),
        Destination.REDSHIFT: WarehouseBehaviorProfile(
            profile_id="redshift-airbyte-2026-09-02-v1",
            destination=Destination.REDSHIFT,
            database_role="database",
            identifier_folding=IdentifierFolding.LOWER,
            empty_string_is_null=True,
            stable_errors=_errors("redshift"),
        ),
    }
)


def warehouse_profile(destination: Destination | str) -> WarehouseBehaviorProfile:
    """Return the frozen supported profile, failing closed for other targets."""

    try:
        key = destination if isinstance(destination, Destination) else Destination(destination)
        return _PROFILES[key]
    except (KeyError, ValueError) as error:
        raise ValueError("warehouse_profile_unsupported_destination") from error


__all__ = [
    "IdentifierFolding",
    "JsonScalarMode",
    "WAREHOUSE_PROFILE_SCHEMA_VERSION",
    "WarehouseBehaviorProfile",
    "warehouse_profile",
]

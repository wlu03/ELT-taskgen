"""Create destination-neutral relation fingerprints from TaskIR logical types.

Canonical forms cover integers, decimals, finite floats, text, booleans, dates, UTC
timestamps, and structural JSON. Outputs contain hashes and row counts, never rows. They
remain private evidence unless protected against low-entropy guessing.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from elt_taskgen.models import ColumnType, canonical_json, sha256_hex


CANONICAL_FINGERPRINT_VERSION = "1"
DECIMAL_PRECISION = 38
DECIMAL_SCALE = 9
_DECIMAL_QUANTUM = decimal.Decimal(1).scaleb(-DECIMAL_SCALE)
_INTEGER_BOUNDS = {
    ColumnType.INTEGER: (-(2**31), 2**31 - 1),
    ColumnType.BIGINT: (-(2**63), 2**63 - 1),
}
_TIMESTAMP_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)

# Reviewed connector fields allowed only through `allowed_extra_columns`.
# Other `_ab_*` or underscore-prefixed business columns remain forbidden.
_AIRBYTE_SOURCE_METADATA_COLUMNS = frozenset(
    {
        "_ab_cdc_cursor",
        "_ab_cdc_deleted_at",
        "_ab_cdc_updated_at",
        "_ab_source_file_last_modified",
        "_ab_source_file_url",
        "_id",
    }
)


def _is_airbyte_metadata_column(name: str) -> bool:
    folded = name.casefold()
    return (
        folded.startswith("_airbyte_")
        or folded in _AIRBYTE_SOURCE_METADATA_COLUMNS
    )


class CanonicalFingerprintError(ValueError):
    """A physical value cannot be represented by the portable type contract."""


class CanonicalRowOrder(str, Enum):
    """Whether row position is part of the compared relation contract."""

    UNORDERED = "unordered"
    ORDERED = "ordered"


class NaiveTimestampPolicy(str, Enum):
    """How a driver datetime without timezone information is interpreted."""

    REJECT = "reject"
    ASSUME_UTC = "assume_utc"


class CanonicalRelationFingerprint(BaseModel):
    """Secret-free exact fingerprint of one projected logical relation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = CANONICAL_FINGERPRINT_VERSION
    row_order: CanonicalRowOrder
    naive_timestamp_policy: NaiveTimestampPolicy
    allowed_extra_columns: tuple[str, ...]
    row_count: StrictInt = Field(ge=0)
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_version(self) -> "CanonicalRelationFingerprint":
        if self.version != CANONICAL_FINGERPRINT_VERSION:
            raise ValueError("unsupported canonical fingerprint version")
        if tuple(sorted(set(self.allowed_extra_columns))) != self.allowed_extra_columns:
            raise ValueError("allowed_extra_columns must be sorted and unique")
        if any(
            not _is_airbyte_metadata_column(name)
            for name in self.allowed_extra_columns
        ):
            raise ValueError("allowed_extra_columns contains a non-Airbyte name")
        return self


def _decimal_value(value: object, *, label: str) -> decimal.Decimal:
    if isinstance(value, bool):
        raise CanonicalFingerprintError(f"{label}: boolean is not numeric")
    if isinstance(value, decimal.Decimal):
        number = value
    elif isinstance(value, int):
        number = decimal.Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalFingerprintError(f"{label}: non-finite number")
        number = decimal.Decimal(str(value))
    elif isinstance(value, str):
        try:
            number = decimal.Decimal(value)
        except decimal.InvalidOperation as exc:
            raise CanonicalFingerprintError(f"{label}: invalid number text") from exc
    else:
        raise CanonicalFingerprintError(
            f"{label}: unsupported numeric value {type(value).__name__}"
        )
    if not number.is_finite():
        raise CanonicalFingerprintError(f"{label}: non-finite number")
    return number


def _canonical_integer(
    value: object,
    column_type: ColumnType,
    *,
    label: str,
) -> str:
    number = _decimal_value(value, label=label)
    integral = number.to_integral_value()
    if number != integral:
        raise CanonicalFingerprintError(f"{label}: non-integral value")
    resolved = int(integral)
    minimum, maximum = _INTEGER_BOUNDS[column_type]
    if not minimum <= resolved <= maximum:
        raise CanonicalFingerprintError(
            f"{label}: value does not fit {column_type.value}"
        )
    return str(resolved)


def _canonical_decimal(value: object, *, label: str) -> str:
    number = _decimal_value(value, label=label)
    try:
        # Python's process-default Decimal context has precision 28, which is
        # too small for the admitted DECIMAL(38,9) boundary.  Keep this local
        # so parity canonicalization does not mutate process-wide arithmetic.
        with decimal.localcontext() as context:
            context.prec = DECIMAL_PRECISION + DECIMAL_SCALE + 4
            quantized = number.quantize(_DECIMAL_QUANTUM)
            unscaled = quantized.scaleb(DECIMAL_SCALE).to_integral_exact()
    except decimal.InvalidOperation as exc:
        raise CanonicalFingerprintError(
            f"{label}: value does not fit DECIMAL(38,9)"
        ) from exc
    if quantized != number:
        raise CanonicalFingerprintError(
            f"{label}: value has more than {DECIMAL_SCALE} decimal places"
        )
    if quantized == 0:
        quantized = abs(quantized)
    if len(str(abs(int(unscaled)))) > DECIMAL_PRECISION:
        raise CanonicalFingerprintError(
            f"{label}: value does not fit DECIMAL(38,9)"
        )
    return format(quantized, f".{DECIMAL_SCALE}f")


def _canonical_float(value: object, *, label: str) -> str:
    number = _decimal_value(value, label=label)
    if number == 0:
        return "0"
    # Normalize spelling while preserving the exact decimal supplied by the
    # driver. This is intentionally not a tolerance comparison.
    return format(number.normalize(), "f")


def _canonical_boolean(value: object, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        folded = value.casefold()
        if folded in {"true", "false"}:
            return folded == "true"
    raise CanonicalFingerprintError(f"{label}: invalid boolean")


def _canonical_date(value: object, *, label: str) -> str:
    if isinstance(value, dt.datetime):
        raise CanonicalFingerprintError(f"{label}: timestamp supplied for date")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise CanonicalFingerprintError(f"{label}: invalid ISO date") from exc
    raise CanonicalFingerprintError(f"{label}: invalid date")


def _canonical_timestamp(
    value: object,
    *,
    label: str,
    naive_timestamp_policy: NaiveTimestampPolicy,
) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not _TIMESTAMP_TEXT.fullmatch(text):
            raise CanonicalFingerprintError(
                f"{label}: timestamp must include time and at most six fractional digits"
            )
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise CanonicalFingerprintError(f"{label}: invalid ISO timestamp") from exc
    elif isinstance(value, dt.datetime):
        parsed = value
    else:
        raise CanonicalFingerprintError(f"{label}: invalid timestamp")
    if parsed.tzinfo is None:
        if naive_timestamp_policy is NaiveTimestampPolicy.REJECT:
            raise CanonicalFingerprintError(
                f"{label}: naive timestamp requires an explicit interpretation policy"
            )
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    normalized = parsed.astimezone(dt.timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_number(value: object, *, label: str) -> str:
    number = _decimal_value(value, label=label)
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _json_node(value: object, *, label: str) -> object:
    """Tagged structural JSON; distinguishes null, strings, and numbers."""

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, (int, float, decimal.Decimal)) and not isinstance(
        value, bool
    ):
        return ["number", _json_number(value, label=label)]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return [
            "array",
            [
                _json_node(item, label=f"{label}[{index}]")
                for index, item in enumerate(value)
            ],
        ]
    if isinstance(value, Mapping):
        pairs: list[list[object]] = []
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise CanonicalFingerprintError(
                f"{label}: JSON object key is not text"
            )
        for key in sorted(keys):
            pairs.append(
                [key, _json_node(value[key], label=f"{label}.{key}")]
            )
        return ["object", pairs]
    raise CanonicalFingerprintError(
        f"{label}: unsupported JSON value {type(value).__name__}"
    )


def _canonical_json_value(value: object, *, label: str) -> str:
    if isinstance(value, str):
        def reject_duplicate_keys(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise CanonicalFingerprintError(
                        f"{label}: duplicate JSON object key {key!r}"
                    )
                result[key] = item
            return result

        def reject_constant(constant: str) -> object:
            raise CanonicalFingerprintError(
                f"{label}: non-standard JSON number {constant}"
            )

        try:
            value = json.loads(
                value,
                parse_float=decimal.Decimal,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_constant,
            )
        except json.JSONDecodeError as exc:
            raise CanonicalFingerprintError(f"{label}: invalid JSON text") from exc
    return canonical_json(_json_node(value, label=label))


def canonical_cell(
    value: object,
    column_type: ColumnType,
    *,
    label: str,
    naive_timestamp_policy: NaiveTimestampPolicy | str = NaiveTimestampPolicy.REJECT,
) -> object:
    """Return a JSON-safe tagged value under one TaskIR logical type."""

    if value is None:
        return ["sql_null"]
    if column_type in {ColumnType.INTEGER, ColumnType.BIGINT}:
        return [
            column_type.value,
            _canonical_integer(value, column_type, label=label),
        ]
    if column_type is ColumnType.FLOAT:
        return ["float", _canonical_float(value, label=label)]
    if column_type is ColumnType.DECIMAL:
        return ["decimal", _canonical_decimal(value, label=label)]
    if column_type is ColumnType.TEXT:
        if not isinstance(value, str):
            raise CanonicalFingerprintError(f"{label}: non-text value")
        return ["text", value]
    if column_type is ColumnType.BOOLEAN:
        return ["boolean", _canonical_boolean(value, label=label)]
    if column_type is ColumnType.DATE:
        return ["date", _canonical_date(value, label=label)]
    if column_type is ColumnType.TIMESTAMP:
        try:
            resolved_timestamp_policy = NaiveTimestampPolicy(
                naive_timestamp_policy
            )
        except ValueError as exc:
            raise CanonicalFingerprintError(
                f"unsupported naive-timestamp policy {naive_timestamp_policy!r}"
            ) from exc
        return [
            "timestamp_utc",
            _canonical_timestamp(
                value,
                label=label,
                naive_timestamp_policy=resolved_timestamp_policy,
            ),
        ]
    if column_type is ColumnType.JSON:
        return ["json", _canonical_json_value(value, label=label)]
    raise CanonicalFingerprintError(
        f"{label}: unsupported logical type {column_type!r}"
    )


def _column_alignment(
    actual_columns: Sequence[str],
    expected_columns: Sequence[tuple[str, ColumnType]],
    *,
    allowed_extra_columns: Collection[str],
) -> tuple[int, ...]:
    if not actual_columns:
        raise CanonicalFingerprintError("actual relation has no columns")
    actual_by_folded: dict[str, tuple[int, str]] = {}
    for index, name in enumerate(actual_columns):
        if not isinstance(name, str) or not name:
            raise CanonicalFingerprintError("actual relation has an invalid column")
        folded = name.casefold()
        if folded in actual_by_folded:
            raise CanonicalFingerprintError(
                f"actual relation has a case-folding collision for {name!r}"
            )
        actual_by_folded[folded] = (index, name)

    expected_names = [name for name, _ in expected_columns]
    if len(expected_names) != len({name.casefold() for name in expected_names}):
        raise CanonicalFingerprintError(
            "expected relation has duplicate case-insensitive columns"
        )
    missing = [name for name in expected_names if name.casefold() not in actual_by_folded]
    if missing:
        raise CanonicalFingerprintError(f"actual relation is missing columns {missing}")
    expected_folded = {name.casefold() for name in expected_names}
    extras = [
        original
        for folded, (_, original) in actual_by_folded.items()
        if folded not in expected_folded
    ]
    allowed_folded: set[str] = set()
    for name in allowed_extra_columns:
        if not isinstance(name, str) or not name:
            raise CanonicalFingerprintError(
                "allowed extra-column policy contains an invalid name"
            )
        folded = name.casefold()
        if not _is_airbyte_metadata_column(folded):
            raise CanonicalFingerprintError(
                "only explicit _airbyte_ or reviewed source metadata columns "
                "may be ignored"
            )
        if folded in allowed_folded:
            raise CanonicalFingerprintError(
                "allowed extra-column policy has a case-folding collision"
            )
        allowed_folded.add(folded)
    extras = [name for name in extras if name.casefold() not in allowed_folded]
    if extras:
        raise CanonicalFingerprintError(
            f"actual relation has unexpected business columns {sorted(extras)}"
        )
    return tuple(actual_by_folded[name.casefold()][0] for name in expected_names)


def _row_values(
    row: object,
    actual_columns: Sequence[str],
    alignment: Sequence[int],
) -> tuple[object, ...]:
    if isinstance(row, Mapping):
        folded: dict[str, object] = {}
        for key, value in row.items():
            if not isinstance(key, str):
                raise CanonicalFingerprintError("row mapping has a non-text key")
            normalized = key.casefold()
            if normalized in folded:
                raise CanonicalFingerprintError(
                    f"row mapping has a case-folding collision for {key!r}"
                )
            folded[normalized] = value
        try:
            return tuple(folded[actual_columns[index].casefold()] for index in alignment)
        except KeyError as exc:
            raise CanonicalFingerprintError("row mapping is missing a column") from exc
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        if len(row) != len(actual_columns):
            raise CanonicalFingerprintError(
                "row width does not match the actual column list"
            )
        return tuple(row[index] for index in alignment)
    raise CanonicalFingerprintError("row is neither a mapping nor a sequence")


def canonical_relation_fingerprint(
    *,
    actual_columns: Sequence[str],
    expected_columns: Sequence[tuple[str, ColumnType]],
    rows: Sequence[object],
    allowed_extra_columns: Collection[str] = (),
    row_order: CanonicalRowOrder | str = CanonicalRowOrder.UNORDERED,
    naive_timestamp_policy: NaiveTimestampPolicy | str = NaiveTimestampPolicy.REJECT,
) -> CanonicalRelationFingerprint:
    """Hash one relation after logical projection, typing, and order policy."""

    if not expected_columns:
        raise CanonicalFingerprintError("expected relation has no columns")
    try:
        resolved_row_order = CanonicalRowOrder(row_order)
    except ValueError as exc:
        raise CanonicalFingerprintError(
            f"unsupported row-order policy {row_order!r}"
        ) from exc
    try:
        resolved_timestamp_policy = NaiveTimestampPolicy(naive_timestamp_policy)
    except ValueError as exc:
        raise CanonicalFingerprintError(
            f"unsupported naive-timestamp policy {naive_timestamp_policy!r}"
        ) from exc
    alignment = _column_alignment(
        actual_columns,
        expected_columns,
        allowed_extra_columns=allowed_extra_columns,
    )
    normalized_allowed_extras = tuple(
        sorted(name.casefold() for name in allowed_extra_columns)
    )
    schema_payload = {
        "columns": [
            {"name": name, "type": column_type.value}
            for name, column_type in expected_columns
        ],
        "row_order": resolved_row_order.value,
        "naive_timestamp_policy": resolved_timestamp_policy.value,
        "allowed_extra_columns": normalized_allowed_extras,
    }
    canonical_rows: list[object] = []
    for row_index, row in enumerate(rows):
        values = _row_values(row, actual_columns, alignment)
        canonical_rows.append(
            [
                canonical_cell(
                    value,
                    column_type,
                    label=f"row {row_index} column {name}",
                    naive_timestamp_policy=resolved_timestamp_policy,
                )
                for value, (name, column_type) in zip(
                    values, expected_columns, strict=True
                )
            ]
        )
    if resolved_row_order is CanonicalRowOrder.UNORDERED:
        canonical_rows.sort(key=canonical_json)
    return CanonicalRelationFingerprint(
        row_order=resolved_row_order,
        naive_timestamp_policy=resolved_timestamp_policy,
        allowed_extra_columns=normalized_allowed_extras,
        row_count=len(canonical_rows),
        schema_digest=sha256_hex(canonical_json(schema_payload)),
        result_digest=sha256_hex(canonical_json(canonical_rows)),
    )


__all__ = [
    "CANONICAL_FINGERPRINT_VERSION",
    "DECIMAL_PRECISION",
    "DECIMAL_SCALE",
    "CanonicalFingerprintError",
    "CanonicalRelationFingerprint",
    "CanonicalRowOrder",
    "NaiveTimestampPolicy",
    "canonical_cell",
    "canonical_relation_fingerprint",
]

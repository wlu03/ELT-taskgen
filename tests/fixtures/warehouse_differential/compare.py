"""Typed comparison for the warehouse differential (collector version 2).

Version 1 compared display strings, so SQL NULL matched the text 'NULL' and
TRUE matched the text 'true', and its "within tolerance" used a symmetric
formula that is not the scorer's. Its recorded files are left as they were
collected.

Each value is recorded losslessly with its driver column type, compared by
logical type and exact value first, and rendered as text only for reports.
Agreement under the reward rule is a separate result computed by the scorer's
own comparator. `encode` uses only the standard library because it also runs
inside the pinned DuckDB runtime, which has no elt_taskgen.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

COLLECTOR_VERSION = "2"
#: The scorer's comparator is asymmetric (the tolerance scales with the
#: candidate), so the order is fixed: the destination's own result is the
#: reference and the grading engine's result is the candidate.
REWARD_RULE_ORDER = "reference=warehouse, candidate=duckdb"

#: Driver column types whose values are JSON documents, per engine.
JSON_COLUMN_TYPES = {
    "duckdb": {"JSON"},
    "snowflake": {"5", "9", "10", "17"},  # VARIANT, OBJECT, ARRAY, MAP
    "databricks": {"variant"},
    "redshift": {"4000"},  # SUPER
}


def encode(value, column_type=""):
    """A JSON-safe, lossless record of one driver value and its column type."""
    column = str(column_type if column_type is not None else "")
    if value is None:
        return {"py": "none", "column": column}
    if isinstance(value, bool):
        return {"py": "bool", "v": value, "column": column}
    if isinstance(value, int):
        return {"py": "int", "v": str(value), "column": column}
    if isinstance(value, Decimal):
        return {"py": "decimal", "v": str(value), "column": column}
    if isinstance(value, float):
        return {"py": "float", "v": repr(value), "column": column}
    if isinstance(value, str):
        return {"py": "str", "v": value, "column": column}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"py": "bytes", "v": bytes(value).hex(), "column": column}
    if isinstance(value, dt.datetime):
        return {"py": "datetime", "v": value.isoformat(), "column": column}
    if isinstance(value, dt.date):
        return {"py": "date", "v": value.isoformat(), "column": column}
    if isinstance(value, dt.time):
        return {"py": "time", "v": value.isoformat(), "column": column}
    if isinstance(value, (list, tuple, dict)):
        return {"py": "container", "v": json.dumps(value, sort_keys=True, default=str), "column": column}
    return {"py": type(value).__name__, "v": str(value), "column": column}


def decode(encoded):
    """The driver value `encode` recorded (exact for numbers and text)."""
    kind, text = encoded["py"], encoded.get("v")
    if kind == "none":
        return None
    if kind == "bool":
        return bool(text)
    if kind == "int":
        return int(text)
    if kind == "decimal":
        return Decimal(text)
    if kind == "float":
        return float(text)
    if kind == "datetime":
        return dt.datetime.fromisoformat(text)
    if kind == "date":
        return dt.date.fromisoformat(text)
    if kind == "time":
        return dt.time.fromisoformat(text)
    return text


def typed(encoded, engine):
    """[logical type, exact value]. Integers and decimals share the logical
    type "number", so 5 and 5.0 agree; SQL NULL, JSON null, empty text and
    the text 'NULL' are four different values."""
    from elt_taskgen.models import ColumnType
    from elt_taskgen.verification.canonical_fingerprint import (
        CanonicalFingerprintError,
        _exact_number_text,
        canonical_cell,
    )

    kind, text = encoded["py"], encoded.get("v")
    if kind == "none":
        return ["sql_null"]
    if kind == "bool":
        return ["boolean", bool(text)]
    if kind in ("int", "decimal", "float"):
        number = Decimal(text)
        if number.is_nan():
            return ["number", "NaN"]
        if number.is_infinite():
            return ["number", "Infinity" if number > 0 else "-Infinity"]
        return ["number", _exact_number_text(number)]
    if kind in ("str", "container"):
        if kind == "container" or encoded.get("column") in JSON_COLUMN_TYPES.get(engine, ()):
            try:
                return canonical_cell(text, ColumnType.JSON, label="probe")
            except CanonicalFingerprintError:
                return ["text", text]
        return ["text", text]
    if kind == "bytes":
        return ["binary", text]
    return [kind, text]


def display(value_typed):
    """Report text for one typed value. Never used for comparison."""
    tag = value_typed[0]
    if tag == "sql_null":
        return "NULL"
    if tag == "boolean":
        return "true" if value_typed[1] else "false"
    return str(value_typed[1])


def reward_rule_agreement(reference, candidate):
    """Whether the scorer would accept `candidate` against gold `reference`.

    The reference takes the gold path (canonical CSV text, then parsed back)
    and the candidate the submission path (`cell_text`), and the scorer's own
    `_vectors_match` decides, so boundaries, argument order, zero, nulls and
    non-finite values follow the reward rule exactly.
    """
    from elt_taskgen.verification import upstream_eval as U

    _columns, gold_rows = U.parse_canonical_csv(
        U.rows_to_canonical_csv([{"v": decode(reference)}], ("v",))
    )
    return U._vectors_match([gold_rows[0]["v"]], [U.cell_text(decode(candidate))])


#: Driver exception names that mean the warehouse could not be asked (network,
#: authentication, session or service), not that it refused the SQL.
_UNMEASURED_ERROR_WORDS = ("Interface", "Operational", "Timeout", "Connection", "Request", "Auth")


def error_kind(exc):
    """Return "unmeasured" for an outage or authentication failure, else "sql"."""
    name = type(exc).__name__
    return "unmeasured" if any(word in name for word in _UNMEASURED_ERROR_WORDS) else "sql"


def outcome(warehouse, warehouse_error_kind, duckdb, engine):
    """The verdict for one probe, including the error cases.

    A deterministic native SQL error where the grading engine produced a value
    is a disagreement: the grader would reward SQL the destination rejects.
    An outage is unmeasured, never evidence about the SQL.
    """
    if warehouse is not None and duckdb is not None:
        return compare(warehouse, duckdb, engine)
    if warehouse is None and warehouse_error_kind == "unmeasured":
        return {"verdict": "unmeasured"}
    if warehouse is None and duckdb is not None:
        return {"verdict": "DIFFERENT", "disagreement": "native_error_local_success"}
    return {"verdict": "not_comparable"}


def compare(warehouse, duckdb, engine):
    """The verdict plus both agreement results for one probe.

    identical        the typed values are equal
    within_tolerance both are numbers, unequal, and the reward rule accepts
    DIFFERENT        anything else, including values the reward rule would
                     accept only because it folds null tokens or letter case
    not_comparable   one side produced no value
    """
    if warehouse is None or duckdb is None:
        return {"verdict": "not_comparable"}
    left, right = typed(warehouse, engine), typed(duckdb, "duckdb")
    exact = left == right
    reward = reward_rule_agreement(warehouse, duckdb)
    if exact:
        verdict = "identical"
    elif left[0] == right[0] == "number" and reward:
        verdict = "within_tolerance"
    else:
        verdict = "DIFFERENT"
    return {
        "verdict": verdict,
        "exact_agreement": exact,
        "reward_rule_agreement": reward,
        "warehouse_typed": left,
        "duckdb_typed": right,
    }

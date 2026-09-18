"""Provide the shared reward comparator for acceptance, calibration, RLVR, and evaluation.

Stage one requires exact per-table row counts. Stage two sorts deterministically and
returns the fraction of marts matching under the pinned tolerant comparator.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from elt_taskgen.models import (
    MartSpec,
    PopulationName,
    Row,
    Scalar,
    TaskIR,
    TaskVariant,
)

if TYPE_CHECKING:  # GoldBundle is owned by reference/gold.py (see INTERFACES.md)
    from elt_taskgen.reference.gold import GoldBundle

#: Upstream refined comparator tolerances (eva_stage2.py _vectors_match).
REL_TOL: float = 1e-2
ABS_TOL: float = 1e-9


class RewardResult(BaseModel):
    """One reward evaluation of a submission against frozen gold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage1_pass: bool
    stage1_detail: dict[str, str]
    mart_scores: dict[str, bool]  # mart -> matched
    reward: float  # 0.0 if stage1 fails, else matched/total marts


# Cell canonicalization + numeric coercion

#: pandas default NA sentinels: upstream's ``keep_default_na=True`` reads turn
#: these literal strings into NaN on BOTH sides before comparison or sorting.
#: Omitting any makes this port STRICTER than upstream, which is a parity bug.
_NA_TOKENS: frozenset[str] = frozenset(
    {
        "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan",
        "1.#IND", "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None",
        "n/a", "nan", "null",
    }
)


def _is_null(value: object) -> bool:
    """NULL in the upstream sense: None, float NaN, or a pandas NA token.

    Exact match, no stripping — mirroring the pandas na-filter on the raw CSV
    field, since upstream reads both sides with ``keep_default_na=True``.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return True
    return isinstance(value, str) and value in _NA_TOKENS


def cell_text(value: Scalar) -> str | None:
    """Canonical text form of one cell; None means SQL NULL (empty CSV field).

    Mirrors upstream materializing both sides via ``df.to_csv`` and re-reading
    with ``dtype=str, keep_default_na=True``.
    """
    if _is_null(value):
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return repr(value)  # shortest round-trip form
    return str(value)


def _numeric_value(value: object) -> float | None:
    """Parse a cell using the pinned numeric-string rules, or return `None`.

    The grammar matches upstream pandas behavior rather than Python's more permissive
    `float`: ASCII numeric forms and exact infinities are accepted; NaN, underscores,
    and overflowed exponent text fail.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) else f
    if isinstance(value, str):
        if not value.isascii():
            return None  # pandas' numeric parser is ASCII-only
        text = value.strip()
        if not text or "_" in text:
            return None
        try:
            f = float(text)
        except ValueError:
            return None
        if math.isnan(f):
            return None
        if math.isinf(f):
            # Only an exact unstripped inf token; an overflowing literal fails.
            if text == value and text.lower().lstrip("+-") in ("inf", "infinity"):
                return f
            return None
        return f
    return None


def _column_is_numeric(values: list[object]) -> bool:
    """True iff every non-null value coerces cleanly (all-null => numeric,
    matching pandas' float dtype for an all-NaN column)."""
    return all(_numeric_value(v) is not None for v in values if not _is_null(v))


# Total-order sorting (port of sort_by_keys / _sort_series_key)

def sort_rows(
    rows: list[Row], key_columns: tuple[str, ...], all_columns: tuple[str, ...]
) -> list[Row]:
    """Rows in TOTAL order: key columns first (case-insensitively resolved),
    then EVERY remaining column as tie-breaker. Per-column numeric coercion
    when every non-null value parses; otherwise trimmed lower-cased strings.
    Nulls sort last (upstream ``na_position='last'``). Stable."""
    if not rows:
        return list(rows)
    lower = {c.lower(): c for c in all_columns}
    resolved: list[str] = []
    for k in key_columns:
        actual = lower.get(k.lower())
        if actual is not None and actual not in resolved:
            resolved.append(actual)
    ordered = resolved + [c for c in all_columns if c not in resolved]

    numeric_col = {c: _column_is_numeric([r.get(c) for r in rows]) for c in ordered}

    def row_key(row: Row) -> tuple:
        parts: list[tuple[int, float, str]] = []
        for c in ordered:
            v = row.get(c)
            if _is_null(v):
                parts.append((1, 0.0, ""))
            elif numeric_col[c]:
                num = _numeric_value(v)
                parts.append((0, num if num is not None else 0.0, ""))
            else:
                parts.append((0, 0.0, (cell_text(v) or "").strip().lower()))
        return tuple(parts)

    return sorted(rows, key=row_key)


# Canonical CSV serialization / parsing

def rows_to_canonical_csv(rows: list[Row], columns: tuple[str, ...]) -> str:
    """Deterministic CSV text: header + one line per row, empty field = NULL."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(
            ["" if (t := cell_text(row.get(c))) is None else t for c in columns]
        )
    return buf.getvalue()


def parse_canonical_csv(text: str) -> tuple[tuple[str, ...], list[Row]]:
    """Inverse of rows_to_canonical_csv: header + rows of str-or-None cells.

    Empty fields parse to None (upstream ``keep_default_na=True``). Raises
    ValueError on a ragged row — malformed gold is missing evidence.
    """
    reader = csv.reader(io.StringIO(text))
    raw = list(reader)
    if not raw:
        return (), []
    header = tuple(raw[0])
    rows: list[Row] = []
    for i, line in enumerate(raw[1:], start=2):
        if len(line) != len(header):
            raise ValueError(
                f"CSV row {i}: {len(line)} fields, header has {len(header)}"
            )
        rows.append({c: (v if v != "" else None) for c, v in zip(header, line)})
    return header, rows


# Stage 1: exact row counts (port of eva_stage1.py)

def compare_stage1(
    expected: dict[str, int], actual: dict[str, int]
) -> tuple[bool, dict[str, str]]:
    """Every expected table present (case-insensitive name match) with the
    EXACT expected row count. Extra actual tables are ignored (upstream only
    iterates the expected list). Empty expectations fail closed."""
    if not expected:
        return False, {
            "__evidence__": "no expected stage-1 counts recorded (fail closed)"
        }
    normalized: dict[str, int] = {}
    for name, count in actual.items():
        normalized.setdefault(name.strip().lower(), count)
    detail: dict[str, str] = {}
    ok = True
    for table in sorted(expected):
        want = expected[table]
        got = normalized.get(table.strip().lower())
        if got is None:
            detail[table] = "table not found"
            ok = False
        elif got != want:
            detail[table] = f"expected {want} rows, got {got}"
            ok = False
        else:
            detail[table] = f"ok ({want} rows)"
    return ok, detail


# Stage 2: per-mart column compare (port of check_corretness/_vectors_match)

def _vectors_match(gold: list[object], actual: list[object]) -> bool:
    """Upstream _vectors_match: numeric path iff BOTH columns coerce cleanly,
    then element-wise both-NaN-ok / |g-a| <= ABS_TOL + REL_TOL*|a|; otherwise
    case-insensitive trimmed string compare with both-null tolerance. A
    single-sided null is always a mismatch."""
    if len(gold) != len(actual):
        return False
    if _column_is_numeric(gold) and _column_is_numeric(actual):
        for g, a in zip(gold, actual):
            g_null, a_null = _is_null(g), _is_null(a)
            if g_null and a_null:
                continue
            if g_null or a_null:
                return False
            x = _numeric_value(g)
            y = _numeric_value(a)
            if x is None or y is None:
                return False
            if x == y:
                continue  # covers equal infinities exactly
            if not (math.isfinite(x) and math.isfinite(y)):
                # The tolerance is relative to the submitted value, so an
                # infinite submission would make it infinite and pass.
                return False
            if not (abs(x - y) <= ABS_TOL + REL_TOL * abs(y)):
                return False
        return True
    for g, a in zip(gold, actual):
        g_null, a_null = _is_null(g), _is_null(a)
        if g_null and a_null:
            continue
        if g_null or a_null:
            return False
        if isinstance(g, str) and isinstance(a, str):
            if g.strip().lower() != a.strip().lower():
                return False
        elif g != a:
            return False
    return True


def compare_mart(
    gold_csv: str,
    actual_rows: list[Row],
    mart: MartSpec,
    *,
    actual_columns: Sequence[str] | None = None,
) -> bool:
    """One mart matched iff: row counts equal, every gold column present in the
    actual output (case-insensitive), and every gold column's vector matches
    after both sides are sorted into the mart's total order."""
    try:
        gold_cols, gold_rows = parse_canonical_csv(gold_csv)
    except ValueError:
        return False
    if not gold_cols:
        return False  # empty/malformed gold is missing evidence
    if len(actual_rows) != len(gold_rows):
        return False

    if actual_rows:
        actual_cols = tuple(actual_rows[0].keys())
        if actual_columns is not None:
            described = tuple(actual_columns)
            if tuple(c.casefold() for c in described) != tuple(
                c.casefold() for c in actual_cols
            ):
                return False
        col_set = set(actual_cols)
        if any(set(r.keys()) != col_set for r in actual_rows):
            return False  # ragged submission
        actual_txt: list[Row] = [
            {c: cell_text(r.get(c)) for c in actual_cols} for r in actual_rows
        ]
    else:
        # DB-API callers can preserve the empty result's schema from
        # cursor.description.  Legacy in-memory callers have no such metadata,
        # so retain their historical gold-schema fallback.
        actual_cols = tuple(actual_columns) if actual_columns is not None else gold_cols
        actual_txt = []

    gold_sorted = sort_rows(gold_rows, mart.key_columns, gold_cols)
    actual_sorted = sort_rows(actual_txt, mart.key_columns, actual_cols)

    lower = {c.lower(): c for c in actual_cols}
    for gold_col in gold_cols:
        actual_col = lower.get(gold_col.lower())
        if actual_col is None:
            return False  # missed column
        gv = [r.get(gold_col) for r in gold_sorted]
        av = [r.get(actual_col) for r in actual_sorted]
        if not _vectors_match(gv, av):
            return False
    return True


# THE reward

def evaluate(
    task: TaskIR,
    gold: "GoldBundle",
    population: PopulationName,
    actual_stage1: dict[str, int],
    actual_marts: dict[str, list[Row]],
) -> RewardResult:
    """reward = 0.0 if stage 1 fails, else fraction of fully-correct marts.

    Fail closed: missing frozen gold for the population (stage 1 or any mart)
    scores as failure, never as a pass.
    """
    pop = population.value
    stage1_gold = dict(getattr(gold, "stage1", None) or {}).get(pop)
    if stage1_gold is None:
        return RewardResult(
            stage1_pass=False,
            stage1_detail={
                "__evidence__": f"no frozen stage-1 gold for population {pop!r}"
            },
            mart_scores={},
            reward=0.0,
        )
    ok, detail = compare_stage1(stage1_gold, actual_stage1)
    if not ok:
        return RewardResult(
            stage1_pass=False, stage1_detail=detail, mart_scores={}, reward=0.0
        )
    stage2_gold = dict(getattr(gold, "stage2_csv", None) or {}).get(pop) or {}
    scores: dict[str, bool] = {}
    for mart in task.marts:
        gold_csv = stage2_gold.get(mart.name)
        if gold_csv is None:
            scores[mart.name] = False  # missing frozen gold for this mart
        else:
            scores[mart.name] = compare_mart(
                gold_csv, list(actual_marts.get(mart.name) or []), mart
            )
    # TaskIR forbids an empty mart list; the guard is defensive so an
    # unvalidated construction fails closed (0.0) instead of raising.
    reward = sum(scores.values()) / len(task.marts) if task.marts else 0.0
    return RewardResult(
        stage1_pass=True, stage1_detail=detail, mart_scores=scores, reward=reward
    )


# Variant reward dispatch (thin wrapper — NO new comparator)

def _mart_fraction(
    task: TaskIR,
    stage2_gold: dict[str, str],
    actual_marts: dict[str, list[Row]],
) -> tuple[dict[str, bool], float]:
    """Fraction of fully-correct marts via compare_mart (fail closed on
    missing frozen gold for any mart)."""
    scores: dict[str, bool] = {}
    for mart in task.marts:
        gold_csv = stage2_gold.get(mart.name)
        if gold_csv is None:
            scores[mart.name] = False  # missing frozen gold for this mart
        else:
            scores[mart.name] = compare_mart(
                gold_csv, list(actual_marts.get(mart.name) or []), mart
            )
    # Defensive only (TaskIR forbids zero marts): fail closed, never divide by 0.
    return scores, (sum(scores.values()) / len(task.marts) if task.marts else 0.0)


def evaluate_variant(
    variant: TaskVariant,
    task: TaskIR,
    gold: "GoldBundle",
    population: PopulationName,
    actual_stage1: dict[str, int] | None = None,
    actual_marts: dict[str, list[Row]] | None = None,
) -> RewardResult:
    """Score one export-time variant through the shared reward functions.

    Extract/load is binary on exact table counts, transform scores marts only, and full
    delegates to both stages. Missing gold yields zero.
    """
    variant = TaskVariant(variant)
    pop = population.value
    if variant is TaskVariant.FULL:
        return evaluate(
            task, gold, population, dict(actual_stage1 or {}), dict(actual_marts or {})
        )
    if variant is TaskVariant.EXTRACT_LOAD:
        stage1_gold = dict(getattr(gold, "stage1", None) or {}).get(pop)
        if stage1_gold is None:
            return RewardResult(
                stage1_pass=False,
                stage1_detail={
                    "__evidence__": f"no frozen stage-1 gold for population {pop!r}"
                },
                mart_scores={},
                reward=0.0,
            )
        ok, detail = compare_stage1(stage1_gold, dict(actual_stage1 or {}))
        return RewardResult(
            stage1_pass=ok,
            stage1_detail=detail,
            mart_scores={},
            reward=1.0 if ok else 0.0,
        )
    # TRANSFORM
    provided = {
        "__provided__": (
            "transform variant: the stage-1 warehouse is provided by the "
            "bundle; stage 1 is not scored"
        )
    }
    stage2_gold = dict(getattr(gold, "stage2_csv", None) or {}).get(pop)
    if stage2_gold is None:
        return RewardResult(
            stage1_pass=True,
            stage1_detail=provided
            | {"__evidence__": f"no frozen stage-2 gold for population {pop!r}"},
            mart_scores={m.name: False for m in task.marts},
            reward=0.0,
        )
    scores, reward = _mart_fraction(task, dict(stage2_gold), dict(actual_marts or {}))
    return RewardResult(
        stage1_pass=True, stage1_detail=provided, mart_scores=scores, reward=reward
    )

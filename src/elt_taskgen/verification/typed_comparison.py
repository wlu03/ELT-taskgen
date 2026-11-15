"""Value comparison for the default reward, using declared mart types.

Keys and integers are exact. Decimal measures allow one billionth of a unit
for driver serialization; floating measures additionally allow 1e-12 relative
roundoff. Numeric parsing and ordering never round identities through float.
"""
from decimal import Decimal, InvalidOperation, localcontext

from elt_taskgen.models import ColumnType, MartSpec, Row

FLOAT_REL_TOL = Decimal('1e-12')
NUMERIC_ABS_TOL = Decimal('1e-9')
COMPARISON_VERSION = 'typed-marts-v1'


def number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or not text.isascii() or '_' in text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    # Bound arithmetic before subtraction: an adversarial exponent must be a
    # wrong answer, not an Overflow/InvalidOperation in the reward process.
    if parsed and not -324 <= parsed.adjusted() <= 308:
        return None
    return parsed


def column_contract(mart: MartSpec, name: str):
    lowered = name.casefold()
    column = next((c for c in mart.columns if c.name.casefold() == lowered), None)
    key = lowered in {c.casefold() for c in mart.key_columns}
    return (column.type if column else None), key


def vectors_match(gold, actual, column_type, *, exact=False,
                  rel_tol=FLOAT_REL_TOL, abs_tol=NUMERIC_ABS_TOL,
                  null_asymmetry=True, decimal_rel_tol=Decimal(0)):
    # Keep upstream's documented non-key text/NULL normalization. Numeric
    # identities and integer-valued outputs never use its relative tolerance.
    from elt_taskgen.verification.upstream_eval import _is_null, _vectors_match
    if len(gold) != len(actual):
        return False
    numeric = column_type in (ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.DECIMAL, ColumnType.FLOAT)
    integral = column_type in (ColumnType.INTEGER, ColumnType.BIGINT)
    if not numeric and not exact:
        if not null_asymmetry:
            pairs = [(g, a) for g, a in zip(gold, actual) if not (_is_null(g) or _is_null(a))]
            return _vectors_match([p[0] for p in pairs], [p[1] for p in pairs])
        return _vectors_match(gold, actual)
    for g, a in zip(gold, actual):
        g_null, a_null = _is_null(g), _is_null(a)
        if g_null or a_null:
            if g_null != a_null and null_asymmetry:
                return False
            continue
        if not numeric:
            if str(g) != str(a):
                return False
            continue
        x, y = number(g), number(a)
        if x is None or y is None:
            return False
        if integral and (x != x.to_integral_value() or y != y.to_integral_value()):
            return False
        if exact or integral:
            if x != y:
                return False
        else:
            # Enough digits for every admitted DECIMAL(38,9), independent of
            # process-global Decimal context and caller-supplied float types.
            with localcontext() as context:
                context.prec = 80
                tolerance = Decimal(str(abs_tol))
                if column_type is ColumnType.FLOAT:
                    tolerance += Decimal(str(rel_tol)) * abs(x)
                elif column_type is ColumnType.DECIMAL:
                    tolerance += Decimal(str(decimal_rel_tol)) * abs(x)
                if abs(x-y) > tolerance:
                    return False
    return True


def sort_rows(rows: list[Row], mart: MartSpec, columns: tuple[str, ...]) -> list[Row]:
    """Native identity ordering, with all output columns as stable tie-breakers."""
    from elt_taskgen.verification.upstream_eval import _is_null
    lower = {c.casefold(): c for c in columns}
    keys = [lower[k.casefold()] for k in mart.key_columns if k.casefold() in lower]
    ordered = keys + [c for c in columns if c not in keys]
    contracts = {c: column_contract(mart, c) for c in ordered}
    def row_key(row):
        result = []
        for name in ordered:
            value = row.get(name)
            kind, exact = contracts[name]
            if _is_null(value):
                result.append((1, 0, Decimal(0), ''))
            elif kind in (ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.DECIMAL, ColumnType.FLOAT) and (parsed := number(value)) is not None:
                result.append((0, 0, parsed, ''))
            else:
                text = str(value)
                result.append((0, 1, Decimal(0), text if exact else text.strip().lower()))
        return tuple(result)
    return sorted(rows, key=row_key)

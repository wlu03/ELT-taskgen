"""Render external identifiers safely for supported SQL dialects."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

from sqlglot import Dialect, exp


class SqlIdentifierError(ValueError):
    """A value cannot be represented as one SQL identifier."""


_PLAIN_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_KEYWORD_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dialect_name(dialect: str) -> str:
    if not isinstance(dialect, str) or not dialect:
        raise SqlIdentifierError(f"invalid SQL dialect {dialect!r}")
    try:
        return type(Dialect.get_or_raise(dialect)).__name__.lower()
    except ValueError as exc:
        raise SqlIdentifierError(f"unsupported SQL dialect {dialect!r}") from exc


@lru_cache(maxsize=None)
def _keyword_parts(dialect: str) -> frozenset[str]:
    """Return words that require quoting in the selected dialect."""

    if dialect == "duckdb":
        # Probe the pinned parser because DuckDB permits some grammar keywords
        # in qualifier and alias positions.
        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            rows = connection.execute(
                "SELECT keyword_name FROM duckdb_keywords() "
                "WHERE keyword_category IN ('reserved', 'type_function')"
            ).fetchall()
        finally:
            connection.close()
        return frozenset(str(row[0]).casefold() for row in rows)

    dialect_class = Dialect.get_or_raise(dialect)
    words = {
        match.group(0).casefold()
        for keyword in dialect_class.tokenizer_class.KEYWORDS
        for match in _KEYWORD_PART.finditer(keyword)
    }
    words.update(
        str(keyword).casefold()
        for keyword in dialect_class.generator_class.RESERVED_KEYWORDS
    )
    return frozenset(words)


def identifier_needs_quoting(value: str, *, dialect: str = "duckdb") -> bool:
    """Return whether ``value`` is unsafe to emit as a bare identifier."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise SqlIdentifierError(f"invalid SQL identifier {value!r}")
    normalized_dialect = _dialect_name(dialect)
    return (
        _PLAIN_IDENTIFIER.fullmatch(value) is None
        or value.casefold() in _keyword_parts(normalized_dialect)
    )


def quote_sql_identifier(
    value: str,
    *,
    dialect: str = "duckdb",
    force: bool = False,
) -> str:
    """Render an identifier, quoting unsafe names or all names when forced."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise SqlIdentifierError(f"invalid SQL identifier {value!r}")
    normalized_dialect = _dialect_name(dialect)
    if not force:
        needs_quotes = (
            _PLAIN_IDENTIFIER.fullmatch(value) is None
            or value.casefold() in _keyword_parts(normalized_dialect)
        )
        if not needs_quotes:
            return value
    try:
        return exp.Identifier(this=value, quoted=True).sql(
            dialect=normalized_dialect
        )
    except ValueError as exc:  # pragma: no cover - normalized above
        raise SqlIdentifierError(f"unsupported SQL dialect {dialect!r}") from exc


def quote_sql_path(
    parts: Iterable[str],
    *,
    dialect: str = "duckdb",
    force: bool = False,
) -> str:
    """Render a dot-qualified identifier path one component at a time."""

    values = tuple(parts)
    if not values:
        raise SqlIdentifierError("SQL identifier path is empty")
    return ".".join(
        quote_sql_identifier(part, dialect=dialect, force=force)
        for part in values
    )


__all__ = [
    "SqlIdentifierError",
    "identifier_needs_quoting",
    "quote_sql_identifier",
    "quote_sql_path",
]

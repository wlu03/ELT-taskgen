"""Normalize candidate sources into complete, connected ``TaskIR`` projects."""


def file_backend_is_safe(columns) -> bool:
    """Can a FILES-backed table round-trip every row?

    The Airbyte file source reads CSV with pandas, which turns an empty cell
    into NaN. A row whose every column is NULL therefore becomes an all-null
    record and is dropped before the warehouse, so a table with no NOT NULL
    column silently loses rows.
    """
    return any(not column.nullable for column in columns)


def reassign_unsafe_file_tables(backends, tables):
    """Move tables that cannot round-trip off the FILES backend.

    ``backends`` and ``tables`` are the adapter's assignments and table specs.
    Returns assignments with each unsafe FILES table moved to POSTGRES, which
    carries NULLs natively.
    """
    from elt_taskgen.models import Backend

    by_name = {table.name: table for table in tables}
    moved = []
    for assignment in backends:
        table = by_name.get(assignment.table)
        if (
            assignment.backend is Backend.FILES
            and table is not None
            and not file_backend_is_safe(table.columns)
        ):
            moved.append(assignment.model_copy(update={"backend": Backend.POSTGRES}))
        else:
            moved.append(assignment)
    return tuple(moved)

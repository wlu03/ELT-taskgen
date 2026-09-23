"""Normalize candidate sources into complete, connected ``TaskIR`` projects."""


def file_backend_is_safe(columns) -> bool:
    """Can a FILES- or REST-backed table round-trip every row?

    A row whose every column is NULL reaches the warehouse as an all-null
    record and is dropped on the way: the Airbyte file source reads CSV with
    pandas, which turns an empty cell into NaN, and the declarative REST
    source delivers ``{"payload": null}`` as a record with no value. A table
    with no NOT NULL column therefore silently loses rows on either backend
    (dlt__workable ``members`` lost 28 of 266 per sync).
    """
    return any(not column.nullable for column in columns)


def reassign_unsafe_file_tables(backends, tables):
    """Move tables that cannot round-trip off the FILES and REST backends.

    ``backends`` and ``tables`` are the adapter's assignments and table specs.
    Returns assignments with each unsafe FILES or REST table moved to
    POSTGRES, which carries NULLs natively.
    """
    from elt_taskgen.models import Backend

    by_name = {table.name: table for table in tables}
    moved = []
    for assignment in backends:
        table = by_name.get(assignment.table)
        if (
            assignment.backend in (Backend.FILES, Backend.REST)
            and table is not None
            and not file_backend_is_safe(table.columns)
        ):
            moved.append(assignment.model_copy(update={"backend": Backend.POSTGRES}))
        else:
            moved.append(assignment)
    return tuple(moved)

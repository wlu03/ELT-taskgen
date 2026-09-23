"""A table with no NOT NULL column cannot ride FILES or REST: an all-null
record is dropped before the warehouse, so such tables move to POSTGRES."""

import unittest

from elt_taskgen.adapters import file_backend_is_safe, reassign_unsafe_file_tables
from elt_taskgen.models import Backend, BackendAssignment, ColumnSpec, ColumnType, TableSpec


def _table(name: str, nullable: bool) -> TableSpec:
    return TableSpec(
        name=name,
        columns=(ColumnSpec(name="payload", type=ColumnType.JSON, nullable=nullable),),
    )


class BackendSafetyTests(unittest.TestCase):
    def test_all_nullable_table_is_unsafe(self) -> None:
        self.assertFalse(file_backend_is_safe(_table("t", True).columns))
        self.assertTrue(file_backend_is_safe(_table("t", False).columns))

    def test_unsafe_files_and_rest_tables_move_to_postgres(self) -> None:
        tables = (_table("files_t", True), _table("rest_t", True), _table("keyed", False))
        backends = (
            BackendAssignment(table="files_t", backend=Backend.FILES),
            BackendAssignment(table="rest_t", backend=Backend.REST),
            BackendAssignment(table="keyed", backend=Backend.REST),
        )
        moved = {a.table: a.backend for a in reassign_unsafe_file_tables(backends, tables)}
        self.assertEqual(
            moved,
            {"files_t": Backend.POSTGRES, "rest_t": Backend.POSTGRES, "keyed": Backend.REST},
        )


if __name__ == "__main__":
    unittest.main()

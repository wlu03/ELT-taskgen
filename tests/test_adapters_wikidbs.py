"""Tests for the WikiDBs pool: the family map instrument and the adapter.

Four things are load-bearing and all four are tested here:

  1. union-find over a fixture edge list produces the components the fixture
     report states, and the persisted map is byte-identical across runs;
  2. a report that disagrees with the graph (or a community that straddles two
     components) raises CrossCheckError and produces NO map — the report is
     the oracle;
  3. Wikidata manufacturing provenance (property/topic ids and labels,
     alternative_* name lists, Q-id-valued columns) never reaches the emitted
     TaskIR, and the guard fires when it is planted;
  4. the population policy is the REAL-DATA one: every population's rows come
     from the vendored CSVs via literal_rows, so nothing is synthesized.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen.adapters import wikidbs
from elt_taskgen.generation import mart_plan as mart_plan_mod
from elt_taskgen.generation import populations as populations_mod
from elt_taskgen.models import (
    AttackKind,
    ColumnType,
    MartSpec,
    Origin,
    PopulationName,
)
from elt_taskgen.reference import solution as reference_solution
from elt_taskgen.verification import attacks

_TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load_tool():
    name = "wikidbs_family_map"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fam = _load_tool()


# ---------------------------------------------------------------------------
# Graph fixture: 8 nodes, components {0,1,2} and {4,5}, singletons 3/6/7
# ---------------------------------------------------------------------------

_EDGES = ((0, 1), (1, 2), (4, 5))
_COMMUNITIES = {0: 1, 1: 1, 2: 2, 4: 3, 5: 3}

_REPORT = """Graph Path: data/graph/fixture.dgl
Total Nodes: 8
Total Edges: 6

=== Connected Components Analysis ===
Number of Connected Components: 2

Top 10 Component Sizes:
  Component 1: 3 nodes
  Component 2: 2 nodes
  ...

Largest Connected Component: 3 nodes

=== Community Detection Analysis ===
Number of Communities: 3
Modularity Score: 0.5000

Top 10 Community Sizes:
  Community 1: 2 nodes
  Community 2: 2 nodes
  Community 3: 1 nodes
  ...
"""


def _write_graph(root: Path, *, report: str = _REPORT, communities=None) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    edges = root / "edges.csv"
    with edges.open("w", encoding="utf-8") as fh:
        fh.write(fam.EDGES_HEADER + "\n")
        for src, tgt in _EDGES:
            fh.write(f"{src}.0,{tgt}.0,0.99,0.0,1\n")
    comm = root / "communities.csv"
    with comm.open("w", encoding="utf-8") as fh:
        fh.write(fam.COMMUNITIES_HEADER + "\n")
        for node, part in sorted((communities or _COMMUNITIES).items()):
            fh.write(f"{node},{part}\n")
    rep = root / "report.txt"
    rep.write_text(report, encoding="utf-8")
    return edges, comm, rep


class TestFamilyMapUnionFind(unittest.TestCase):
    def test_components_match_the_fixture_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges, comm, rep = _write_graph(Path(tmp))
            fmap = fam.build_family_map(edges, comm, rep)
            self.assertEqual(fmap.total_nodes, 8)
            self.assertEqual(fmap.connected_nodes, 5)
            self.assertEqual(fmap.singleton_nodes, 3)
            # Component id is the MINIMUM member, not discovery order.
            self.assertEqual(
                [fmap.component_of(n) for n in range(8)],
                [
                    "c00000", "c00000", "c00000", "c00003",
                    "c00004", "c00004", "c00006", "c00007",
                ],
            )
            self.assertEqual(fmap.sizes["c00000"], 3)
            self.assertTrue(fmap.is_singleton(3))
            self.assertFalse(fmap.is_singleton(4))
            self.assertEqual(fmap.members("c00004"), (4, 5))
            self.assertEqual(fmap.evidence["report_components"], "2")

    def test_persisted_map_is_byte_identical_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edges, comm, rep = _write_graph(root)
            fmap = fam.build_family_map(edges, comm, rep)
            a = fam.write_map(fmap, root / "a.csv.gz")
            b = fam.write_map(fam.build_family_map(edges, comm, rep), root / "b.csv.gz")
            self.assertEqual(a, b)
            self.assertEqual(
                (root / "a.csv.gz").read_bytes(), (root / "b.csv.gz").read_bytes()
            )
            text = gzip.open(root / "a.csv.gz", "rt", encoding="utf-8").read()
            self.assertIn("node_id,component_id,component_size", text)
            self.assertIn("00003,c00003,1", text)
            reloaded = fam.load_map(root / "a.csv.gz")
            self.assertEqual(reloaded.assignments, fmap.assignments)
            self.assertEqual(reloaded.sizes, fmap.sizes)
            self.assertEqual(reloaded.connected_nodes, fmap.connected_nodes)

    def test_node_outside_the_universe_is_a_key_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges, comm, rep = _write_graph(Path(tmp))
            fmap = fam.build_family_map(edges, comm, rep)
            with self.assertRaises(KeyError):
                fmap.component_of(8)


class TestFamilyMapCrossCheck(unittest.TestCase):
    """The report is the oracle: any disagreement fails loudly."""

    def _build(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            edges, comm, rep = _write_graph(Path(tmp), **kwargs)
            return fam.build_family_map(edges, comm, rep)

    def test_wrong_component_count_raises(self):
        report = _REPORT.replace("Number of Connected Components: 2", "Number of Connected Components: 5")
        with self.assertRaises(fam.CrossCheckError) as ctx:
            self._build(report=report)
        self.assertIn("connected components", str(ctx.exception))

    def test_wrong_largest_component_raises(self):
        report = _REPORT.replace("Largest Connected Component: 3", "Largest Connected Component: 4")
        with self.assertRaises(fam.CrossCheckError):
            self._build(report=report)

    def test_wrong_edge_count_raises(self):
        report = _REPORT.replace("Total Edges: 6", "Total Edges: 8")
        with self.assertRaises(fam.CrossCheckError) as ctx:
            self._build(report=report)
        self.assertIn("Total Edges", str(ctx.exception))

    def test_wrong_top_sizes_raise(self):
        report = _REPORT.replace("Component 2: 2 nodes", "Component 2: 1 nodes")
        with self.assertRaises(fam.CrossCheckError):
            self._build(report=report)

    def test_community_straddling_two_components_raises(self):
        # node 4 lives in component c00004, node 0 in c00000: one partition
        # covering both is a refinement violation.
        bad = dict(_COMMUNITIES)
        bad[4] = 1
        bad[5] = 1
        with self.assertRaises(fam.CrossCheckError) as ctx:
            self._build(communities=bad)
        self.assertIn("straddle", str(ctx.exception))

    def test_community_node_set_mismatch_raises(self):
        bad = dict(_COMMUNITIES)
        bad[6] = 4  # node 6 has no edge, so it cannot be in a community
        with self.assertRaises(fam.CrossCheckError):
            self._build(communities=bad)

    def test_report_without_the_oracle_fields_raises(self):
        with self.assertRaises(fam.CrossCheckError):
            self._build(report="Total Nodes: 8\n")

    def test_edge_id_outside_the_universe_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edges, comm, rep = _write_graph(root)
            with edges.open("a", encoding="utf-8") as fh:
                fh.write("99.0,1.0,0.99,0.0,1\n")
            with self.assertRaises(fam.CrossCheckError):
                fam.build_family_map(edges, comm, rep)

    def test_unexpected_edge_header_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edges, comm, rep = _write_graph(root)
            edges.write_text("a,b\n0.0,1.0\n", encoding="utf-8")
            with self.assertRaises(fam.CrossCheckError):
                fam.build_family_map(edges, comm, rep)


# ---------------------------------------------------------------------------
# WikiDBs database fixture (the real schema.json / tables layout, in miniature)
# ---------------------------------------------------------------------------

DB_DIRNAME = "00004 fixture_orders_db"

_SCHEMA = {
    "database_name": "fixture_orders_db",
    "alternative_database_names": ["Fixture_Orders_DB", "author-ada-lovelace"],
    "wikidata_property_id": "P50",
    "wikidata_property_label": "author",
    "wikidata_topic_item_id": "Q7259",
    "wikidata_topic_item_label": "Ada Lovelace",
    "tables": [
        {
            "table_name": "publications",
            "alternative_table_names": ["Publications", "author Ada Lovelace"],
            "file_name": "publications.csv",
            "columns": [
                {
                    "column_name": "publication_title",
                    "alternative_column_names": ["publication_title", "label"],
                    "data_type": "string",
                    "wikidata_property_id": None,
                },
                {
                    "column_name": "page_count",
                    "alternative_column_names": ["page_count", "number of pages"],
                    "data_type": "quantity",
                    "wikidata_property_id": "P1104",
                },
                {
                    "column_name": "publisher_name",
                    "alternative_column_names": ["publisher_name", "publisher"],
                    "data_type": "wikibase-entityid",
                    "wikidata_property_id": "P123",
                },
                {
                    "column_name": "freebase_id",
                    "alternative_column_names": ["Freebase ID", "Freebase ID"],
                    "data_type": "external-id",
                    "wikidata_property_id": "P646",
                },
            ],
            "foreign_keys": [
                {
                    "column_name": "publisher_name",
                    "reference_column_name": "publisher_label",
                    "reference_table_name": "publishers",
                }
            ],
        },
        {
            "table_name": "publishers",
            "alternative_table_names": ["Publishers", "publisher"],
            "file_name": "publishers.csv",
            "columns": [
                {
                    "column_name": "publisher_label",
                    "alternative_column_names": ["publisher_label", "label"],
                    "data_type": "string",
                    "wikidata_property_id": None,
                },
                {
                    "column_name": "country_name",
                    "alternative_column_names": ["country_name", "country"],
                    "data_type": "wikibase-entityid",
                    "wikidata_property_id": "P17",
                },
            ],
            "foreign_keys": [],
        },
    ],
}

_PUBLICATIONS = [
    ["publication_title", "page_count", "publisher_name", "freebase_id"],
    ["Notes on the Analytical Engine", "42", "Taylor and Francis", "Q111"],
    ["Sketch of the Analytical Engine", "17", "Taylor and Francis", "Q222"],
    ["On Bernoulli numbers", "9", "Scientific Memoirs", "Q333"],
    ["An unattributed note", "", "", "Q444"],
]

_PUBLISHERS = [
    ["publisher_label", "country_name"],
    ["Taylor and Francis", "United Kingdom"],
    ["Scientific Memoirs", "United Kingdom"],
    ["Unreferenced Press", ""],
]


def _write_db(root: Path, *, dirname: str = DB_DIRNAME, schema=None) -> Path:
    db = root / dirname
    (db / "tables").mkdir(parents=True, exist_ok=True)
    (db / "tables_with_item_ids").mkdir(parents=True, exist_ok=True)
    (db / "schema.json").write_text(
        json.dumps(schema or _SCHEMA, indent=1), encoding="utf-8"
    )
    for name, rows in (("publications.csv", _PUBLICATIONS), ("publishers.csv", _PUBLISHERS)):
        with (db / "tables" / name).open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
        # The item-id variant has the SAME header and Q-id values — the shape
        # the adapter must never read.
        with (db / "tables_with_item_ids" / name).open(
            "w", encoding="utf-8", newline=""
        ) as fh:
            writer = csv.writer(fh)
            writer.writerow(rows[0])
            for i, _row in enumerate(rows[1:], start=1):
                writer.writerow([f"Q{i}{j}" for j in range(len(rows[0]))])
    return db


def _fixture_map(root: Path) -> Path:
    edges, comm, rep = _write_graph(root / "graph")
    fmap = fam.build_family_map(edges, comm, rep)
    path = root / "map.csv.gz"
    fam.write_map(fmap, path)
    return path


class TestWikiDbsAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = _write_db(self.root)
        self.map_path = _fixture_map(self.root)
        self.task = wikidbs.to_task_ir(self.db, family_map_path=self.map_path)

    def tearDown(self):
        self._tmp.cleanup()

    # -- identity ----------------------------------------------------------

    def test_identity_comes_from_the_component_and_the_catalog(self):
        # node 4 is in component c00004 of the fixture graph.
        self.assertEqual(self.task.family_id, "wikidbs__c00004")
        self.assertIs(self.task.origin, Origin.WIKIDBS)
        self.assertEqual(self.task.license, "CC-BY-4.0")
        self.assertIn(DB_DIRNAME, self.task.attribution)
        self.assertTrue(self.task.task_id.startswith("wikidbs__c00004__"))

    def test_ingest_is_deterministic(self):
        again = wikidbs.to_task_ir(self.db, family_map_path=self.map_path)
        self.assertEqual(again.content_hash(), self.task.content_hash())

    def test_stress_duplicate_rule_matches_the_mart_plan(self):
        stress = next(
            population
            for population in self.task.populations
            if population.name is PopulationName.STRESS
        )
        text = " ".join(stress.conditions)
        # This fixture's plan has an explicit publications DEDUPE operation.
        # The population may state that local fact, but must never impose a
        # blanket dedupe policy on every duplicated table.
        self.assertIn("marts deduplicate publications", text)
        self.assertIn("counts ONCE", text)
        self.assertNotIn("correct logic must dedupe", text)

    def test_schema_and_link_come_from_the_data(self):
        names = sorted(t.name for t in self.task.tables)
        self.assertEqual(names, ["publications", "publishers"])
        rel = self.task.relationships[0]
        self.assertEqual(rel.child_table, "publications")
        self.assertEqual(rel.parent_table, "publishers")
        # One publication has an empty publisher_name, so the link is OPTIONAL
        # — computed from the real rows, not assumed.
        self.assertFalse(rel.required)
        # page_count is all-numeric in the real data.
        self.assertEqual(
            self.task.table("publications").column("page_count").type.value, "integer"
        )

    def test_legacy_profile_prefers_a_mixed_null_real_column(self):
        """The completeness metric must not duplicate the linked-row count.

        ``publication_title`` is first and always present.  ``subtitle`` comes
        later but has both a real value and a missing value, so it is the only
        profile whose COUNT can differ from COUNT(link) on legal WikiDBs rows.
        """
        schema = json.loads(json.dumps(_SCHEMA))
        schema["database_name"] = "nullable_profile_db"
        schema["tables"][0]["columns"].insert(
            1,
            {"column_name": "subtitle", "data_type": "string"},
        )
        db = _write_db(
            self.root,
            dirname="00005 nullable_profile_db",
            schema=schema,
        )
        rows = [
            ["publication_title", "subtitle", "page_count", "publisher_name", "freebase_id"],
            ["Notes on the Analytical Engine", "Commentary", "42", "Taylor and Francis", "Q111"],
            ["Sketch of the Analytical Engine", "", "17", "Taylor and Francis", "Q222"],
            ["On Bernoulli numbers", "Appendix", "9", "Scientific Memoirs", "Q333"],
            ["An unattributed note", "", "", "", "Q444"],
        ]
        with (db / "tables" / "publications.csv").open(
            "w", encoding="utf-8", newline=""
        ) as fh:
            csv.writer(fh).writerows(rows)

        converted = wikidbs._convert(db, wikidbs.load_schema(db))
        mart, _shape = wikidbs._build_legacy_star(
            converted.tables,
            converted.relationships,
            "nullable_profile_db",
            converted.identifier_columns,
            rows=converted.rows,
            minimum_columns=6,
        )
        names = {column.name for column in mart.columns}
        self.assertIn("non_null_subtitle_count", names)
        self.assertNotIn("non_null_publication_title_count", names)
        aggregate = next(
            op for op in mart.plan.ops if op.kind.value == "aggregate"
        )
        self.assertIn('COUNT("publications__subtitle")', aggregate.details.values())
        conditions = wikidbs._profile_witness_conditions(
            (mart,), converted.relationships, converted.rows
        )
        self.assertEqual(len(conditions), 1)
        self.assertTrue(conditions[0].startswith("MISSING-VALUE WITNESS:"))
        self.assertIn("publications", conditions[0])
        self.assertIn("subtitle", conditions[0])
        self.assertNotIn("publication_title", conditions[0])

    def test_legacy_profile_prefers_mixed_null_and_same_parent_repeat(self):
        """Both profile counts must have real, linked discrimination witnesses."""
        schema = json.loads(json.dumps(_SCHEMA))
        schema["database_name"] = "profile_witness_db"
        schema["tables"][0]["columns"][1:1] = [
            {"column_name": "subtitle", "data_type": "string"},
            {"column_name": "series_name", "data_type": "string"},
        ]
        db = _write_db(
            self.root,
            dirname="00005 profile_witness_db",
            schema=schema,
        )
        rows = [
            [
                "publication_title",
                "subtitle",
                "series_name",
                "page_count",
                "publisher_name",
                "freebase_id",
            ],
            [
                "Notes on the Analytical Engine",
                "Commentary",
                "Shared series",
                "42",
                "Taylor and Francis",
                "Q111",
            ],
            [
                "Sketch of the Analytical Engine",
                "",
                "Shared series",
                "17",
                "Taylor and Francis",
                "Q222",
            ],
            [
                "On Bernoulli numbers",
                "Appendix",
                "",
                "9",
                "Scientific Memoirs",
                "Q333",
            ],
            ["An unattributed note", "", "", "", "", "Q444"],
        ]
        with (db / "tables" / "publications.csv").open(
            "w", encoding="utf-8", newline=""
        ) as fh:
            csv.writer(fh).writerows(rows)

        converted = wikidbs._convert(db, wikidbs.load_schema(db))
        mart, _shape = wikidbs._build_legacy_star(
            converted.tables,
            converted.relationships,
            "profile_witness_db",
            converted.identifier_columns,
            rows=converted.rows,
            minimum_columns=6,
        )
        names = {column.name for column in mart.columns}
        self.assertIn("distinct_series_name_count", names)
        self.assertIn("non_null_series_name_count", names)
        self.assertNotIn("non_null_subtitle_count", names)

        conditions = wikidbs._profile_witness_conditions(
            (mart,), converted.relationships, converted.rows
        )
        self.assertEqual(len(conditions), 2)
        self.assertTrue(conditions[0].startswith("MISSING-VALUE WITNESS:"))
        self.assertTrue(conditions[1].startswith("REPEATED-VALUE WITNESS:"))
        self.assertTrue(all("series_name" in condition for condition in conditions))
        self.assertTrue(all("subtitle" not in condition for condition in conditions))
        self.assertIn(
            "omitting DISTINCT from distinct_series_name_count changes at least "
            "one mart output",
            conditions[1],
        )

    # -- provenance stripping ---------------------------------------------

    def test_no_wikidata_provenance_in_the_emitted_task(self):
        dump = json.dumps(self.task.model_dump(mode="json"), ensure_ascii=False)
        for planted in (
            "P50", "Q7259", "Ada Lovelace", "author-ada-lovelace",
            "Fixture_Orders_DB", "wikidata_property_id", "alternative_table_names",
        ):
            self.assertNotIn(planted, dump, f"{planted!r} leaked into the task")

    def test_quarantine_accessor_sees_what_the_adapter_does_not(self):
        quarantined = wikidbs.provenance_strings(self.db)
        self.assertIn("Ada Lovelace", quarantined)
        self.assertIn("P50", quarantined)
        # ...and the structural view simply does not have those keys.
        schema = wikidbs.load_schema(self.db)
        self.assertEqual(
            set(schema.model_dump().keys()), {"database_name", "tables"}
        )

    def test_qid_valued_column_is_stripped_from_the_schema(self):
        # freebase_id holds Q-ids in the fixture: Wikidata provenance in data
        # clothing, dropped rather than shipped.
        columns = {c.name for c in self.task.table("publications").columns}
        self.assertNotIn("freebase_id", columns)
        self.assertIn("publication_title", columns)

    def test_guard_fires_on_a_planted_provenance_label(self):
        table = self.task.tables[0]
        poisoned = self.task.model_copy(
            update={
                "tables": (
                    table.model_copy(update={"description": "author"}),
                )
                + self.task.tables[1:]
            }
        )
        with self.assertRaises(wikidbs.ProvenanceLeakError):
            wikidbs.assert_no_provenance_leak(poisoned, self.db)

    def test_guard_fires_on_a_planted_wikidata_id(self):
        poisoned = self.task.model_copy(update={"title": "Ada Lovelace Q7259 corpus"})
        with self.assertRaises(wikidbs.ProvenanceLeakError):
            wikidbs.assert_no_provenance_leak(poisoned, self.db)

    def test_reading_tables_with_item_ids_is_refused(self):
        # Same header, Q-id values: the only thing that catches it is the
        # column-level identifier rule.
        db2 = self.root / "swapped"
        db2.mkdir()
        target = db2 / DB_DIRNAME
        target.mkdir()
        (target / "schema.json").write_bytes((self.db / "schema.json").read_bytes())
        (target / "tables").mkdir()
        for name in ("publications.csv", "publishers.csv"):
            (target / "tables" / name).write_bytes(
                (self.db / "tables_with_item_ids" / name).read_bytes()
            )
        with self.assertRaises(wikidbs.WikiDbsIngestError) as ctx:
            wikidbs.to_task_ir(target, family_map_path=self.map_path)
        self.assertIn("tables_with_item_ids", str(ctx.exception))

    def test_is_wikidata_id_column(self):
        self.assertTrue(wikidbs.is_wikidata_id_column(["Q1", "Q2", "P50"]))
        self.assertFalse(wikidbs.is_wikidata_id_column(["Q1", "London", "Paris"]))
        self.assertFalse(wikidbs.is_wikidata_id_column([]))

    # -- the real-data population policy ----------------------------------

    def test_every_population_carries_the_real_rows(self):
        self.assertTrue(self.task.has_all_populations())
        real_titles = {row[0] for row in _PUBLICATIONS[1:]}
        for pop in self.task.populations:
            self.assertEqual(pop.scale, {}, f"{pop.name.value} declares a synthetic scale")
            self.assertTrue(pop.literal_rows, f"{pop.name.value} has no provided rows")
            self.assertEqual(
                sorted(pop.literal_rows), ["publications", "publishers"]
            )
            for row in pop.literal_rows["publications"]:
                self.assertIn(
                    row["publication_title"],
                    real_titles,
                    "a fabricated row appeared in a provided-rows population",
                )
            self.assertIn(wikidbs.REAL_DATA_POLICY, " ".join(pop.conditions))

    def test_primary_is_every_real_row_and_populations_validate(self):
        primary = self.task.population(PopulationName.PRIMARY)
        self.assertEqual(len(primary.literal_rows["publications"]), len(_PUBLICATIONS) - 1)
        self.assertEqual(len(primary.literal_rows["publishers"]), len(_PUBLISHERS) - 1)
        self.assertEqual(populations_mod.validate_population_coverage(self.task), [])

    def test_resampled_reorders_the_same_real_rows(self):
        primary = self.task.population(PopulationName.PRIMARY)
        resampled = self.task.population(PopulationName.RESAMPLED)
        for table in ("publications", "publishers"):
            self.assertEqual(
                sorted(map(json.dumps, primary.literal_rows[table])),
                sorted(map(json.dumps, resampled.literal_rows[table])),
            )

    def test_counterfactual_leaves_dimension_rows_unmatched(self):
        counter = self.task.population(PopulationName.COUNTERFACTUAL)
        rel = self.task.relationships[0]
        parents = {r[rel.parent_columns[0]] for r in counter.literal_rows[rel.parent_table]}
        children = {r[rel.child_columns[0]] for r in counter.literal_rows[rel.child_table]}
        self.assertTrue(parents - children, "no unmatched dimension row was constructed")

    def test_counterfactual_does_not_add_mart_invisible_dangling_link(self):
        """A P <- B roll-up cannot see a retained B after P is removed."""
        rel = self.task.relationships[0]
        self.assertFalse(rel.required)
        parent_column = rel.parent_columns[0]
        child_column = rel.child_columns[0]
        primary = self.task.population(PopulationName.PRIMARY)
        counter = self.task.population(PopulationName.COUNTERFACTUAL)

        primary_parents = {
            row[parent_column]
            for row in primary.literal_rows[rel.parent_table]
        }
        self.assertFalse(
            any(
                row[child_column] is not None
                and row[child_column] not in primary_parents
                for row in primary.literal_rows[rel.child_table]
            ),
            "the fixture should exercise optionality caused only by NULL",
        )

        counter_parents = {
            row[parent_column]
            for row in counter.literal_rows[rel.parent_table]
        }
        self.assertFalse(
            any(
                row[child_column] is not None
                and row[child_column] not in counter_parents
                for row in counter.literal_rows[rel.child_table]
            ),
            "one-hop parent removal would be invisible to the emitted mart",
        )
        self.assertFalse(
            any("DANGLING LINK" in condition for condition in counter.conditions),
            counter.conditions,
        )

        # The witness is a carve-out, never a fabricated row.
        for table in (rel.parent_table, rel.child_table):
            real = {
                json.dumps(row, sort_keys=True)
                for row in primary.literal_rows[table]
            }
            self.assertTrue(
                all(
                    json.dumps(row, sort_keys=True) in real
                    for row in counter.literal_rows[table]
                )
            )

    def test_stress_duplicates_real_rows_only(self):
        primary = self.task.population(PopulationName.PRIMARY)
        stress = self.task.population(PopulationName.STRESS)
        for table in ("publications", "publishers"):
            base = {json.dumps(r, sort_keys=True) for r in primary.literal_rows[table]}
            for row in stress.literal_rows[table]:
                self.assertIn(json.dumps(row, sort_keys=True), base)
        self.assertGreaterEqual(
            len(stress.literal_rows["publications"]),
            len(primary.literal_rows["publications"]),
        )

    def test_development_is_smaller_and_link_closed(self):
        dev = self.task.population(PopulationName.DEVELOPMENT)
        primary = self.task.population(PopulationName.PRIMARY)
        for table in ("publications", "publishers"):
            self.assertLessEqual(
                len(dev.literal_rows[table]), len(primary.literal_rows[table])
            )
        for rel in self.task.relationships:
            if not rel.required:
                continue
            keys = {r[rel.parent_columns[0]] for r in dev.literal_rows[rel.parent_table]}
            for row in dev.literal_rows[rel.child_table]:
                self.assertIn(row[rel.child_columns[0]], keys)


class TestNodeCorrespondence(unittest.TestCase):
    def test_directory_prefix_is_the_node_id(self):
        self.assertEqual(wikidbs.node_id_for_dir("00042 Some Db"), 42)
        self.assertEqual(wikidbs.part_for_node(42), "part-0")
        self.assertEqual(wikidbs.part_for_node(88880), "part-4")
        with self.assertRaises(wikidbs.WikiDbsIngestError):
            wikidbs.node_id_for_dir("not-a-wikidbs-dir")

    def test_unverifiable_layout_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "part-0").mkdir()
            with self.assertRaises(wikidbs.WikiDbsIngestError):
                wikidbs.verify_node_correspondence(Path(tmp))

    def test_unverifiable_layout_falls_back_to_one_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment, evidence = wikidbs.family_segment_for(4, root=Path(tmp))
            self.assertEqual(segment, wikidbs.FALLBACK_FAMILY_SEGMENT)
            self.assertIn("fallback_reason", evidence)

    def test_missing_family_map_falls_back_to_one_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment, evidence = wikidbs.family_segment_for(
                4, map_path=Path(tmp) / "absent.csv.gz"
            )
            self.assertEqual(segment, wikidbs.FALLBACK_FAMILY_SEGMENT)
            self.assertIn("family map", evidence["fallback_reason"])

    def test_fallback_segment_is_a_legal_family_segment(self):
        # It has to survive slugify_family unchanged, or the family namespace
        # silently changes on the conservative path.
        from elt_taskgen.models import slugify_family

        self.assertEqual(
            slugify_family(wikidbs.FALLBACK_FAMILY_SEGMENT),
            wikidbs.FALLBACK_FAMILY_SEGMENT,
        )
        self.assertEqual(slugify_family("c00042"), "c00042")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestValueDrivenTyping(unittest.TestCase):
    """Column types come from the REAL values, and must be types DuckDB can
    hold — the trusted loader (reference/solution.py) CREATEs the column from
    this decision and fails the whole load if a value does not fit.

    Measured on `00050 UNIVERSITY_HUMANITIES_ECONOMICS_LODZ_STAFF`: a Wikidata
    authority id of 167144782990278895584 was typed BIGINT and killed stage 4
    with `Conversion Error: Type INT128 ... out of range for the destination
    type INT64`.
    """

    def test_small_integers_are_integer(self) -> None:
        self.assertIs(
            wikidbs.infer_column_type(["1", "2", "3"], "quantity"),
            ColumnType.INTEGER,
        )

    def test_wide_integers_are_bigint(self) -> None:
        self.assertIs(
            wikidbs.infer_column_type(["9999999999", "1"], "quantity"),
            ColumnType.BIGINT,
        )

    def test_integers_wider_than_bigint_are_text(self) -> None:
        # The overflow rule itself: declared "quantity" skips the identifier
        # short-circuit, so the measured 00050 regression value actually
        # drives the wider-than-BIGINT branch.
        self.assertIs(
            wikidbs.infer_column_type(["167144782990278895584"], "quantity"),
            ColumnType.TEXT,
        )
        # Incident pin: the shipped 00050 column declared "external-id" —
        # TEXT via the identifier rule before any value is read.
        self.assertIs(
            wikidbs.infer_column_type(["167144782990278895584"], "external-id"),
            ColumnType.TEXT,
        )

    def test_the_overflow_boundary_is_exactly_the_bigint_range(self) -> None:
        self.assertIs(
            wikidbs.infer_column_type([str(2**63 - 1)], "quantity"),
            ColumnType.BIGINT,
        )
        self.assertIs(
            wikidbs.infer_column_type([str(2**63)], "quantity"),
            ColumnType.TEXT,
        )

    def test_leading_zero_integers_are_text(self) -> None:
        """'0732' is a Cologne phonetics code, not 732: `int()` destroys the
        formatting (measured on 00012 ...TEAM_MEMBER_GIVEN_NAMES, 24 of 59
        values; the released task shipped 732). Canonical numerals only."""
        self.assertIs(
            wikidbs.infer_column_type(["3827", "0732", "268"], "string"),
            ColumnType.TEXT,
        )
        self.assertIs(wikidbs.infer_column_type(["01", "1"], "string"), ColumnType.TEXT)
        self.assertIs(
            wikidbs.infer_column_type(["0", "1", "+5", "-3"], "quantity"),
            ColumnType.INTEGER,
        )
        self.assertIs(wikidbs.infer_column_type(["00"], "string"), ColumnType.TEXT)

    def test_leading_zero_decimals_are_text(self) -> None:
        self.assertIs(wikidbs.infer_column_type(["007.5"], "string"), ColumnType.TEXT)
        self.assertIs(
            wikidbs.infer_column_type(["0.5", ".5", "1e3", "-2.25"], "string"),
            ColumnType.DECIMAL,
        )

    def test_declared_external_id_is_text_even_when_numeric(self) -> None:
        """An identifier is never a measure; its formatting is its value."""
        self.assertIs(
            wikidbs.infer_column_type(["12", "34"], "external-id"), ColumnType.TEXT
        )
        self.assertIs(
            wikidbs.infer_column_type(["12", "34"], "string"), ColumnType.INTEGER
        )

    def test_integer_coercion_round_trips(self) -> None:
        """End to end: a code column with leading zeros is shipped VERBATIM,
        and `_coerce` fails closed if anything ever types such a value integer."""
        import copy

        schema = copy.deepcopy(_SCHEMA)
        schema["tables"][0]["columns"].append(
            {
                "column_name": "cologne_phonetics_code",
                "alternative_column_names": ["cologne_phonetics_code"],
                "data_type": "string",
                "wikidata_property_id": "P3879",
            }
        )
        codes = ["0732", "268", "01", ""]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "00004 codes_db"
            (db / "tables").mkdir(parents=True)
            (db / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            with (db / "tables" / "publications.csv").open(
                "w", encoding="utf-8", newline=""
            ) as fh:
                writer = csv.writer(fh)
                writer.writerow(_PUBLICATIONS[0] + ["cologne_phonetics_code"])
                for row, code in zip(_PUBLICATIONS[1:], codes, strict=True):
                    writer.writerow(row + [code])
            with (db / "tables" / "publishers.csv").open(
                "w", encoding="utf-8", newline=""
            ) as fh:
                csv.writer(fh).writerows(_PUBLISHERS)
            conv = wikidbs._convert(db, wikidbs.load_schema(db))
        pubs = next(t for t in conv.tables if t.name == "publications")
        self.assertIs(pubs.column("cologne_phonetics_code").type, ColumnType.TEXT)
        shipped = [r["cologne_phonetics_code"] for r in conv.rows["publications"]]
        self.assertEqual(shipped, ["0732", "268", "01", None])
        # Every integer-typed value in every table prints back as itself.
        for table in conv.tables:
            for col in table.columns:
                if col.type in (ColumnType.INTEGER, ColumnType.BIGINT):
                    for r in conv.rows[table.name]:
                        v = r[col.name]
                        self.assertTrue(v is None or isinstance(v, int), (table.name, col.name, v))
        with self.assertRaises(wikidbs.WikiDbsIngestError):
            wikidbs._coerce("0732", ColumnType.INTEGER)
        self.assertEqual(wikidbs._coerce("+5", ColumnType.INTEGER), 5)


# ---------------------------------------------------------------------------
# The keyness rule: a declared edge enters the IR only if the property the IR
# USES it for is true of the shipped rows
# ---------------------------------------------------------------------------

def _keyness_schema() -> dict:
    """orders(customer_label) -> customers(customer_label): one plain edge."""
    return {
        "database_name": "keyness_db",
        "alternative_database_names": [],
        "wikidata_property_id": None,
        "wikidata_property_label": None,
        "wikidata_topic_item_id": None,
        "wikidata_topic_item_label": None,
        "tables": [
            {
                "table_name": "orders",
                "alternative_table_names": [],
                "file_name": "orders.csv",
                "columns": [
                    {
                        "column_name": "order_label",
                        "alternative_column_names": [],
                        "data_type": "string",
                        "wikidata_property_id": None,
                    },
                    {
                        "column_name": "amount",
                        "alternative_column_names": [],
                        "data_type": "quantity",
                        "wikidata_property_id": None,
                    },
                    {
                        "column_name": "customer_label",
                        "alternative_column_names": [],
                        "data_type": "string",
                        "wikidata_property_id": None,
                    },
                ],
                "foreign_keys": [
                    {
                        "column_name": "customer_label",
                        "reference_column_name": "customer_label",
                        "reference_table_name": "customers",
                    }
                ],
            },
            {
                "table_name": "customers",
                "alternative_table_names": [],
                "file_name": "customers.csv",
                "columns": [
                    {
                        "column_name": "customer_label",
                        "alternative_column_names": [],
                        "data_type": "string",
                        "wikidata_property_id": None,
                    },
                    {
                        "column_name": "country_name",
                        "alternative_column_names": [],
                        "data_type": "string",
                        "wikidata_property_id": None,
                    },
                ],
                "foreign_keys": [],
            },
        ],
    }


def _write_keyness_db(root: Path, *, orders, customers) -> Path:
    db = root / "00004 keyness_db"
    (db / "tables").mkdir(parents=True, exist_ok=True)
    (db / "schema.json").write_text(
        json.dumps(_keyness_schema(), indent=1), encoding="utf-8"
    )
    for name, rows in (("orders.csv", orders), ("customers.csv", customers)):
        with (db / "tables" / name).open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
    return db


_ORDER_HEADER = ["order_label", "amount", "customer_label"]
_CUSTOMER_HEADER = ["customer_label", "country_name"]


class TestParentKeynessGate(unittest.TestCase):
    """WikiDBs' declared 'foreign keys' are label overlaps, not key constraints.

    The IR consumes `Relationship` as a key constraint — `build_star` projects
    the grain with NO `DISTINCT` on exactly that assumption — so an edge whose
    PARENT column repeats fans the join out, `GROUP BY` hides the duplication
    in the output, and every measure ships inflated. Measured on the ingested
    corpus before this gate: 115 of 163 emitted relationships (70.6%) named a
    non-key parent and 16 of 16 executable marts emitted a count that
    contradicted the mart's own column description.

    `required` cannot catch it: `required` is derived from the CHILD side, so
    "0 of 49 required=True edges violated" is a tautology, not a check.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _convert(self, orders, customers):
        db = _write_keyness_db(
            self.root,
            orders=[_ORDER_HEADER] + orders,
            customers=[_CUSTOMER_HEADER] + customers,
        )
        return wikidbs._convert(db, wikidbs.load_schema(db))

    # -- the predicate itself ---------------------------------------------

    def test_is_key_of_is_measured_on_the_rows_not_on_a_derived_field(self):
        rows = ({"a": "x", "b": 1}, {"a": "y", "b": 1})
        self.assertTrue(wikidbs.is_key_of(rows, "a"))
        self.assertFalse(wikidbs.is_key_of(rows, "b"))
        # A NULL is not a key value, and an empty table witnesses nothing.
        self.assertFalse(wikidbs.is_key_of(({"a": None}, {"a": "y"}), "a"))
        self.assertFalse(wikidbs.is_key_of((), "a"))

    # -- case A ------------------------------------------------------------

    def test_case_a_unique_parent_and_no_dangling_child_is_required(self):
        conv = self._convert(
            [["o1", "5", "Acme"], ["o2", "7", "Globex"]],
            [["Acme", "US"], ["Globex", "US"]],
        )
        self.assertEqual(len(conv.relationships), 1)
        self.assertTrue(conv.relationships[0].required)
        self.assertEqual(conv.value_overlaps, ())

    # -- case B ------------------------------------------------------------

    def test_case_b_dangling_child_keeps_the_edge_and_states_the_condition(self):
        conv = self._convert(
            [["o1", "5", "Acme"], ["o2", "7", "Nowhere Ltd"]],
            [["Acme", "US"], ["Globex", "US"]],
        )
        self.assertEqual(len(conv.relationships), 1)
        self.assertFalse(conv.relationships[0].required)
        pops = wikidbs.real_populations(
            "t", conv.tables, conv.relationships, conv.rows
        )
        primary = next(p for p in pops if p.name is PopulationName.PRIMARY)
        stated = [c for c in primary.conditions if "DANGLING LINK" in c]
        self.assertEqual(len(stated), 1, primary.conditions)
        self.assertIn("orders.customer_label", stated[0])
        self.assertIn("customers.customer_label", stated[0])
        self.assertIn("1 of 2", stated[0])
        # An unstated trap is an unfair key, so EVERY population that carries
        # the dangling rows says so — not just `primary`.
        for pop in pops:
            dangling = any(
                r.get("customer_label") not in {"Acme", "Globex"}
                and r.get("customer_label") is not None
                for r in pop.literal_rows["orders"]
            )
            if dangling:
                self.assertTrue(
                    any("DANGLING LINK" in c for c in pop.conditions),
                    f"{pop.name} carries a dangling row and does not say so",
                )

    def test_optional_missing_link_is_stated_as_a_population_witness(self):
        conv = self._convert(
            [["o1", "5", "Acme"], ["o2", "7", ""]],
            [["Acme", "US"], ["Globex", "US"]],
        )
        self.assertEqual(len(conv.relationships), 1)
        self.assertFalse(conv.relationships[0].required)
        pops = wikidbs.real_populations(
            "t", conv.tables, conv.relationships, conv.rows
        )
        primary = next(p for p in pops if p.name is PopulationName.PRIMARY)
        stated = [c for c in primary.conditions if "MISSING LINK WITNESS" in c]
        self.assertEqual(len(stated), 1, primary.conditions)
        self.assertIn("orders", stated[0])
        self.assertIn("customer_label", stated[0])
        self.assertIn("match no customers.customer_label", stated[0])

    # -- case C: the defect -------------------------------------------------

    def test_case_c_non_key_parent_is_refused_and_recorded_as_a_value_overlap(self):
        conv = self._convert(
            [["o1", "5", "Acme"], ["o2", "7", "Acme"]],
            # 'Acme' twice: a label overlap, not a key. Every child row now
            # has TWO parent rows, so the star's COUNT would double.
            [["Acme", "US"], ["Acme", "CA"]],
        )
        self.assertEqual(conv.relationships, ())
        self.assertEqual(len(conv.value_overlaps), 1)
        overlap = conv.value_overlaps[0]
        self.assertEqual(overlap.parent_table, "customers")
        self.assertEqual(overlap.parent_column, "customer_label")
        self.assertEqual((overlap.parent_rows, overlap.parent_distinct), (2, 1))
        self.assertIn("not a key", overlap.note)

    def test_case_c_a_nullable_parent_column_is_also_not_a_key(self):
        conv = self._convert(
            [["o1", "5", "Acme"]], [["Acme", "US"], ["", "CA"]]
        )
        self.assertEqual(conv.relationships, ())
        self.assertEqual(conv.value_overlaps[0].parent_nulls, 1)

    # -- case D ------------------------------------------------------------

    def test_case_d_an_edge_with_no_witness_is_dropped(self):
        conv = self._convert(
            [["o1", "5", "Nowhere Ltd"], ["o2", "7", ""]],
            [["Acme", "US"], ["Globex", "US"]],
        )
        self.assertEqual(conv.relationships, ())
        self.assertEqual(conv.value_overlaps, ())
        self.assertEqual(
            conv.witnessless_edges, ("orders.customer_label -> customers.customer_label",)
        )

    # -- case E ------------------------------------------------------------

    def test_case_e_a_database_with_no_usable_edge_is_refused(self):
        db = _write_keyness_db(
            self.root,
            orders=[_ORDER_HEADER, ["o1", "5", "Acme"], ["o2", "7", "Acme"]],
            customers=[_CUSTOMER_HEADER, ["Acme", "US"], ["Acme", "CA"]],
        )
        with self.assertRaises(wikidbs.WikiDbsIngestError) as caught:
            wikidbs.to_task_ir(db, family_map_path=self.root / "missing.csv.gz")
        self.assertIn("no usable foreign keys", str(caught.exception))

    # -- the mart-level second reading -------------------------------------

    def test_a_star_refuses_rather_than_grain_on_a_non_key(self):
        conv = self._convert(
            [["o1", "5", "Acme"], ["o2", "7", "Acme"]],
            [["Acme", "US"], ["Acme", "CA"]],
        )
        # Hand `_build_legacy_star` the edge `_convert` refused: the second
        # reading must reach the same verdict from the rows alone.
        rel = wikidbs.Relationship(
            child_table="orders",
            child_columns=("customer_label",),
            parent_table="customers",
            parent_columns=("customer_label",),
            required=False,
        )
        with self.assertRaises(wikidbs.WikiDbsIngestError):
            wikidbs._build_legacy_star(
                conv.tables, (rel,), "keyness_db", None, rows=conv.rows
            )

    def test_picking_a_grain_with_no_edges_states_the_reason(self):
        with self.assertRaises(wikidbs.WikiDbsIngestError) as caught:
            wikidbs._pick_relationship(())
        self.assertIn("no join surface", str(caught.exception))


class TestUsableEdgeRanking(unittest.TestCase):
    """The shortlist must rank on edges that EXIST, not edges merely declared.

    Measured over 1,151 in-envelope part-0 databases, the share of declared
    edges with a non-key parent rises monotonically with FK richness (21.5% at
    1-3 edges, 72.9% at 16+), because an FK-rich WikiDBs database is FK-rich
    precisely because it is a label-lookup schema. Ranking on
    `-foreign_key_count` therefore sought out the databases this adapter
    cannot use: the corpus it selected measured 70.6% non-key parents.
    """

    def _candidate(self, **kwargs):
        base = dict(
            node_id=1,
            directory="00001 x",
            part="part-0",
            component="c00001",
            component_size=1,
            table_count=3,
            foreign_key_count=1,
            usable_foreign_key_count=1,
            row_count=100,
            csv_bytes=10,
            eligible=True,
        )
        base.update(kwargs)
        return wikidbs.Candidate(**base)

    def test_many_declared_edges_lose_to_fewer_usable_ones(self):
        rich_but_useless = self._candidate(
            node_id=1, foreign_key_count=20, usable_foreign_key_count=1
        )
        modest_but_real = self._candidate(
            node_id=2, foreign_key_count=4, usable_foreign_key_count=4
        )
        self.assertLess(modest_but_real.sort_key, rich_but_useless.sort_key)

    def test_usable_edges_are_counted_from_the_shipped_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _write_keyness_db(
                root,
                orders=[_ORDER_HEADER, ["o1", "5", "Acme"]],
                customers=[_CUSTOMER_HEADER, ["Acme", "US"], ["Acme", "CA"]],
            )
            schema = wikidbs.load_schema(db)
            raw = {t.table_name: wikidbs.read_rows(db, t) for t in schema.tables}
            self.assertEqual(wikidbs._usable_edge_count(schema, raw), 0)
            # Make the parent column a key and the same edge becomes usable.
            with (db / "tables" / "customers.csv").open(
                "w", encoding="utf-8", newline=""
            ) as fh:
                csv.writer(fh).writerows(
                    [_CUSTOMER_HEADER, ["Acme", "US"], ["Globex", "CA"]]
                )
            raw = {t.table_name: wikidbs.read_rows(db, t) for t in schema.tables}
            self.assertEqual(wikidbs._usable_edge_count(schema, raw), 1)


# ---------------------------------------------------------------------------
# A chain-funding fixture: parent with a TEXT attribute, bridge with a numeric
# NOT NULL measure, fan-out 3 on one parent — everything argmax_profile needs
# ---------------------------------------------------------------------------

def _chain_schema() -> dict:
    """players(team_name) -> teams(team_name): a chain-fundable edge."""
    return {
        "database_name": "chain_teams_db",
        "tables": [
            {
                "table_name": "teams",
                "file_name": "teams.csv",
                "columns": [
                    {"column_name": "team_name", "data_type": "string"},
                    {"column_name": "team_label", "data_type": "string"},
                ],
                "foreign_keys": [],
            },
            {
                "table_name": "players",
                "file_name": "players.csv",
                "columns": [
                    {"column_name": "player_id", "data_type": "quantity"},
                    {"column_name": "player_label", "data_type": "string"},
                    {"column_name": "score", "data_type": "quantity"},
                    {"column_name": "team_name", "data_type": "string"},
                ],
                "foreign_keys": [
                    {
                        "column_name": "team_name",
                        "reference_column_name": "team_name",
                        "reference_table_name": "teams",
                    }
                ],
            },
        ],
    }


_TEAM_HEADER = ["team_name", "team_label"]
#: `player_id` is unique per row, so `_convert` measures it as an IDENTIFIER
#: column: the library takes it as the countable key and never as a measure.
#: `score` therefore carries at least one repeated value in every fixture — a
#: numeric column whose values are ALL distinct is an identifier by the pool's
#: own rule and is barred from `bridge_amount` (00012 shipped SUM(rider_id)).
_PLAYER_HEADER = ["player_id", "player_label", "score", "team_name"]

#: Fan-out 4 on Alpha, 1 on Beta — and NO childless team: the vendor's rows
#: alone cannot witness LEFT-vs-INNER on this edge. Cal and Eve share Alpha's
#: largest score under different labels, the one tie an argmax tie-break can
#: decide; without it the adapter no longer selects the `top` mart
#: (batch10 2026-09-11).
_PLAYERS_FANOUT = [
    ["1", "Ann", "10", "Alpha"],
    ["2", "Bob", "20", "Alpha"],
    ["3", "Cal", "30", "Alpha"],
    ["5", "Eve", "30", "Alpha"],
    ["4", "Dee", "20", "Beta"],
]


def _write_chain_db(root: Path, *, teams, players, dirname: str = "00005 chain_teams_db") -> Path:
    db = root / dirname
    (db / "tables").mkdir(parents=True, exist_ok=True)
    (db / "schema.json").write_text(
        json.dumps(_chain_schema(), indent=1), encoding="utf-8"
    )
    for name, rows in (("teams.csv", teams), ("players.csv", players)):
        with (db / "tables" / name).open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
    return db


def _write_optional_dimension_chain_db(root: Path) -> Path:
    """teams <- players -> zroles, with optionality caused only by one NULL."""
    db = root / "00006 optional_dimension_chain_db"
    (db / "tables").mkdir(parents=True, exist_ok=True)
    schema = {
        "database_name": "optional_dimension_chain_db",
        "tables": [
            {
                "table_name": "teams",
                "file_name": "teams.csv",
                "columns": [
                    {"column_name": "team_name", "data_type": "string"},
                    {"column_name": "team_label", "data_type": "string"},
                ],
                "foreign_keys": [],
            },
            {
                "table_name": "zroles",
                "file_name": "zroles.csv",
                "columns": [
                    {"column_name": "role_name", "data_type": "string"},
                    {"column_name": "role_label", "data_type": "string"},
                ],
                "foreign_keys": [],
            },
            {
                "table_name": "players",
                "file_name": "players.csv",
                "columns": [
                    {"column_name": "player_id", "data_type": "quantity"},
                    {"column_name": "player_label", "data_type": "string"},
                    {"column_name": "player_state", "data_type": "string"},
                    {"column_name": "score", "data_type": "quantity"},
                    {"column_name": "team_name", "data_type": "string"},
                    {"column_name": "role_name", "data_type": "string"},
                ],
                "foreign_keys": [
                    {
                        "column_name": "team_name",
                        "reference_column_name": "team_name",
                        "reference_table_name": "teams",
                    },
                    {
                        "column_name": "role_name",
                        "reference_column_name": "role_name",
                        "reference_table_name": "zroles",
                    },
                ],
            },
        ],
    }
    (db / "schema.json").write_text(json.dumps(schema, indent=1), encoding="utf-8")
    fixtures = {
        "teams.csv": (
            ("team_name", "team_label"),
            ("Alpha", "First squad"),
            ("Beta", "Second squad"),
        ),
        "zroles.csv": (
            ("role_name", "role_label"),
            ("Striker", "Attack"),
            ("Defender", "Defence"),
            ("Keeper", "Goal"),
        ),
        "players.csv": (
            (
                "player_id",
                "player_label",
                "player_state",
                "score",
                "team_name",
                "role_name",
            ),
            ("1", "Ann", "active", "10", "Alpha", "Striker"),
            ("2", "Bob", "inactive", "20", "Alpha", "Defender"),
            ("3", "Cal", "active", "30", "Alpha", ""),
            ("4", "Dee", "inactive", "20", "Beta", "Striker"),
            ("5", "Eve", "active", "20", "Beta", "Keeper"),
            ("6", "Fox", "inactive", "30", "Beta", "Defender"),
        ),
    }
    for name, values in fixtures.items():
        with (db / "tables" / name).open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            csv.writer(handle).writerows(values)
    return db


class TestOptionalDanglingMartEdge(unittest.TestCase):
    """The real-row carve-out must kill the join it claims to exercise."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map_path = _fixture_map(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _execute(task, population, mart, sql):
        con = duckdb.connect(":memory:")
        try:
            for table in task.tables:
                reference_solution.create_table(con, table)
                rows = population.literal_rows[table.name]
                if not rows:
                    continue
                columns = tuple(column.name for column in table.columns)
                marks = ", ".join("?" for _ in columns)
                quoted = ", ".join(f'"{column}"' for column in columns)
                con.executemany(
                    f'INSERT INTO "{table.name}" ({quoted}) VALUES ({marks})',
                    [tuple(row.get(column) for column in columns) for row in rows],
                )
            return reference_solution.execute_mart(con, mart, sql)
        finally:
            con.close()

    def test_optional_second_hop_is_real_and_executable(self):
        task = wikidbs.to_task_ir(
            _write_optional_dimension_chain_db(self.root),
            family_map_path=self.map_path,
        )
        mart = next(mart for mart in task.marts if mart.name.endswith("_rollup"))
        primary = task.population(PopulationName.PRIMARY)
        counter = task.population(PopulationName.COUNTERFACTUAL)
        rel = next(
            rel
            for rel in task.relationships
            if rel.child_table == "players" and rel.parent_table == "zroles"
        )
        self.assertFalse(rel.required)

        # A NULL optional second-hop key matches no role, but the bridge row is
        # still visible to the team-grained mart through players.team_name.
        # Population prose must state only the measured edge fact, never claim
        # that such a row cannot influence any parent-grained mart output.
        missing_conditions = tuple(
            condition
            for condition in primary.conditions
            if "MISSING LINK WITNESS" in condition
            and "players" in condition
            and "role_name" in condition
        )
        self.assertTrue(missing_conditions, primary.conditions)
        self.assertTrue(
            all(
                "influence no parent-grained mart row" not in condition
                for condition in missing_conditions
            ),
            missing_conditions,
        )
        without_null_bridge = primary.model_copy(
            update={
                "literal_rows": {
                    **primary.literal_rows,
                    "players": tuple(
                        row
                        for row in primary.literal_rows["players"]
                        if row["role_name"] is not None
                    ),
                }
            }
        )
        sql = task.reference.sql_by_mart[mart.name]
        self.assertNotEqual(
            self._execute(task, primary, mart, sql),
            self._execute(task, without_null_bridge, mart, sql),
            "the NULL optional-second-hop bridge row must affect the rollup",
        )

        role_keys = {
            row[rel.parent_columns[0]]
            for row in counter.literal_rows[rel.parent_table]
        }
        self.assertTrue(
            any(
                row[rel.child_columns[0]] is not None
                and row[rel.child_columns[0]] not in role_keys
                for row in counter.literal_rows[rel.child_table]
            ),
            "counterfactual did not retain a bridge row after removing its role",
        )

        mutant = attacks._apply_kind(
            AttackKind.INNER_JOIN, sql, mart, "second_hop"
        )
        self.assertIsNotNone(mutant)
        gold = self._execute(task, counter, mart, sql)
        wrong = self._execute(task, counter, mart, mutant)
        self.assertNotEqual(gold, wrong)

        cases = {case.name: case for case in task.attack_cases}
        self.assertEqual(
            cases["inner_join__second_hop"].expected_pass,
            {PopulationName.COUNTERFACTUAL: False},
        )

    def test_computed_grain_one_hop_uses_explicit_child_link_pairs(self):
        """orphan_coverage has no source parent_keys but its join still counts."""
        db = _write_optional_dimension_chain_db(self.root)
        base = wikidbs.to_task_ir(db, family_map_path=self.map_path)
        converted = wikidbs._convert(db, wikidbs.load_schema(db))
        evidence = next(
            candidate
            for candidate in wikidbs.chain_candidates(
                converted.tables,
                converted.relationships,
                observed=wikidbs.observed_domains(
                    converted.tables,
                    {name: list(rows) for name, rows in converted.rows.items()},
                ),
                exclude_measures=converted.identifier_columns,
            )
            if candidate.parent == "zroles"
        )
        self.assertTrue(evidence.owner_link_optional)
        built = mart_plan_mod.orphan_coverage(
            evidence, mart="players_zroles_orphan_coverage"
        )
        self.assertEqual(built.shape.parent_keys, ())
        mart = MartSpec(
            name=built.plan.mart,
            description="Coverage of players whose optional role is absent.",
            grain="One row per role value carried by players.",
            key_columns=built.shape.key_columns,
            columns=built.columns,
            plan=built.plan,
        )
        populations = wikidbs.real_populations(
            base.task_id,
            converted.tables,
            converted.relationships,
            converted.rows,
            (mart,),
            (built.shape,),
        )
        task = base.model_copy(
            update={
                "marts": (mart,),
                "populations": populations,
                "attack_cases": (),
                "reference": None,
            }
        )
        task = task.model_copy(
            update={"reference": reference_solution.build_reference(task)}
        )
        counter = task.population(PopulationName.COUNTERFACTUAL)
        role_keys = {
            row["role_name"] for row in counter.literal_rows["zroles"]
        }
        self.assertTrue(
            any(
                row["role_name"] is not None
                and row["role_name"] not in role_keys
                for row in counter.literal_rows["players"]
            ),
            "computed-grain branch did not add a non-null dangling owner key",
        )
        self.assertTrue(
            any("DANGLING LINK" in condition for condition in counter.conditions),
            counter.conditions,
        )
        sql = task.reference.sql_by_mart[mart.name]
        mutant = attacks._apply_kind(AttackKind.INNER_JOIN, sql, mart)
        self.assertIsNotNone(mutant)
        self.assertNotEqual(
            self._execute(task, counter, mart, sql),
            self._execute(task, counter, mart, mutant),
        )


class TestIdentifierColumnsAreNeverMeasures(unittest.TestCase):
    """The pool's own rule — a numeric column whose real values are all
    distinct is an identifier, never a measure — now holds on the LIBRARY
    path too (00012 shipped SUM(pro_cycling_stats_cyclist_id) and ranked
    cyclists by 'largest pro_cycling_stats_cyclist_id')."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map_path = _fixture_map(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_identifier_columns_are_not_library_measures(self):
        # Every numeric column of the bridge is unique per row: both are
        # identifiers, so there is a countable key but NO measure. The
        # measure-hungry shapes are unfundable and the task falls back to the
        # legacy count-only star.
        players = [
            ["1", "Ann", "10", "Alpha"],
            ["2", "Bob", "20", "Alpha"],
            ["3", "Cal", "30", "Alpha"],
            ["4", "Dee", "40", "Beta"],
        ]
        db = _write_chain_db(
            self.root,
            teams=[_TEAM_HEADER, ["Alpha", "First squad"], ["Beta", "Second squad"]],
            players=[_PLAYER_HEADER] + players,
        )
        conv = wikidbs._convert(db, wikidbs.load_schema(db))
        self.assertEqual(conv.identifier_columns["players"], frozenset({"player_id", "score"}))
        candidates = wikidbs._chain_candidates_for(
            conv.tables, conv.relationships, conv.rows, conv.identifier_columns
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].bridge_amount, "")
        self.assertIn(candidates[0].bridge_key, {"player_id", "score"})
        marts, shapes = wikidbs._build_marts(
            conv.tables, conv.relationships, conv.rows, "chain_teams_db",
            conv.identifier_columns,
        )
        self.assertEqual(shapes[0].shape_name, "star")
        for mart in marts:
            for column in mart.columns:
                self.assertNotIn("Sum of score", column.description)
                self.assertNotIn("Sum of player_id", column.description)
                self.assertNotIn("largest score", column.description)
        task = wikidbs.to_task_ir(db, family_map_path=self.map_path)
        self.assertFalse(any(m.name.endswith("_top") for m in task.marts))

    def test_a_repeated_measure_funds_the_argmax_with_the_identifier_as_key(self):
        # One repeated value makes `score` a measure again; the argmax keys
        # on the identifier and measures on the score — disjoint roles.
        players = [
            ["1", "Ann", "10", "Alpha"],
            ["2", "Bob", "20", "Alpha"],
            ["3", "Cal", "30", "Alpha"],
            ["4", "Dee", "20", "Beta"],
        ]
        db = _write_chain_db(
            self.root,
            teams=[_TEAM_HEADER, ["Alpha", "First squad"], ["Beta", "Second squad"]],
            players=[_PLAYER_HEADER] + players,
        )
        conv = wikidbs._convert(db, wikidbs.load_schema(db))
        candidates = wikidbs._chain_candidates_for(
            conv.tables, conv.relationships, conv.rows, conv.identifier_columns
        )
        e = candidates[0]
        self.assertEqual((e.bridge_key, e.bridge_amount), ("player_id", "score"))
        self.assertEqual((e.parent_key, e.parent_attr), ("team_name", "team_label"))

    def test_nonunique_numeric_fallback_is_not_published_as_top_row_id(self):
        """A countable numeric column is not automatically a row identifier."""
        players = [
            ["1", "Ann", "10", "Alpha"],
            ["1", "Bob", "20", "Alpha"],
            ["2", "Cal", "20", "Alpha"],
            ["2", "Dee", "10", "Beta"],
        ]
        db = _write_chain_db(
            self.root,
            teams=[
                _TEAM_HEADER,
                ["Alpha", "First squad"],
                ["Beta", "Second squad"],
                ["Gamma", "No players"],
            ],
            players=[_PLAYER_HEADER] + players,
        )
        conv = wikidbs._convert(db, wikidbs.load_schema(db))
        candidates = wikidbs._chain_candidates_for(
            conv.tables, conv.relationships, conv.rows, conv.identifier_columns
        )
        self.assertTrue(candidates)
        self.assertFalse(candidates[0].bridge_key_is_unique)
        marts, _shapes = wikidbs._build_marts(
            conv.tables,
            conv.relationships,
            conv.rows,
            "chain_teams_db",
            conv.identifier_columns,
        )
        top = next(mart for mart in marts if mart.name.endswith("_top"))
        self.assertNotIn("top_row_id", {column.name for column in top.columns})


class TestChainFundingRanking(unittest.TestCase):
    """T1: the shortlist ranks FIRST on edges that fund plan-library chains.

    Measured before this ranking existed: the usable-edge ranker's top-8
    shipped exactly 1 legacy-star mart of 2-3 target columns on 8 of 8 tasks
    (17% of the anchor's 14.6-column median) because none of its picks funded
    a single chain. The funding count is measured with the SAME machinery
    `_build_marts` selects with, so it cannot drift from what ingest does.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _candidate(self, **kwargs):
        base = dict(
            node_id=1,
            directory="00001 x",
            part="part-0",
            component="c00001",
            component_size=1,
            table_count=3,
            foreign_key_count=1,
            usable_foreign_key_count=1,
            row_count=100,
            csv_bytes=10,
            eligible=True,
        )
        base.update(kwargs)
        return wikidbs.Candidate(**base)

    def test_funding_edge_beats_more_usable_but_starving_edges(self):
        starving = self._candidate(
            node_id=1, usable_foreign_key_count=8, chain_funding_edge_count=0
        )
        funding = self._candidate(
            node_id=2, usable_foreign_key_count=1, chain_funding_edge_count=1
        )
        self.assertLess(funding.sort_key, starving.sort_key)

    def test_funding_count_is_measured_with_the_real_selector(self):
        db = _write_chain_db(
            self.root,
            teams=[_TEAM_HEADER, ["Alpha", "First squad"], ["Beta", "Second squad"]],
            players=[_PLAYER_HEADER] + _PLAYERS_FANOUT,
        )
        schema = wikidbs.load_schema(db)
        self.assertEqual(wikidbs._chain_funding_edge_count(db, schema), 1)

    def test_a_one_to_one_edge_funds_nothing(self):
        # Every team has exactly ONE player: no fan-out, so COUNT and
        # COUNT(DISTINCT) can never disagree and the chain gate refuses.
        db = _write_chain_db(
            self.root,
            teams=[_TEAM_HEADER, ["Alpha", "First squad"], ["Beta", "Second squad"]],
            players=[
                _PLAYER_HEADER,
                ["1", "Ann", "10", "Alpha"],
                ["2", "Dee", "10", "Beta"],
            ],
        )
        schema = wikidbs.load_schema(db)
        self.assertEqual(wikidbs._chain_funding_edge_count(db, schema), 0)

    def test_an_unconvertible_database_funds_nothing(self):
        # A non-key parent is refused by `_convert`, so it cannot fund.
        db = _write_keyness_db(
            self.root,
            orders=[_ORDER_HEADER, ["o1", "5", "Acme"], ["o2", "7", "Acme"]],
            customers=[_CUSTOMER_HEADER, ["Acme", "US"], ["Acme", "CA"]],
        )
        schema = wikidbs.load_schema(db)
        self.assertEqual(wikidbs._chain_funding_edge_count(db, schema), 0)


class TestManufacturedChildlessWitness(unittest.TestCase):
    """T2: the childless gate is relaxed ONLY where the population policy
    manufactures the witness, and the attack claim follows the witness.

    The counterfactual population deterministically removes the facts of a
    slice of dimension rows so that "dimension rows with zero linked facts
    really occur" — its own conditions say so. A childless parent in a
    population the solver is graded on is a real witness, so the chain gate
    may see it (`_effective_links`); and the LEFT-vs-INNER / dropped-COALESCE
    attack cases must then claim the population that HOLDS the witness — a
    PRIMARY claim would be the false one. The key_parents gate (the C5 fix)
    is untouched throughout.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map_path = _fixture_map(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _db(self, *, childless_team: bool) -> Path:
        teams = [_TEAM_HEADER, ["Alpha", "First squad"], ["Beta", "Second squad"]]
        if childless_team:
            teams.append(["Gamma", "Third squad"])
        return _write_chain_db(
            self.root, teams=teams, players=[_PLAYER_HEADER] + _PLAYERS_FANOUT
        )

    def test_effective_links_merge_only_the_manufactured_childless(self):
        db = self._db(childless_team=False)
        conv = wikidbs._convert(db, wikidbs.load_schema(db))
        from elt_taskgen.adapters.evidence import link_statistics

        primary = link_statistics(
            conv.relationships, {k: list(v) for k, v in conv.rows.items()}
        )
        effective = wikidbs._effective_links(
            conv.tables, conv.relationships, conv.rows
        )
        edge = ("players", "team_name", "teams", "team_name")
        self.assertEqual(primary[edge][1], 0, "fixture must have no vendor childless")
        self.assertGreaterEqual(effective[edge][1], 1, "carve-out witness not merged")
        # Fan-out is NOT merged: the carve-out only removes rows.
        self.assertEqual(primary[edge][0], effective[edge][0])

    def test_manufactured_witness_funds_a_chain_mart(self):
        task = wikidbs.to_task_ir(
            self._db(childless_team=False), family_map_path=self.map_path
        )
        self.assertEqual(len(task.marts), 2)
        self.assertEqual(
            {mart.name.rsplit("_", 1)[-1] for mart in task.marts},
            {"distribution", "top"},
        )
        # The chain mart is WIDER than the 2-3 column legacy star and every
        # column declares its kind (the 55%-computed instrument).
        for mart in task.marts:
            self.assertGreaterEqual(len(mart.columns), 6)
            for column in mart.columns:
                self.assertIsNotNone(column.kind, f"{column.name} declares no kind")
        # The real-data policy is intact on every population.
        for pop in task.populations:
            self.assertTrue(pop.literal_rows)
            self.assertIn(wikidbs.REAL_DATA_POLICY, " ".join(pop.conditions))

    def test_manufactured_witness_keeps_the_counterfactual_claim(self):
        task = wikidbs.to_task_ir(
            self._db(childless_team=False), family_map_path=self.map_path
        )
        by_name = {c.name: c for c in task.attack_cases}
        for name in ("inner_join", "no_null_default"):
            case = by_name.get(name)
            if case is None:
                continue
            self.assertEqual(
                case.expected_pass,
                {PopulationName.COUNTERFACTUAL: False},
                f"{name} must claim the population that holds the witness",
            )
        # Every other counterfactual-derived claim still moves to PRIMARY.
        repointed = [
            c
            for c in task.attack_cases
            if c.expected_pass == {PopulationName.PRIMARY: False}
        ]
        self.assertTrue(repointed, "the re-pointing must survive the exception")

    def test_vendor_witnessed_childless_keeps_the_primary_claim(self):
        task = wikidbs.to_task_ir(
            self._db(childless_team=True), family_map_path=self.map_path
        )
        self.assertTrue(any(mart.name.endswith("_top") for mart in task.marts))
        case = {c.name: c for c in task.attack_cases}["inner_join"]
        self.assertEqual(
            case.expected_pass,
            {PopulationName.PRIMARY: False},
            "a vendor-witnessed childless parent keeps today's PRIMARY claim",
        )

    def test_chain_ingest_is_deterministic(self):
        db = self._db(childless_team=False)
        first = wikidbs.to_task_ir(db, family_map_path=self.map_path)
        again = wikidbs.to_task_ir(db, family_map_path=self.map_path)
        self.assertEqual(first.content_hash(), again.content_hash())

    def test_chain_grain_rests_on_a_measured_key(self):
        # The C5 predicate, re-read from the emitted task: the mart grains on
        # teams.team_name, and that column is a KEY of the shipped rows.
        task = wikidbs.to_task_ir(
            self._db(childless_team=False), family_map_path=self.map_path
        )
        primary = task.population(PopulationName.PRIMARY)
        self.assertTrue(wikidbs.is_key_of(primary.literal_rows["teams"], "team_name"))


class TestTieWitnessedBoundaryClaim(unittest.TestCase):
    """`custom@wrong_boundary_else` claims the population that has the TIE.

    The argmax ladder is ``CASE WHEN tied_count = 0 THEN 'empty' WHEN
    tied_count = 1 THEN 'unique' ELSE 'tied' END``, so dropping its mandatory
    ELSE is observable on ONE row shape: a parent whose maximum measure is
    held by two or more fact rows. On a pool whose measures are real vendored
    values (00012 METEC_SOLARWATT_TEAM_MEMBERS_DB grades on an identifier
    column) that row can be absent from the vendor's own rows and present only
    in STRESS, which appends byte-exact replicas of real leaf rows. Measured
    there before the fix: declared {primary: False}, measured primary=1.0 and
    stress=0.0 — a declared matrix measurement contradicts.

    The witness is MEASURED with `_top_tie_rows` on both candidate row sets,
    never assumed, and the claim follows it. STRESS is in
    `gates.GRADED_POPULATIONS`, so the case stays real required evidence.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map_path = _fixture_map(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _db(self, players, dirname):
        return _write_chain_db(
            self.root,
            teams=[_TEAM_HEADER, ["Alpha", "First squad"], ["Beta", "Second squad"]],
            players=[_PLAYER_HEADER] + players,
            dirname=dirname,
        )

    #: Alpha's maximum is the FIRST player row, which is exactly the row the
    #: stress population replicates — so the tie exists there and nowhere else.
    _STRESS_TIE = [
        ["1", "Ann", "30", "Alpha"],
        ["2", "Bob", "20", "Alpha"],
        ["3", "Cal", "10", "Alpha"],
        ["4", "Dee", "20", "Beta"],
    ]
    #: Two vendored rows already share Alpha's maximum: the vendor witnesses it.
    _PRIMARY_TIE = [
        ["1", "Ann", "30", "Alpha"],
        ["2", "Bob", "30", "Alpha"],
        ["3", "Cal", "10", "Alpha"],
        ["4", "Dee", "40", "Beta"],
    ]

    def _boundary_case(self, players, dirname):
        task = wikidbs.to_task_ir(
            self._db(players, dirname), family_map_path=self.map_path
        )
        by_name = {c.name: c for c in task.attack_cases}
        self.assertIn(
            "custom__wrong_boundary_else",
            by_name,
            "the argmax ladder must still declare its boundary mutant",
        )
        return by_name["custom__wrong_boundary_else"]

    def test_top_tie_is_measured_not_assumed(self):
        """A replica tie (STRESS) exercises tied_count and tie_state; only a
        tie between DISTINCT rows with DIFFERENT labels exercises the
        tie-break. Both are measured, and only the second funds the mart."""
        db = self._db(self._PRIMARY_TIE, "00001 chain_primary_tie_db")
        conv = wikidbs._convert(db, wikidbs.load_schema(db))
        _marts, shapes = wikidbs._build_marts(
            conv.tables, conv.relationships, conv.rows,
            "chain_primary_tie_db", conv.identifier_columns,
        )
        shape = next(shape for shape in shapes if shape.shape_name == "argmax_profile")
        stress = wikidbs._stress_rows(conv.tables, conv.relationships, conv.rows)
        self.assertEqual(wikidbs._top_tie_rows(shape, conv.rows), 1)
        self.assertEqual(wikidbs._top_tie_rows(shape, conv.rows, distinct_labels=True), 1)
        self.assertIs(
            wikidbs._tie_witness_population(shapes, conv.rows, stress),
            PopulationName.PRIMARY,
        )
        # The stress-only fixture: a replica ties, but under the same label.
        db2 = self._db(self._STRESS_TIE, "00002 chain_stress_tie_db")
        conv2 = wikidbs._convert(db2, wikidbs.load_schema(db2))
        stress2 = wikidbs._stress_rows(conv2.tables, conv2.relationships, conv2.rows)
        self.assertEqual(wikidbs._top_tie_rows(shape, conv2.rows), 0)
        self.assertEqual(wikidbs._top_tie_rows(shape, stress2), 1)
        self.assertEqual(wikidbs._top_tie_rows(shape, stress2, distinct_labels=True), 0)

    def test_a_stress_only_tie_does_not_fund_the_argmax(self):
        """batch10 2026-09-11, wikidbs__c60432: the stress replicas tied at the
        maximum, the boundary claim was re-pointed at STRESS, and the
        population adversary still refused the task — a replica carries the
        same label, so the declared tie-break (smallest label first) decided
        nothing on any population. The argmax is not selected on such rows;
        the adapter takes the next shape instead."""
        task = wikidbs.to_task_ir(
            self._db(self._STRESS_TIE, "00002 chain_stress_claim_db"),
            family_map_path=self.map_path,
        )
        self.assertFalse(any(m.name.endswith("_top") for m in task.marts))
        self.assertNotIn(
            "custom__wrong_boundary_else", {c.name for c in task.attack_cases}
        )

    def test_a_vendor_witnessed_tie_keeps_the_primary_claim(self):
        case = self._boundary_case(self._PRIMARY_TIE, "00003 chain_primary_tie_db")
        self.assertEqual(case.expected_pass, {PopulationName.PRIMARY: False})

    def test_a_vendor_witnessed_tie_is_stated_to_the_adversary(self):
        """The population adversary reads conditions, never rows: the
        populations whose rows hold the decisive tie say so."""
        task = wikidbs.to_task_ir(
            self._db(self._PRIMARY_TIE, "00006 chain_primary_witness_db"),
            family_map_path=self.map_path,
        )
        stated = {
            pop.name: any(c.startswith("TIE WITNESS: players") for c in pop.conditions)
            for pop in task.populations
        }
        for name in (PopulationName.PRIMARY, PopulationName.RESAMPLED, PopulationName.STRESS):
            self.assertTrue(stated[name], name)
        line = next(
            c for c in task.population(PopulationName.PRIMARY).conditions
            if c.startswith("TIE WITNESS")
        )
        self.assertIn("differ in player_label", line)
        self.assertIn("tie-break on player_label decides", line)

    def test_no_tie_anywhere_does_not_fund_the_argmax(self):
        task = wikidbs.to_task_ir(
            self._db(
                [
                    ["1", "Ann", "10", "Alpha"],
                    ["2", "Bob", "20", "Alpha"],
                    ["3", "Cal", "30", "Alpha"],
                    ["4", "Dee", "20", "Beta"],
                ],
                "00004 chain_no_tie_db",
            ),
            family_map_path=self.map_path,
        )
        self.assertFalse(any(m.name.endswith("_top") for m in task.marts))

    def test_the_one_stress_rule_is_shared_with_the_population_builder(self):
        # Two derivations of the stress rows could disagree, and a catalogue
        # pointed at rows built differently is the same bug in a new place.
        db = self._db(self._STRESS_TIE, "00005 chain_stress_shared_db")
        conv = wikidbs._convert(db, wikidbs.load_schema(db))
        built = wikidbs._stress_rows(conv.tables, conv.relationships, conv.rows)
        pops = wikidbs.real_populations(
            "t", conv.tables, conv.relationships, conv.rows
        )
        stress = next(p for p in pops if p.name is PopulationName.STRESS)
        self.assertEqual(dict(stress.literal_rows), dict(built))


class TestArgmaxWitnessMeasurement(unittest.TestCase):
    """The two argmax facts a population can only be TOLD about, measured on
    plain row dicts: a tie between distinct rows with different labels, and a
    parent whose linked rows all lack the measure."""

    def _shape(self):
        from elt_taskgen.generation.mart_plan import FactRoles, StarShape

        return StarShape(
            mart="top", parent="teams", parent_keys=("team_name",),
            key_columns=("parent_key",), fact="players",
            fact_link_columns=("team_name",), shape_name="argmax_profile",
            roles=FactRoles(link_key="player_id", measure="score", label="player_label"),
        )

    def test_distinct_label_tie_and_all_missing_are_measured(self):
        shape = self._shape()
        rows = {
            "teams": ({"team_name": "Alpha"}, {"team_name": "Beta"}, {"team_name": "Gamma"}),
            "players": (
                {"player_id": 1, "player_label": "Ann", "score": 30, "team_name": "Alpha"},
                {"player_id": 2, "player_label": "Bob", "score": 30, "team_name": "Alpha"},
                {"player_id": 3, "player_label": "Cal", "score": 10, "team_name": "Alpha"},
                # Beta: a replica ties under the SAME label — no tie-break decision.
                {"player_id": 4, "player_label": "Dee", "score": 20, "team_name": "Beta"},
                {"player_id": 4, "player_label": "Dee", "score": 20, "team_name": "Beta"},
                # Gamma: linked rows, none with a measure.
                {"player_id": 5, "player_label": "Eve", "score": None, "team_name": "Gamma"},
                {"player_id": 6, "player_label": "Fay", "score": None, "team_name": "Gamma"},
            ),
        }
        self.assertEqual(wikidbs._top_tie_rows(shape, rows), 2)
        self.assertEqual(wikidbs._top_tie_rows(shape, rows, distinct_labels=True), 1)
        self.assertEqual(wikidbs._all_missing_measure_parents(shape, rows), 1)
        lines = wikidbs._tie_witness_conditions((shape,), rows)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("TIE WITNESS: players"))
        self.assertIn("differ in player_label", lines[0])
        self.assertTrue(lines[1].startswith("ALL-MISSING-MEASURE WITNESS: at least one teams row"))
        # Neither fact: nothing is claimed.
        bare = {"teams": rows["teams"], "players": rows["players"][2:4]}
        self.assertEqual(wikidbs._tie_witness_conditions((shape,), bare), ())


class TestRenderableDomains(unittest.TestCase):
    """An observed domain value becomes a SQL string literal in the library's
    predicates, and the library does not escape embedded quotes — measured on
    part-0: "Province of L'Aquila" crashes `fan_out_rollup` at build time.
    The adapter refuses the material at selection instead."""

    def test_quoted_value_drops_the_whole_domain(self):
        domains = {
            ("t", "status"): ("active", "Province of L'Aquila"),
            ("t", "kind"): ("red", "blue", "green"),
        }
        cleaned = wikidbs._renderable_domains(domains)
        self.assertNotIn(("t", "status"), cleaned)
        self.assertEqual(cleaned[("t", "kind")], ("red", "blue", "green"))


class TestDedupeFaithfulMarts(unittest.TestCase):
    """A DEDUPE step over the plan's carried columns must collapse ONLY
    byte-identical rows, because that is what the emitted column description
    promises. Measured on part-0 00160: 53 pairwise-distinct bridge rows,
    three pairs agreeing on every carried column, gold=50 under a sentence
    promising 53 — the wrong-gold defect class. The adapter drops the
    combination rather than freezing a self-contradicting answer key."""

    def _mart(self, dedupe_columns):
        from elt_taskgen.models import (
            ColumnType,
            MartColumn,
            MartOp,
            MartOpKind,
            MartPlan,
            MartSpec,
        )

        return MartSpec(
            name="m",
            grain="One row per k.",
            key_columns=("k",),
            columns=(
                MartColumn(name="k", type=ColumnType.TEXT, description="Key."),
            ),
            plan=MartPlan(
                mart="m",
                ops=(
                    MartOp(
                        kind=MartOpKind.DEDUPE,
                        description="Distinct bridge rows.",
                        tables=("bridge",),
                        columns=tuple(dedupe_columns),
                    ),
                ),
            ),
        )

    def test_lossy_projection_is_dropped(self):
        rows = {
            "bridge": (
                {"a": 1, "b": "x", "extra": "left"},
                {"a": 1, "b": "x", "extra": "right"},  # distinct row, collapses
            )
        }
        marts, shapes = wikidbs._dedupe_faithful_marts(
            (self._mart(("a", "b")),), ("shape",), rows
        )
        self.assertEqual(marts, ())
        self.assertEqual(shapes, ())

    def test_byte_identical_duplicates_are_faithful(self):
        rows = {
            "bridge": (
                {"a": 1, "b": "x", "extra": "same"},
                {"a": 1, "b": "x", "extra": "same"},  # byte-identical: fine
                {"a": 2, "b": "y", "extra": "other"},
            )
        }
        marts, shapes = wikidbs._dedupe_faithful_marts(
            (self._mart(("a", "b")),), ("shape",), rows
        )
        self.assertEqual(len(marts), 1)
        self.assertEqual(shapes, ("shape",))

    def test_full_row_projection_is_always_faithful(self):
        # The legacy star dedupes over EVERY fact column: faithful by
        # construction, whatever the rows are.
        rows = {
            "bridge": (
                {"a": 1, "b": "x", "extra": "left"},
                {"a": 1, "b": "x", "extra": "right"},
            )
        }
        marts, _shapes = wikidbs._dedupe_faithful_marts(
            (self._mart(("a", "b", "extra")),), ("shape",), rows
        )
        self.assertEqual(len(marts), 1)

    def test_empty_column_tuple_means_select_distinct_star(self):
        rows = {
            "bridge": (
                {"a": 1, "b": "x", "extra": "left"},
                {"a": 1, "b": "x", "extra": "right"},
            )
        }
        marts, _shapes = wikidbs._dedupe_faithful_marts(
            (self._mart(()),), ("shape",), rows
        )
        self.assertEqual(len(marts), 1)


class TestRealRowShapeClaims(unittest.TestCase):
    def _shape(self, name, claims):
        return wikidbs.StarShape(
            mart="m",
            parent="parents",
            parent_keys=("id",),
            key_columns=("parent_key",),
            shape_name=name,
            attack_claims=tuple(claims),
        )

    def test_cohort_claims_keep_only_real_row_guarantees(self):
        shape = self._shape(
            "status_cohort_union",
            (
                "dropped_filter",
                "inner_join",
                "no_dedup",
                "no_null_default",
                "wrong_denominator",
            ),
        )
        (safe,) = wikidbs._real_row_safe_shapes((shape,))
        self.assertEqual(
            ("dropped_filter", "inner_join", "no_null_default"),
            safe.attack_claims,
        )

    def test_snapshot_drops_unmeasured_order_and_ratio_claims(self):
        shape = self._shape(
            "latest_snapshot",
            ("inner_join", "no_null_default", "wrong_denominator", "wrong_window"),
        )
        (safe,) = wikidbs._real_row_safe_shapes((shape,))
        self.assertEqual(("inner_join", "no_null_default"), safe.attack_claims)

    def test_measure_distribution_drops_unmeasured_ratio_claim(self):
        shape = self._shape(
            "measure_state_distribution",
            ("dropped_filter", "inner_join", "no_null_default", "wrong_denominator"),
        )
        (safe,) = wikidbs._real_row_safe_shapes((shape,))
        self.assertEqual(
            ("dropped_filter", "inner_join", "no_null_default"),
            safe.attack_claims,
        )

    def test_other_shapes_are_unchanged(self):
        shape = self._shape("fan_out_rollup", ("no_dedup", "wrong_grain"))
        (same,) = wikidbs._real_row_safe_shapes((shape,))
        self.assertIs(shape, same)

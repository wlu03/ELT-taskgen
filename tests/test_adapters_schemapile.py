"""Tests for adapters/schemapile.py, tools/schemapile_index.py and the CLI.

The fixture reproduces the REAL schemapile-perm.json shape — one top-level JSON
object keyed by "<file>.sql", each value ``{"INFO": {URL, LICENSE, PERMISSIVE},
"TABLES": {<table>: {"COLUMNS": {...}, "PRIMARY_KEYS": [...],
"FOREIGN_KEYS": [...], "CHECKS": [...], "INDEXES": [...], "COMMENT": ...}}}`` —
and exercises the four properties the pool stands on:

  * the streaming decoder reads the object incrementally (tiny chunk sizes
    force the buffer-refill path) and fails closed on truncation;
  * clustering fuses records that share an origin REPOSITORY or a normalized
    SHAPE, so one repo's six migration files are ONE family;
  * a record that is not PERMISSIVE (or carries no LICENSE) is refused;
  * the configurable relational filter keeps 2-table fragments out.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from elt_taskgen.adapters import schemapile as sp  # noqa: E402
from elt_taskgen.generation import populations as populations_mod  # noqa: E402
from elt_taskgen.models import ColumnType, Origin, PopulationName  # noqa: E402
from tools import schemapile_index as spi  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture: the real record shape, in miniature
# ---------------------------------------------------------------------------

REPO_A = "https://github.com/example/shop/blob/abc123/db/{}?raw=true\n"
REPO_B = "https://github.com/other/fork/blob/def456/sql/{}?raw=true"
REPO_C = "https://github.com/tiny/frag/blob/999/one.sql"
REPO_D = "https://github.com/closed/source/blob/111/priv.sql"


def _col(type_: str, *, nullable=None, primary=False, checks=None, comment=None) -> dict:
    return {
        "TYPE": type_,
        "NULLABLE": nullable,
        "UNIQUE": None,
        "DEFAULT": None,
        "CHECKS": list(checks or []),
        "IS_PRIMARY": primary,
        "IS_INDEX": False,
        "COMMENT": comment,
    }


def _table(columns: dict, *, pks=(), fks=(), checks=(), indexes=()) -> dict:
    return {
        "COLUMNS": columns,
        "PRIMARY_KEYS": list(pks),
        "FOREIGN_KEYS": list(fks),
        "CHECKS": list(checks),
        "INDEXES": list(indexes),
        "COMMENT": None,
    }


def _fk(columns, table, referred) -> dict:
    return {
        "COLUMNS": list(columns),
        "FOREIGN_TABLE": table,
        "REFERRED_COLUMNS": list(referred),
        "ON_DELETE": "Cascade",
        "ON_UPDATE": None,
    }


def shop_tables() -> dict:
    """5 tables, 4 resolvable FKs, 17 columns — clears the default filter."""
    return {
        "customers": _table(
            {
                "customer_id": _col("Int", nullable=False, primary=True),
                "customer_name": _col("Varchar", nullable=False, comment="Legal name."),
                "region": _col("Varchar"),
            },
            pks=["customer_id"],
        ),
        "orders": _table(
            {
                "order_id": _col("BigInt", nullable=False, primary=True),
                "customer_id": _col("Int", nullable=False),
                "amount": _col("Decimal(10,2)"),
                "status": _col("Varchar", checks=["status IN ('open', 'closed')"]),
            },
            pks=["order_id"],
            fks=[_fk(["customer_id"], "customers", ["customer_id"])],
        ),
        "order_items": _table(
            {
                "item_id": _col("Int", nullable=False, primary=True),
                "order_id": _col("BigInt", nullable=False),
                "product_id": _col("Int"),
                "quantity": _col("Int"),
            },
            pks=["item_id"],
            fks=[
                _fk(["order_id"], "orders", ["order_id"]),
                _fk(["product_id"], "products", ["product_id"]),
                _fk(["warehouse_id"], "warehouses", ["warehouse_id"]),  # unresolvable
            ],
        ),
        "products": _table(
            {
                "product_id": _col("Int", nullable=False, primary=True),
                "product_name": _col("Varchar", nullable=False),
                "category": _col("Varchar"),
            },
            pks=["product_id"],
        ),
        "shipments": _table(
            {
                "shipment_id": _col("Int", nullable=False, primary=True),
                "order_id": _col("BigInt"),
                "carrier": _col("Varchar"),
            },
            pks=["shipment_id"],
        ),
    }


def blog_tables() -> dict:
    """3 tables — below the default relational floor, same repo as the shop."""
    return {
        "users": _table(
            {
                "user_id": _col("Int", nullable=False, primary=True),
                "email": _col("Varchar", nullable=False),
            },
            pks=["user_id"],
        ),
        "posts": _table(
            {
                "post_id": _col("Int", nullable=False, primary=True),
                "user_id": _col("Int", nullable=False),
                "body": _col("Text"),
            },
            pks=["post_id"],
            fks=[_fk(["user_id"], "users", ["user_id"])],
        ),
        "comments": _table(
            {
                "comment_id": _col("Int", nullable=False, primary=True),
                "post_id": _col("Int", nullable=False),
                "note": _col("Text"),
            },
            pks=["comment_id"],
            fks=[_fk(["post_id"], "posts", ["post_id"])],
        ),
    }


def fragment_tables() -> dict:
    return {
        "logs": _table({"log_id": _col("Int", nullable=False, primary=True)}, pks=["log_id"]),
        "notes": _table({"note_id": _col("Int", nullable=False, primary=True)}, pks=["note_id"]),
    }


#: Record keys, ordered so the strongest record also sorts first.
KEY_SHOP = "000001_001_create_shop.sql"
KEY_BLOG = "000002_002_create_blog.sql"
KEY_FORK = "000003_shop_copy.sql"
KEY_FRAG = "000004_fragment.sql"
KEY_PRIV = "000005_private.sql"


def corpus() -> dict:
    return {
        KEY_SHOP: {
            "INFO": {"URL": REPO_A.format("001_create_shop.sql"), "LICENSE": "MIT", "PERMISSIVE": True},
            "TABLES": shop_tables(),
        },
        KEY_BLOG: {
            "INFO": {"URL": REPO_A.format("002_create_blog.sql"), "LICENSE": "MIT", "PERMISSIVE": True},
            "TABLES": blog_tables(),
        },
        KEY_FORK: {
            # Different repository, byte-identical schema: a vendored copy.
            "INFO": {"URL": REPO_B.format("shop_copy.sql"), "LICENSE": "APACHE-2.0", "PERMISSIVE": True},
            "TABLES": shop_tables(),
        },
        KEY_FRAG: {
            "INFO": {"URL": REPO_C, "LICENSE": "BSD-3-CLAUSE", "PERMISSIVE": True},
            "TABLES": fragment_tables(),
        },
        KEY_PRIV: {
            "INFO": {"URL": REPO_D, "LICENSE": "GPL-3.0", "PERMISSIVE": False},
            "TABLES": shop_tables(),
        },
    }


def write_corpus(directory: Path, payload: dict | None = None) -> Path:
    path = Path(directory) / sp.SOURCE_FILENAME
    path.write_text(json.dumps(payload if payload is not None else corpus()), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Streaming reader
# ---------------------------------------------------------------------------

class StreamingTest(unittest.TestCase):
    def test_streams_every_record_at_tiny_chunk_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_corpus(Path(tmp))
            expected = corpus()
            for chunk in (7, 64, 4096):
                streamed = dict(sp.stream_records(path, chunk_size=chunk))
                self.assertEqual(sorted(streamed), sorted(expected), f"chunk={chunk}")
                self.assertEqual(streamed[KEY_SHOP], expected[KEY_SHOP], f"chunk={chunk}")

    def test_pretty_printed_source_streams_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pretty.json"
            path.write_text(json.dumps(corpus(), indent=2), encoding="utf-8")
            self.assertEqual(
                sorted(k for k, _ in sp.stream_records(path, chunk_size=13)),
                sorted(corpus()),
            )

    def test_find_record_and_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_corpus(Path(tmp))
            self.assertIn("TABLES", sp.find_record(path, KEY_FORK))
            with self.assertRaises(KeyError):
                sp.find_record(path, "nope.sql")

    def test_truncated_corpus_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cut.json"
            text = json.dumps(corpus())
            path.write_text(text[: len(text) // 2], encoding="utf-8")
            with self.assertRaises(sp.SchemaPileFormatError):
                list(sp.stream_records(path))

    def test_non_object_corpus_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "arr.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(sp.SchemaPileFormatError):
                list(sp.stream_records(path))


# ---------------------------------------------------------------------------
# Normalization, types, fingerprints
# ---------------------------------------------------------------------------

class NormalizationTest(unittest.TestCase):
    def test_canonical_type_covers_the_real_spellings(self):
        cases = {
            "Varchar": ColumnType.TEXT,
            "VARCHAR2(255)": ColumnType.TEXT,
            "Int": ColumnType.INTEGER,
            "int4": ColumnType.INTEGER,
            "UnsignedTinyInt": ColumnType.INTEGER,
            "BigInt": ColumnType.BIGINT,
            "int8": ColumnType.BIGINT,
            "BIGSERIAL": ColumnType.BIGINT,
            "SERIAL": ColumnType.INTEGER,
            "Timestamp": ColumnType.TIMESTAMP,
            "TIME_STAMP_DFL": ColumnType.TIMESTAMP,
            "Datetime": ColumnType.TIMESTAMP,
            "Date": ColumnType.DATE,
            "Boolean": ColumnType.BOOLEAN,
            "BOOLEAN_CHAR": ColumnType.BOOLEAN,
            "bit": ColumnType.BOOLEAN,
            "DoublePrecision": ColumnType.FLOAT,
            "NUMBER": ColumnType.DECIMAL,
            "Decimal(10,2)": ColumnType.DECIMAL,
            "JSON": ColumnType.JSON,
            "TECH_ID": ColumnType.TEXT,
            "": ColumnType.TEXT,
            None: ColumnType.TEXT,
            # Longer spellings that CONTAIN a shorter rule's token. Before the
            # ordering fix 'enum' matched the 'num' rule (-> DECIMAL) and
            # 'interval'/'point' matched 'int' (-> INTEGER): 6,139 SchemaPile
            # Enum columns became numeric measures ("Sum of gender").
            "Enum": ColumnType.TEXT,
            "ENUM('a','b')": ColumnType.TEXT,
            "GenderEnum": ColumnType.TEXT,
            "Interval": ColumnType.TEXT,
            "INTERVAL DAY TO SECOND": ColumnType.TEXT,
            "Point": ColumnType.TEXT,
            "MultiPoint": ColumnType.TEXT,
            "TimeDelta": ColumnType.TEXT,
            "DataStoreServiceReportingPluginType": ColumnType.TEXT,
            # Non-regressions around the new leading rules.
            "TIMESTAMP WITH TIME ZONE": ColumnType.TIMESTAMP,
            "Numeric(10,2)": ColumnType.DECIMAL,
            "SmallInt": ColumnType.INTEGER,
            "int4range": ColumnType.INTEGER,
            "set('a','b')": ColumnType.TEXT,
        }
        for raw, expected in cases.items():
            self.assertEqual(sp.canonical_type(raw), expected, raw)

    def test_type_rules_longer_tokens_precede_their_substrings(self):
        """FIRST MATCH WINS, so an earlier token contained in a later,
        differently-typed token makes the later rule unreachable. This is
        exactly how 'int' shadowed 'interval'/'point' and 'num' shadowed
        'enum'; the invariant is now pinned for every future edit."""
        rules = sp._TYPE_RULES
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                earlier, later = rules[i], rules[j]
                self.assertFalse(
                    earlier[0] in later[0] and earlier[1] != later[1],
                    f"rule {later!r} can never match: {earlier!r} precedes it "
                    "and is a substring of it with a different type",
                )

    def test_enum_column_is_text_and_keeps_its_check_domain(self):
        """A bare 'Enum' TYPE (SchemaPile strips the values) is TEXT, so the
        table CHECK domain attaches to it — and it is never a numeric measure."""
        record = {
            "INFO": {"URL": REPO_A.format("enum.sql"), "LICENSE": "MIT", "PERMISSIVE": True},
            "TABLES": {
                "tickets": _table(
                    {
                        "ticket_id": _col("Int", nullable=False, primary=True),
                        "queue_id": _col("Int", nullable=False),
                        "status": _col("Enum", nullable=False),
                    },
                    pks=["ticket_id"],
                    fks=[_fk(["queue_id"], "queues", ["queue_id"])],
                    checks=["status IN ('open', 'closed')"],
                ),
                "queues": _table(
                    {
                        "queue_id": _col("Int", nullable=False, primary=True),
                        "queue_name": _col("Varchar", nullable=False),
                    },
                    pks=["queue_id"],
                ),
            },
        }
        tables, rels = sp.record_to_tables(record, key="enum.sql")
        tickets = next(t for t in tables if t.name == "tickets")
        status = tickets.column("status")
        self.assertIs(status.type, ColumnType.TEXT)
        self.assertEqual(status.enum_values, ("open", "closed"))
        # A bridge whose only non-key, non-FK column is the Enum has NO measure.
        self.assertIsNone(sp._measure_column(tickets, rels))

    def test_origin_repo_from_url(self):
        self.assertEqual(
            sp.origin_repo(
                "https://github.com/alaindet/garnet/blob/7d26a/database/x.sql?raw=true\n"
            ),
            "github.com/alaindet/garnet",
        )
        self.assertEqual(sp.origin_repo("https://www.gitlab.com/a/b/c"), "gitlab.com/a/b")

    def test_urlless_records_get_their_own_repo(self):
        a = sp.origin_repo("", key="one.sql")
        b = sp.origin_repo("", key="two.sql")
        self.assertTrue(a.startswith("unknown/"))
        self.assertNotEqual(a, b)  # never one shared 'unknown' bucket

    def test_shape_fingerprint_normalizes_order_case_and_dialect(self):
        base = shop_tables()
        variant = {
            name.upper(): dict(
                table,
                COLUMNS={
                    col.upper(): (
                        dict(spec, TYPE={"Int": "INTEGER", "BigInt": "int8"}.get(spec["TYPE"], spec["TYPE"]))
                    )
                    for col, spec in reversed(list(table["COLUMNS"].items()))
                },
            )
            for name, table in reversed(list(base.items()))
        }
        self.assertEqual(sp.shape_fingerprint(base), sp.shape_fingerprint(variant))

    def test_shape_fingerprint_separates_different_schemas(self):
        self.assertNotEqual(
            sp.shape_fingerprint(shop_tables()), sp.shape_fingerprint(blog_tables())
        )
        changed = shop_tables()
        changed["customers"]["COLUMNS"]["loyalty_tier"] = _col("Varchar")
        self.assertNotEqual(sp.shape_fingerprint(shop_tables()), sp.shape_fingerprint(changed))


# ---------------------------------------------------------------------------
# Clustering (independence)
# ---------------------------------------------------------------------------

class ClusteringTest(unittest.TestCase):
    def _index(self, tmp: str) -> sp.SchemaPileIndex:
        return spi.build_index(write_corpus(Path(tmp)))

    def test_same_repo_records_share_one_cluster(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            by_key = {r.key: r for r in index.records}
            self.assertEqual(by_key[KEY_SHOP].cluster, by_key[KEY_BLOG].cluster)
            self.assertNotEqual(by_key[KEY_SHOP].repo, by_key[KEY_FRAG].repo)
            self.assertNotEqual(by_key[KEY_SHOP].cluster, by_key[KEY_FRAG].cluster)

    def test_same_shape_across_repos_is_fused(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            by_key = {r.key: r for r in index.records}
            self.assertNotEqual(by_key[KEY_SHOP].repo, by_key[KEY_FORK].repo)
            self.assertEqual(by_key[KEY_SHOP].shape, by_key[KEY_FORK].shape)
            self.assertEqual(by_key[KEY_SHOP].cluster, by_key[KEY_FORK].cluster)
            cluster = index.cluster(by_key[KEY_SHOP].cluster)
            self.assertEqual(
                set(cluster.members), {KEY_SHOP, KEY_BLOG, KEY_FORK, KEY_PRIV}
            )
            self.assertEqual(len(cluster.repos), 3)
            self.assertTrue(cluster.family_id.startswith("schemapile__"))

    def test_representative_is_the_strongest_ingestible_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            cluster = index.cluster(index.record(KEY_SHOP).cluster)
            # KEY_BLOG fails the floor; KEY_PRIV is not permissive; the two
            # equal-strength shop records tie-break on key.
            self.assertEqual(cluster.representative, KEY_SHOP)
            self.assertEqual(list(cluster.candidates), [KEY_SHOP, KEY_FORK])
            self.assertEqual(index.cluster(index.record(KEY_FRAG).cluster).representative, "")

    def test_cluster_ids_are_deterministic_and_order_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_corpus(Path(tmp))
            first = spi.build_index(path)
            shuffled = dict(reversed(list(corpus().items())))
            other = Path(tmp) / "reordered.json"
            other.write_text(json.dumps(shuffled), encoding="utf-8")
            second = spi.build_index(other)
            self.assertEqual(
                [(r.key, r.cluster) for r in first.records],
                [(r.key, r.cluster) for r in second.records],
            )

    def test_index_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            out = sp.write_index(index, Path(tmp) / "index.json")
            reloaded = sp.load_index(out)
            self.assertEqual(reloaded.model_dump(), index.model_dump())
            self.assertEqual(reloaded.stats["records"], 5)
            self.assertEqual(reloaded.stats["non_permissive"], 1)
            self.assertEqual(reloaded.stats["records_passing_filter"], 2)
            self.assertEqual(reloaded.licenses["MIT"], 2)

    def test_missing_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                sp.load_index(Path(tmp) / "absent.json")


# ---------------------------------------------------------------------------
# Licensing gate
# ---------------------------------------------------------------------------

class LicenseGateTest(unittest.TestCase):
    def test_non_permissive_record_is_refused(self):
        record = corpus()[KEY_PRIV]
        with self.assertRaises(sp.NonPermissiveRecordError):
            sp.assert_usable_license(record, KEY_PRIV)
        with self.assertRaises(sp.NonPermissiveRecordError):
            sp.to_task_ir(record, key=KEY_PRIV, cluster="anything")

    def test_missing_license_is_refused_even_when_permissive(self):
        record = corpus()[KEY_SHOP]
        record["INFO"]["LICENSE"] = None
        with self.assertRaises(sp.NonPermissiveRecordError):
            sp.to_task_ir(record, key=KEY_SHOP, cluster="anything")

    def test_non_permissive_record_is_never_a_cluster_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = spi.build_index(write_corpus(Path(tmp)))
            cluster = index.cluster(index.record(KEY_PRIV).cluster)
            self.assertIn(KEY_PRIV, cluster.members)
            self.assertNotIn(KEY_PRIV, cluster.candidates)
            self.assertNotEqual(cluster.representative, KEY_PRIV)


# ---------------------------------------------------------------------------
# Relational filter
# ---------------------------------------------------------------------------

class RelationalFilterTest(unittest.TestCase):
    def test_defaults_are_the_documented_policy(self):
        f = sp.DEFAULT_FILTER
        self.assertEqual(
            (f.min_tables, f.max_tables, f.min_columns, f.min_foreign_keys,
             f.min_linked_table_pairs, f.min_tables_with_pk),
            (4, 40, 12, 3, 2, 2),
        )

    def test_fragment_fails_with_named_reasons(self):
        problems = sp.filter_problems(sp.record_metrics(corpus()[KEY_FRAG]))
        self.assertTrue(problems)
        joined = " ".join(problems)
        self.assertIn("min_tables", joined)
        self.assertIn("min_foreign_keys", joined)

    def test_shop_passes_and_counts_only_resolvable_foreign_keys(self):
        metrics = sp.record_metrics(corpus()[KEY_SHOP])
        self.assertEqual(sp.filter_problems(metrics), ())
        self.assertEqual(metrics.tables, 5)
        # order_items declares 3 FKs but one points at an absent table.
        self.assertEqual(metrics.foreign_keys_declared, 4)
        self.assertEqual(metrics.foreign_keys, 3)
        self.assertEqual(metrics.tables_with_pk, 5)

    def test_filter_is_configurable(self):
        record = corpus()[KEY_BLOG]
        with self.assertRaises(sp.RelationalFilterError):
            sp.to_task_ir(record, key=KEY_BLOG, cluster="c_blog")
        relaxed = sp.RelationalFilter(
            min_tables=3, min_columns=6, min_foreign_keys=2, min_linked_table_pairs=2
        )
        task = sp.to_task_ir(record, key=KEY_BLOG, cluster="c_blog", filt=relaxed)
        self.assertEqual(len(task.tables), 3)

    def test_max_tables_rejects_a_corpus_sized_dump(self):
        record = corpus()[KEY_SHOP]
        strict = sp.RelationalFilter(max_tables=4)
        with self.assertRaises(sp.RelationalFilterError):
            sp.to_task_ir(record, key=KEY_SHOP, cluster="c", filt=strict)


# ---------------------------------------------------------------------------
# Record -> TaskIR
# ---------------------------------------------------------------------------

class TaskIRTest(unittest.TestCase):
    def _task(self):
        return sp.to_task_ir(corpus()[KEY_SHOP], key=KEY_SHOP, cluster="c_shop_abc123")

    def test_identity_license_and_attribution(self):
        task = self._task()
        self.assertEqual(task.origin, Origin.SCHEMAPILE)
        self.assertEqual(task.family_id, "schemapile__c_shop_abc123")
        self.assertEqual(task.cluster_id, task.family_id)
        self.assertTrue(task.task_id.startswith(task.family_id + "__"))
        self.assertEqual(task.license, "MIT")
        self.assertIn("github.com/example/shop", task.attribution)
        self.assertIn(KEY_SHOP, task.attribution)
        self.assertIn("MIT", task.attribution)

    def test_schema_types_keys_and_relationships(self):
        task = self._task()
        self.assertEqual(
            [t.name for t in task.tables],
            ["customers", "order_items", "orders", "products", "shipments"],
        )
        orders = task.table("orders")
        self.assertEqual(orders.primary_key, ("order_id",))
        self.assertFalse(orders.column("order_id").nullable)
        self.assertEqual(orders.column("order_id").type, ColumnType.BIGINT)
        self.assertEqual(orders.column("amount").type, ColumnType.DECIMAL)
        # NULLABLE: null means 'the DDL did not say' => nullable.
        self.assertTrue(orders.column("amount").nullable)
        self.assertEqual(task.table("customers").column("customer_name").description, "Legal name.")
        edges = {(r.child_table, r.parent_table) for r in task.relationships}
        self.assertEqual(
            edges, {("orders", "customers"), ("order_items", "orders"), ("order_items", "products")}
        )
        required = {r.child_table: r.required for r in task.relationships if r.parent_table == "customers"}
        self.assertTrue(required["orders"])  # NOT NULL FK => required link

    def test_check_constraint_becomes_an_enum_domain(self):
        task = self._task()
        self.assertEqual(task.table("orders").column("status").enum_values, ("open", "closed"))

    def test_nullable_timestamp_without_status_domain_emits_snapshot(self):
        record = json.loads(json.dumps(corpus()[KEY_SHOP]))
        order_columns = record["TABLES"]["orders"]["COLUMNS"]
        order_columns["status"]["CHECKS"] = []
        order_columns["created_at"] = _col("Timestamp", nullable=True)

        task = sp.to_task_ir(
            record,
            key=KEY_SHOP,
            cluster="c_nullable_snapshot",
        )
        self.assertTrue(task.table("orders").column("created_at").nullable)
        snapshot = next(mart for mart in task.marts if mart.name.endswith("_snapshot"))
        # Statusless: no status payload beside the latest row's own columns.
        # The mart names its columns after the chain, and this chain's LABEL
        # column is `orders.status` (its CHECK domain was removed above), so
        # the label column is spelled `latest_status` and is the only one.
        self.assertEqual(
            {
                "customer_id",
                "customer_name",
                "orders_count",
                "lifetime_amount",
                "latest_order_id",
                "latest_status",
                "latest_amount",
                "latest_amount_share",
            },
            {column.name for column in snapshot.columns},
        )
        extrema = next(op for op in snapshot.plan.ops if op.kind.value == "extrema")
        self.assertEqual(
            '"snapshot_at" DESC NULLS LAST, '
            '"snapshot_row_id" ASC NULLS LAST',
            extrema.details["order_by"],
        )

    def test_mart_has_a_left_join_grain_and_real_grouping(self):
        task = self._task()
        mart = task.marts[0]
        kinds = [op.kind.value for op in mart.plan.ops]
        # Shape selection is evidence-driven: this schema now funds the
        # one-hop status-cohort UNION before a two-hop roll-up.  Pin the
        # semantic floor, not a particular library shape or selection order.
        self.assertGreaterEqual(kinds.count("join"), 1)
        # The plan library types its GROUP BY by what the measures DO: a mart
        # whose measures filter inside the aggregate is a `filtered_aggregate`,
        # one that counts distinctly is a `distinct`. Asserting the bare
        # `aggregate` kind would re-introduce exactly the blindness the typed
        # kinds exist to remove, so assert the FAMILY.
        self.assertTrue(
            {"aggregate", "filtered_aggregate", "distinct"} & set(kinds),
            f"no group-by op in {kinds}",
        )
        self.assertIn("tie_break", kinds)
        self.assertTrue(all(op.join_type.value == "left" for op in mart.plan.ops if op.join_type))
        self.assertGreaterEqual(len(mart.columns), 3)
        self.assertTrue(set(mart.key_columns) <= {c.name for c in mart.columns})

    def test_populations_declare_the_synthetic_policy(self):
        task = self._task()
        self.assertEqual({p.name for p in task.populations}, set(PopulationName))
        for pop in task.populations:
            self.assertEqual(pop.conditions[0], sp.SYNTHETIC_POPULATION_POLICY)
        primary = task.population(PopulationName.PRIMARY)
        self.assertEqual(set(primary.scale), {t.name for t in task.tables})

    def test_scale_hint_delegates_fk_capacity_to_shared_policy(self):
        task = self._task()
        # Turn the existing two-link order_items table into an FK-only-key
        # specimen.  The old adapter-local 60/240 heuristic returned 240 here,
        # beyond the 60-row product parent pool's unique-key capacity.
        tables = tuple(
            table.model_copy(update={"primary_key": ("product_id",)})
            if table.name == "order_items"
            else table
            for table in task.tables
        )
        expected = populations_mod.schema_scale_hint(tables, task.relationships)
        self.assertEqual(sp._scale_hint(tables, task.relationships), expected)
        self.assertLess(expected["order_items"], 240)

    def test_every_table_gets_exactly_one_backend(self):
        task = self._task()
        self.assertEqual(
            sorted(b.table for b in task.backends), sorted(t.name for t in task.tables)
        )

    def test_ingest_is_deterministic(self):
        self.assertEqual(self._task().content_hash(), self._task().content_hash())

    def test_identifier_collisions_are_refused(self):
        record = {
            "INFO": {"URL": REPO_A.format("dup.sql"), "LICENSE": "MIT", "PERMISSIVE": True},
            "TABLES": {
                "My Table": _table({"a": _col("Int", primary=True)}, pks=["a"]),
                "my_table": _table({"b": _col("Int", primary=True)}, pks=["b"]),
            },
        }
        with self.assertRaises(ValueError):
            sp.record_to_tables(record, key="dup.sql")

    def test_task_from_index_resolves_cluster_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_corpus(Path(tmp))
            index = spi.build_index(source)
            cluster = index.record(KEY_SHOP).cluster
            by_cluster = sp.task_from_index(index, source=source, cluster=cluster)
            by_key = sp.task_from_index(index, source=source, key=KEY_SHOP)
            self.assertEqual(by_cluster.task_id, by_key.task_id)
            self.assertEqual(by_cluster.family_id, f"schemapile__{cluster}")
            # The vendored copy in the other repo shares the family: one
            # cluster, one family, whichever member is ingested.
            fork = sp.task_from_index(index, source=source, key=KEY_FORK)
            self.assertEqual(fork.family_id, by_key.family_id)
            self.assertNotEqual(fork.task_id, by_key.task_id)
            with self.assertRaises(ValueError):
                sp.task_from_index(index, source=source)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliTest(unittest.TestCase):
    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        """Run the CLI, capturing its stdout so the suite stays readable."""
        from elt_taskgen import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_list_then_ingest_registers_one_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_corpus(root)
            index_path = sp.write_index(spi.build_index(source), root / "index.json")
            workspace = root / "ws"
            cluster = sp.load_index(index_path).record(KEY_SHOP).cluster

            listed, output = self._run(
                [
                    "ingest-schemapile",
                    "--workspace", str(workspace),
                    "--index", str(index_path),
                    "--list",
                ]
            )
            self.assertEqual(listed, 0)
            self.assertIn(cluster, output)
            self.assertIn(KEY_SHOP, output)

            code, _ = self._run(
                [
                    "ingest-schemapile",
                    "--workspace", str(workspace),
                    "--index", str(index_path),
                    "--source", str(source),
                    "--cluster", cluster,
                ]
            )
            self.assertEqual(code, 0)
            registered = sorted(p.name for p in (workspace / "tasks").iterdir())
            self.assertEqual(len(registered), 1)
            self.assertTrue(registered[0].startswith(f"schemapile__{cluster}__"))

    def test_cluster_and_key_are_mutually_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_corpus(root)
            index_path = sp.write_index(spi.build_index(source), root / "index.json")
            code, _ = self._run(
                [
                    "ingest-schemapile",
                    "--workspace", str(root / "ws"),
                    "--index", str(index_path),
                    "--source", str(source),
                ]
            )
            self.assertEqual(code, 2)

    def test_contamination_refusal_uses_the_shared_exit_2_convention(self):
        """The exit code and the wording a wrapper matches on.

        `ingest-schemapile` used to refuse with exit 1 and its own message
        dialect, so `if [ $? -eq 2 ]` — the contamination test every other
        ingest path answers to — silently misread a refusal here. Both the code
        and the line shape are pinned (see INGEST EXIT-CODE CONTRACT in
        cli.py)."""
        from elt_taskgen.verification import contamination

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_corpus(root)
            index_path = sp.write_index(spi.build_index(source), root / "index.json")
            workspace = root / "ws"
            index = sp.load_index(index_path)
            cluster = index.record(KEY_SHOP).cluster
            task = sp.task_from_index(index, source=source, cluster=cluster)

            # Plant the candidate's own WHOLE-SCHEMA fingerprint on an anchor
            # deny list: family names never collide for this pool, so the
            # schema fingerprint is the realistic fatal case.
            schema_fp = next(
                fp
                for fp in contamination.schema_fingerprints(task.tables)
                if fp.startswith("schema:")
            )
            contamination.ContaminationIndex(
                workspace / "state" / "contamination"
            ).add_benchmark("eltbench", [schema_fp])

            code, output = self._run(
                [
                    "ingest-schemapile",
                    "--workspace", str(workspace),
                    "--index", str(index_path),
                    "--source", str(source),
                    "--cluster", cluster,
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("contamination FATAL [schema/eltbench]", output)
            self.assertIn(
                f"REFUSED: 1 fatal contamination collision(s) — {task.task_id} "
                "was NOT registered",
                output,
            )
            # Fail closed means fail closed: nothing entered the workspace.
            self.assertFalse((workspace / "tasks").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

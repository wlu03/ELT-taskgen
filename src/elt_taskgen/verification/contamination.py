"""Provide one fail-closed overlap detector for pre- and post-generation checks.

A missing index is a fatal collision. Armed anchors are type-blind, so only compatible
name-shape namespaces count as coverage, and every result records its coverage level.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Iterable

import sqlglot
from pydantic import BaseModel, ConfigDict

from elt_taskgen.models import Relationship, TableSpec, TaskIR, canonical_json, sha256_hex

# Embedded deny lists (family names as data)

#: The 100 ELT-Bench database families, verbatim from the pinned checkout.
ELTBENCH_FAMILIES: tuple[str, ...] = (
    "address", "airline", "amplitude", "app_store", "apple_store", "asana",
    "authors", "beer_factory", "bike_share_1", "book_publishing_company",
    "books", "california_schools", "car_retails", "card_games", "cars",
    "chicago_crime", "citeseer", "codebase_comments", "codebase_community",
    "coinmarketcap", "college_completion", "computer_student", "cookbook",
    "cs_semester", "debit_card_specializing", "disney", "donor",
    "european_football_1", "european_football_2", "facebook_ads", "financial",
    "food_inspection", "food_inspection_2", "formula_1", "genes", "github",
    "hockey", "human_resources", "ice_hockey_draft", "image_and_language",
    "instagram_business", "language_corpus", "law_episode", "legislator",
    "lever", "linkedin", "mailchimp", "marketo", "mental_health_survey",
    "menu", "microsoft_ads", "mondial_geo", "movie", "movie_3",
    "movie_platform", "movielens", "movies_4", "music_platform_2",
    "music_tracker", "olympics", "pardot", "pinterest",
    "professional_basketball", "public_review_platform", "qualtrics",
    "recurly", "regional_sales", "restaurant", "retail_complains",
    "retail_world", "retails", "sales", "sales_in_weather", "shakespeare",
    "shipping", "simpson_episodes", "soccer_2016", "social_media",
    "software_company", "student_club", "student_loan", "superhero",
    "superstore", "synthea", "talkingdata", "thrombosis_prediction",
    "tiktok_ads", "toxicology", "trains", "twilio", "twitter_organic",
    "university", "video_games", "workday", "works_cycles", "world",
    "world_development_indicators", "xero", "youtube_analytics", "zuora",
)

#: PARTIAL Spider2-DBT families; extend via add_benchmark() from a checkout.
SPIDER2_DBT_FAMILIES: tuple[str, ...] = (
    "google_ads", "hubspot", "jira", "shopify", "stripe", "zendesk", "salesforce",
    "netsuite", "quickbooks", "recharge", "klaviyo", "jaffle_shop",
    "ad_reporting", "app_reporting", "greenhouse", "iterable", "pendo",
)

#: PARTIAL ADE-Bench families; extend via add_benchmark() from a checkout.
ADE_BENCH_FAMILIES: tuple[str, ...] = (
    "jaffle_shop", "northwind", "tpch", "ecommerce_analytics",
)

_EMBEDDED_DENY_LISTS: dict[str, tuple[str, ...]] = {
    "eltbench": ELTBENCH_FAMILIES,
    "spider2_dbt": SPIDER2_DBT_FAMILIES,
    "ade_bench": ADE_BENCH_FAMILIES,
}

#: Fingerprint-namespace prefix -> Collision.kind (`shape*` reports as schema).
_PREFIX_TO_KIND: tuple[tuple[str, str], ...] = (
    ("family:", "family"),
    ("schema:", "schema"),
    ("schema-table:", "schema"),
    ("shape:", "schema"),
    ("shape-table:", "schema"),
    ("sql:", "sql"),
    ("deps:", "deps"),
    ("fixture:", "fixture"),
    ("data:", "data"),
    ("text:", "text"),
)

#: Exact matches here REJECT; every other prefix is borderline (audit queue),
#: because generic per-table shapes such as `region` legitimately recur.
_FATAL_PREFIXES: frozenset[str] = frozenset(
    {"family:", "schema:", "shape:", "sql:", "fixture:", "data:"}
)

_ADMITTED_STORE = "admitted"

#: Structural (not name) namespaces: a name-only index misses a renamed copy.
STRUCTURAL_PREFIXES: tuple[str, ...] = (
    "schema:", "schema-table:", "shape:", "shape-table:", "sql:", "deps:",
    "fixture:", "data:",
)

#: The namespaces that ARM the firewall: only these can collide between a
#: TEXT-typed anchor and a real-typed candidate.
ARMING_PREFIXES: tuple[str, ...] = ("shape:", "shape-table:")

#: Env var that turns an under-covered index from a WARNING into a refusal.
REQUIRE_FIREWALL_ENV = "ELT_TASKGEN_REQUIRE_FIREWALL"

#: Env var selecting how this layer BEHAVES. One read point governs every
#: refusal in the pipeline, so the operator flips one variable rather than
#: editing ~25 call sites across cli/gates/repair/adapters/tools.
ENFORCEMENT_ENV = "ELT_TASKGEN_CONTAMINATION"


class Enforcement(str, Enum):
    """Select contamination enforcement behavior.

    `ENFORCE` detects, rejects fatal overlaps, and queues borderline cases. `OBSERVE`
    records results without blocking or queuing. `OFF` skips detection and evidence
    entirely.
    """

    ENFORCE = "enforce"
    OBSERVE = "observe"
    OFF = "off"


def enforcement(environ: dict[str, str] | None = None) -> Enforcement:
    """Current enforcement mode; ENFORCE unless the env var says otherwise.

    Fail closed on an unrecognised value: a typo must not silently disarm the
    layer. Same default-safe shape as `required_coverage_from_env`.
    """
    import os

    raw = (
        (environ if environ is not None else os.environ)
        .get(ENFORCEMENT_ENV, "")
        .strip()
        .lower()
    )
    if raw in ("observe", "record", "warn", "audit"):
        return Enforcement.OBSERVE
    if raw in ("off", "0", "false", "no", "disabled", "none"):
        return Enforcement.OFF
    return Enforcement.ENFORCE


def enforcing(environ: dict[str, str] | None = None) -> bool:
    """Is the layer allowed to refuse or queue right now?"""
    return enforcement(environ) is Enforcement.ENFORCE


class Collision(BaseModel):
    """One detected overlap between a candidate/task and an indexed corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str            # 'family'|'schema'|'sql'|'fixture'|'deps'|'text'|'data'|'index'
    against: str         # corpus name: 'eltbench'|'spider2_dbt'|'ade_bench'|'admitted'|'index'
    detail: str
    fatal: bool


class CoverageLevel(str, Enum):
    """How much of a firewall the index actually is. Ordered, comparable."""

    UNARMED = "unarmed"
    NAME_ONLY = "name_only"
    ARMED = "armed"


#: Ranking used by `coverage_failure` (higher is more covered).
_COVERAGE_RANK: dict[CoverageLevel, int] = {
    CoverageLevel.UNARMED: 0,
    CoverageLevel.NAME_ONLY: 1,
    CoverageLevel.ARMED: 2,
}


class StoreCoverage(BaseModel):
    """What ONE persisted corpus store contributes to the index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    fingerprints: int
    by_kind: dict[str, int] = {}
    #: True when the store holds only the seeded family list: nothing measured in.
    embedded_only: bool = False


class IndexCoverage(BaseModel):
    """WHAT the contamination index can see, recorded beside every result.

    "0 collisions" is only as strong as the index it was checked against, and
    this record makes the two impossible to confuse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: CoverageLevel
    index_dir: str
    #: Benchmark corpora only — the firewall proper.
    stores: tuple[StoreCoverage, ...] = ()
    benchmark_fingerprints: int = 0
    benchmark_by_kind: dict[str, int] = {}
    #: Type-blind `shape*` fingerprints (ARMING_PREFIXES); 0 grades NAME_ONLY.
    shape_fingerprints: int = 0
    #: Our own output: coverage against SELF-duplication, so it never upgrades
    #: `level` — counting it makes a workspace that saw no anchor look protected.
    admitted_tasks: int = 0
    admitted_fingerprints: int = 0
    admitted_by_kind: dict[str, int] = {}

    @property
    def is_firewall(self) -> bool:
        return self.level is CoverageLevel.ARMED

    @property
    def structural_fingerprints(self) -> int:
        """Benchmark fingerprints that describe structure, not just a name."""
        return sum(
            n
            for namespace, n in self.benchmark_by_kind.items()
            if f"{namespace}:" in STRUCTURAL_PREFIXES
        )

    @property
    def typed_only_structural(self) -> bool:
        """Structural fingerprints present, none of them type-blind.

        The pre-`shape:` store format: every typed hash in it is inert against
        the TEXT-typed anchors, so the remedy is "re-run measure-target".
        """
        return self.structural_fingerprints > 0 and self.shape_fingerprints == 0

    def summary(self) -> str:
        """One line, loud when the index is not a firewall, with the remedy."""
        kinds = (
            ", ".join(
                f"{k}={self.benchmark_by_kind[k]}"
                for k in sorted(self.benchmark_by_kind)
            )
            or "none"
        )
        stores = (
            ", ".join(
                f"{s.name}({s.fingerprints}{'; seeded-only' if s.embedded_only else ''})"
                for s in self.stores
            )
            or "no benchmark store"
        )
        tail = (
            f"benchmark fingerprints={self.benchmark_fingerprints} "
            f"[{kinds}] (type-blind shape={self.shape_fingerprints}) across "
            f"{stores}; admitted corpus="
            f"{self.admitted_tasks} task(s)/{self.admitted_fingerprints} fingerprint(s)"
        )
        if self.level is CoverageLevel.ARMED:
            return f"contamination firewall ARMED: {tail}"
        if self.level is CoverageLevel.UNARMED:
            return (
                "CONTAMINATION FIREWALL UNARMED: no persisted store at "
                f"{self.index_dir} — every check fails closed. {tail}. "
                f"{ARMING_REMEDY}"
            )
        if self.typed_only_structural:
            return (
                "CONTAMINATION FIREWALL NOT ARMED (typed-only index): the "
                "index holds only TYPED schema fingerprints (pre-shape "
                "format) and no type-blind shape: fingerprints — the anchors "
                "are TEXT-typed, so a typed hash never collides with a "
                "real-typed copy of a benchmark schema; re-run measure-target "
                f"to upgrade the store in place. {tail}. {ARMING_REMEDY}"
            )
        return (
            "CONTAMINATION FIREWALL NOT ARMED (name-only index): the index "
            "holds benchmark FAMILY NAMES and no structural fingerprints, so "
            "a clean result means only 'this task's name is not on the list' "
            "— a renamed copy of a benchmark schema passes it. "
            f"{tail}. {ARMING_REMEDY}"
        )


#: `deps:` is deliberately not claimed: the pinned anchors carry no FK metadata.
ARMING_REMEDY = (
    "Arm it with: elt-taskgen measure-target --workspace <ws> --bench-root "
    "<pinned ELT-Bench checkout> (loads the 100 anchors' type-blind schema "
    "shape fingerprints and the evaluation SQL fingerprints), and extend the "
    "other pools with add_benchmark() from their pinned checkouts."
)


def coverage_failure(
    coverage: IndexCoverage, minimum: CoverageLevel = CoverageLevel.ARMED
) -> Collision | None:
    """The fatal Collision for an index below `minimum`, or None.

    An ordinary Collision so a refusal takes the same fail-closed path as a real
    overlap; a second rejection mechanism would drift from this one.
    """
    if not enforcing():
        return None
    if _COVERAGE_RANK[coverage.level] >= _COVERAGE_RANK[minimum]:
        return None
    return Collision(
        kind="index",
        against="index",
        detail=(
            f"contamination coverage {coverage.level.value!r} is below the "
            f"required {minimum.value!r}: {coverage.summary()}"
        ),
        fatal=True,
    )


def required_coverage_from_env(environ: dict[str, str] | None = None) -> CoverageLevel | None:
    """`ELT_TASKGEN_REQUIRE_FIREWALL` as a minimum level, or None (warn only).

    Default-off is deliberate: the offline demo and the test suite have no
    benchmark checkout, so safety comes from recording coverage on every check
    instead; a corpus-producing operator sets the variable to make it refuse.
    """
    import os

    raw = (environ if environ is not None else os.environ).get(
        REQUIRE_FIREWALL_ENV, ""
    ).strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return None
    if raw in ("name", "name_only"):
        return CoverageLevel.NAME_ONLY
    return CoverageLevel.ARMED


class ContaminationResult(BaseModel):
    """One scan: the collisions AND the coverage they were measured against.

    Nothing that RECORDS a result may use the collisions-only
    `check_pre`/`check_post`: a result without its coverage is the defect this
    class exists to close.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_point: str
    coverage: IndexCoverage
    collisions: tuple[Collision, ...] = ()

    @property
    def fatal_count(self) -> int:
        return sum(1 for c in self.collisions if c.fatal)

    def detail(self) -> str:
        return (
            f"{len(self.collisions)} collision(s), {self.fatal_count} fatal; "
            f"{self.coverage.summary()}"
        )


# Normalization + fingerprint computation (pure functions)

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to '_', strip edges."""
    return _NORM_RE.sub("_", name.strip().lower()).strip("_")


def normalize_sql(sql: str) -> str:
    """Canonical form of a SQL text for hashing.

    sqlglot re-render with identifier normalization, so whitespace and case
    cannot hide a copy; regex fallback when the SQL does not parse.
    """
    try:
        parsed = sqlglot.parse(sql, read="duckdb")
        rendered = " ; ".join(
            e.sql(dialect="duckdb", normalize=True) for e in parsed if e is not None
        )
        if rendered.strip():
            return rendered.strip().lower()
    except Exception:
        pass
    text = re.sub(r"--[^\n]*", " ", sql)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip().lower()


def sql_fingerprint(sql: str) -> str:
    return "sql:" + sha256_hex(normalize_sql(sql))


def _table_material(table: TableSpec) -> str:
    """TYPED per-table material: normalized name + sorted `column:type` pairs."""
    cols = sorted(f"{normalize_name(c.name)}:{c.type.value}" for c in table.columns)
    return f"{normalize_name(table.name)}|{','.join(cols)}"


def _shape_material(table: TableSpec) -> str:
    """TYPE-BLIND per-table material: normalized table name + sorted column NAMES.

    No types or nullability: exactly what a TEXT-typed anchor and a real-typed
    candidate still share when one is a copy of the other.
    """
    cols = sorted(normalize_name(c.name) for c in table.columns)
    return f"{normalize_name(table.name)}|{','.join(cols)}"


def schema_fingerprints(tables: Iterable[TableSpec]) -> set[str]:
    """Whole-schema + per-table hashes in BOTH the typed and type-blind namespaces.

    Only `shape:`/`shape-table:` can collide with an ELT-Bench anchor; the typed
    pair is kept as a harmless superset for admitted-corpus self-dedup.
    """
    tables = tuple(tables)
    materials = sorted(_table_material(t) for t in tables)
    out = {"schema:" + sha256_hex("\n".join(materials))}
    out.update("schema-table:" + sha256_hex(m) for m in materials)
    shapes = sorted(_shape_material(t) for t in tables)
    out.add("shape:" + sha256_hex("\n".join(shapes)))
    out.update("shape-table:" + sha256_hex(m) for m in shapes)
    return out


def deps_fingerprint(relationships: Iterable[Relationship]) -> str:
    edges = sorted(
        "{}({})->{}({}):{}".format(
            normalize_name(r.child_table),
            ",".join(normalize_name(c) for c in r.child_columns),
            normalize_name(r.parent_table),
            ",".join(normalize_name(c) for c in r.parent_columns),
            "req" if r.required else "opt",
        )
        for r in relationships
    )
    return "deps:" + sha256_hex("\n".join(edges))


def family_aliases(task: TaskIR) -> set[str]:
    """Normalized alias names of a task family (pool-qualified AND base)."""
    aliases: set[str] = set()
    for raw in (task.family_id, task.task_id):
        norm = normalize_name(raw)
        if norm:
            aliases.add(norm)
        if "__" in raw:
            base = normalize_name(raw.split("__", 1)[1])
            if base:
                aliases.add(base)
    title = normalize_name(task.title)
    if title:
        aliases.add(title)
    return aliases


def text_fingerprints(text: str, *, min_len: int = 30) -> set[str]:
    """Hashes of normalized lines/sentences long enough to be distinctive."""
    out: set[str] = set()
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if not normalized:
        return out
    for part in re.split(r"[.\n;]+", normalized):
        part = part.strip()
        if len(part) >= min_len:
            out.add("text:" + sha256_hex(part))
    return out


def sha256_hex_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_fingerprint(path: Path, *, kind: str = "fixture") -> str:
    return f"{kind}:" + sha256_hex_bytes(path.read_bytes())


def task_fingerprints(task: TaskIR) -> set[str]:
    """Pre-generation fingerprint set of a TaskIR (candidate OR benchmark anchor)."""
    fps: set[str] = {f"family:{a}" for a in family_aliases(task)}
    fps.update(schema_fingerprints(task.tables))
    if task.relationships:
        fps.add(deps_fingerprint(task.relationships))
    if task.reference is not None:
        for sql in task.reference.sql_by_mart.values():
            fps.add(sql_fingerprint(sql))
    fps.update(text_fingerprints(task.solver_prompt))
    return fps


def _kind_for(fingerprint: str) -> str:
    for prefix, kind in _PREFIX_TO_KIND:
        if fingerprint.startswith(prefix):
            return kind
    return "text"


def _namespace_for(fingerprint: str) -> str:
    """The fingerprint's own namespace ('schema-table', not the 'schema' kind).

    Coverage reports namespaces because `schema:` and `schema-table:` answer
    different questions about how much of a benchmark was measured in.
    """
    for prefix, _ in _PREFIX_TO_KIND:
        if fingerprint.startswith(prefix):
            return prefix[:-1]
    return "text"


def _count_by_namespace(fingerprints: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fp in fingerprints:
        namespace = _namespace_for(fp)
        counts[namespace] = counts.get(namespace, 0) + 1
    return dict(sorted(counts.items()))


def _is_fatal(fingerprint: str) -> bool:
    return any(fingerprint.startswith(p) for p in _FATAL_PREFIXES)


# The index

class ContaminationIndex:
    """ONE persisted fingerprint index; `check_pre` and `check_post` both use it.

    ARMED-BUT-EMPTY IS FAILURE: with no persisted store every check returns a
    fatal Collision(kind='index'). The embedded deny lists never count as armed
    on their own — the caller must have built the index deliberately.
    """

    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)

    # Persistence

    def _store_path(self, name: str) -> Path:
        safe = normalize_name(name)
        if not safe:
            raise ValueError(f"invalid corpus name {name!r}")
        return self.index_dir / f"{safe}.json"

    def _read_store(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:  # fail closed
            raise RuntimeError(f"corrupt contamination store {path}: {e}") from e

    def _write_store(self, path: Path, payload: dict) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(payload), encoding="utf-8")

    def add_benchmark(self, name: str, fingerprints: Iterable[str]) -> None:
        """Merge fingerprint strings into the persisted store for `name`."""
        path = self._store_path(name)
        existing: set[str] = set()
        if path.exists():
            existing.update(self._read_store(path).get("fingerprints", []))
        existing.update(fingerprints)
        self._write_store(
            path, {"name": normalize_name(name), "fingerprints": sorted(existing)}
        )

    def add_admitted_task(self, task: TaskIR) -> None:
        """Fingerprint an accepted task into the admitted corpus (AdmittedIndex)."""
        path = self._store_path(_ADMITTED_STORE)
        payload = {"tasks": {}}
        if path.exists():
            payload = self._read_store(path)
            payload.setdefault("tasks", {})
        payload["tasks"][task.task_id] = {
            "family_id": task.family_id,
            "fingerprints": sorted(task_fingerprints(task)),
        }
        self._write_store(path, payload)

    # Loading

    def _persisted_stores(self) -> list[Path]:
        if not self.index_dir.is_dir():
            return []
        return sorted(p for p in self.index_dir.glob("*.json") if p.is_file())

    def is_armed(self) -> bool:
        """Does ANY persisted store exist? A BOOTSTRAP check, not a firewall check.

        The CLI seeds the deny lists on first use, so this is true almost
        everywhere and `coverage()` answers the question that matters; it
        survives because a store-less index must still fail closed first.
        """
        return bool(self._persisted_stores())

    def coverage(self) -> IndexCoverage:
        """WHICH fingerprint kinds this index holds, and how many. Pure.

        ARMED needs a type-blind whole-schema `shape:` fingerprint in a BENCHMARK
        store; typed hashes are inert against TEXT-typed anchors, and the
        admitted corpus is self-dedup coverage only, so it never upgrades a grade.
        """
        embedded = {
            name: {f"family:{normalize_name(f)}" for f in fams}
            for name, fams in _EMBEDDED_DENY_LISTS.items()
        }
        stores: list[StoreCoverage] = []
        benchmark_all: list[str] = []
        for path in self._persisted_stores():
            name = path.stem
            if name == _ADMITTED_STORE:
                continue
            fps = sorted(set(self._read_store(path).get("fingerprints", [])))
            benchmark_all.extend(fps)
            stores.append(
                StoreCoverage(
                    name=name,
                    fingerprints=len(fps),
                    by_kind=_count_by_namespace(fps),
                    embedded_only=bool(fps) and set(fps) <= embedded.get(name, set()),
                )
            )

        admitted = self._admitted_tasks()
        admitted_fps: list[str] = []
        for entry in admitted.values():
            admitted_fps.extend(entry.get("fingerprints", []))

        by_kind = _count_by_namespace(benchmark_all)
        shape_count = sum(
            n for ns, n in by_kind.items() if f"{ns}:" in ARMING_PREFIXES
        )
        # ARMED needs the FATAL type-blind namespace: typed `schema:` never meets
        # a TEXT-typed anchor, and `shape-table:` alone can audit but not reject.
        if not self._persisted_stores():
            level = CoverageLevel.UNARMED
        elif by_kind.get("shape", 0) > 0:
            level = CoverageLevel.ARMED
        else:
            level = CoverageLevel.NAME_ONLY

        return IndexCoverage(
            level=level,
            index_dir=str(self.index_dir),
            stores=tuple(stores),
            benchmark_fingerprints=len(benchmark_all),
            benchmark_by_kind=by_kind,
            shape_fingerprints=shape_count,
            admitted_tasks=len(admitted),
            admitted_fingerprints=len(admitted_fps),
            admitted_by_kind=_count_by_namespace(admitted_fps),
        )

    def _benchmark_corpora(self) -> dict[str, set[str]]:
        """Merged embedded + persisted fingerprints per benchmark corpus."""
        corpora: dict[str, set[str]] = {
            name: {f"family:{normalize_name(f)}" for f in fams}
            for name, fams in _EMBEDDED_DENY_LISTS.items()
        }
        for path in self._persisted_stores():
            name = path.stem
            if name == _ADMITTED_STORE:
                continue
            corpora.setdefault(name, set()).update(
                self._read_store(path).get("fingerprints", [])
            )
        return corpora

    def _admitted_tasks(self) -> dict[str, dict]:
        path = self.index_dir / f"{_ADMITTED_STORE}.json"
        if not path.exists():
            return {}
        return dict(self._read_store(path).get("tasks", {}))

    # Checking

    def _armed_failure(self) -> Collision:
        # Observed/off: an unbuilt index is a fact to record, not a refusal.
        return Collision(
            kind="index",
            against="index",
            detail=(
                f"contamination index at {self.index_dir} holds no persisted "
                "store — armed-but-empty is a FAILURE, not a clean pass; build "
                "the index (anchors/benchmarks/admitted) before checking"
            ),
            fatal=enforcing(),
        )

    def _collide(self, fingerprints: set[str], task: TaskIR) -> list[Collision]:
        mode = enforcement()
        if mode is Enforcement.OFF:
            return []
        fatal_ok = mode is Enforcement.ENFORCE
        collisions: list[Collision] = []
        for corpus, indexed in sorted(self._benchmark_corpora().items()):
            for fp in sorted(fingerprints & indexed):
                collisions.append(
                    Collision(
                        kind=_kind_for(fp),
                        against=corpus,
                        detail=f"fingerprint {fp} present in corpus {corpus!r}",
                        fatal=fatal_ok and _is_fatal(fp),
                    )
                )
        for task_id, entry in sorted(self._admitted_tasks().items()):
            if task_id == task.task_id or entry.get("family_id") == task.family_id:
                continue  # a task never collides with itself / its own family
            for fp in sorted(fingerprints & set(entry.get("fingerprints", []))):
                collisions.append(
                    Collision(
                        kind=_kind_for(fp),
                        against="admitted",
                        detail=(
                            f"fingerprint {fp} matches previously admitted "
                            f"task {task_id!r}"
                        ),
                        fatal=fatal_ok and _is_fatal(fp),
                    )
                )
        return collisions

    def check_pre(self, task: TaskIR) -> list[Collision]:
        """Pre-generation call point: candidate fingerprints vs the index."""
        collisions: list[Collision] = []
        if not self.is_armed():
            collisions.append(self._armed_failure())
        collisions.extend(self._collide(task_fingerprints(task), task))
        return collisions

    def check_post(
        self, task: TaskIR, task_dir: Path, answer_key_dir: Path
    ) -> list[Collision]:
        """Post-generation call point: EMITTED artifacts vs the same index.

        Adds fixture, gold-data, reference-SQL and task-text hashes to the pre
        fingerprints. A missing artifact tree is a fatal failure, never a pass.
        """
        task_dir = Path(task_dir)
        answer_key_dir = Path(answer_key_dir)
        collisions: list[Collision] = []
        if not self.is_armed():
            collisions.append(self._armed_failure())
        for label, d in (("task_dir", task_dir), ("answer_key_dir", answer_key_dir)):
            if not d.is_dir():
                collisions.append(
                    Collision(
                        kind="index",
                        against="index",
                        detail=f"post-check {label} {d} is missing — fail closed",
                        fatal=True,
                    )
                )
        if any(c.kind == "index" and "missing" in c.detail for c in collisions):
            return collisions

        fps = task_fingerprints(task)
        fps.update(text_fingerprints(task.title))

        for path in sorted(task_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(task_dir).as_posix()
            if rel.startswith("sources/"):
                fps.add(file_fingerprint(path, kind="fixture"))
            elif path.suffix in {".yaml", ".yml", ".csv", ".md", ".txt", ".json"}:
                fps.update(text_fingerprints(path.read_text(encoding="utf-8", errors="replace")))

        for path in sorted(answer_key_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(answer_key_dir).as_posix()
            if rel.startswith("gold/"):
                fps.add(file_fingerprint(path, kind="data"))
            elif path.suffix == ".sql":
                fps.add(sql_fingerprint(path.read_text(encoding="utf-8", errors="replace")))

        collisions.extend(self._collide(fps, task))
        return collisions

    # Scanning: collisions with the coverage they were measured against

    def scan_pre(
        self, task: TaskIR, *, require: CoverageLevel | None = None
    ) -> ContaminationResult:
        """`check_pre` plus the coverage record. Prefer this when RECORDING.

        `require` turns an under-covered index into a fatal collision instead of
        a warning; callers take the level from `required_coverage_from_env()`.
        """
        return self._scan("pre", task, self.check_pre(task), require)

    def scan_post(
        self,
        task: TaskIR,
        task_dir: Path,
        answer_key_dir: Path,
        *,
        require: CoverageLevel | None = None,
    ) -> ContaminationResult:
        """`check_post` plus the coverage record. Prefer this when RECORDING."""
        return self._scan(
            "post", task, self.check_post(task, task_dir, answer_key_dir), require
        )

    def _scan(
        self,
        call_point: str,
        task: TaskIR,
        collisions: list[Collision],
        require: CoverageLevel | None,
    ) -> ContaminationResult:
        cov = self.coverage()
        if require is not None:
            failure = coverage_failure(cov, require)
            if failure is not None:
                collisions = [failure, *collisions]
        return ContaminationResult(
            call_point=call_point, coverage=cov, collisions=tuple(collisions)
        )

# ELT-taskgen — module interface contracts

This document defines the module interfaces. Shared types referenced by these
signatures live in `elt_taskgen.models` (`models.py`), which builders must not
change. Raise any apparent interface conflict with the architect. Each module
defines its own intermediate types that are not in `models.py` (for example,
`CandidateSpec`, `RunResult`, and `GoldBundle`) as Pydantic models with
`frozen=True` where appropriate.

**Execution terminology.** This file documents code as implemented. The
architecture authority is [`EXECUTION_MODEL.md`](EXECUTION_MODEL.md): DuckDB is
the private oracle and high-volume semantic RLVR runtime; Snowflake, Databricks,
and Redshift are sparse real-runtime certification targets. A function named
`runtime` or a successful mock test does not establish cloud certification. The
combined-task DuckDB scorer described by the target architecture is still an
implementation gap where explicitly noted below.

Conventions binding on every module:

* `Path` means `pathlib.Path`; all paths absolute.
* Determinism: no wall-clock in any generated artifact; all RNG seeded via
  `models.derive_seed`; stable sort orders; canonical JSON/YAML dumps
  (`models.canonical_json`, and `yaml.safe_dump(..., sort_keys=True)`).
* Fail closed: missing artifact/evidence => raise or return a failed
  `GateResult`; never a silent pass.
* Every module starts with a docstring that explains its purpose.
* Ports from `elt-training-data/curation/scripts/` note provenance in the
  docstring of the ported function.

**Counts in this document are derived from code.** The pipeline has
**14** ledger stages (`engine.STAGE_ORDER`), **13** shared-integrity gates
(`gates.GATE_NAMES`), and separate EL/T gate rosters
(`gates.VARIANT_GATE_NAMES`: **15** for `extract_load`, **16** for
`transform`). Compute these counts from the code. The roster is also
self-describing at runtime: `gates.SCORER_VERSION`
(manually bumped, currently `1.2.0`) and the derived `gates.ROSTER_DIGEST` are
stamped into every `AcceptanceReport`, and
`variant_battery.roster_staleness(payload, variant)` turns a battery recorded
under an older roster or scorer into a named refusal rather than a silent pass.
`tests/test_docs_consistency.py` pins the counts stated in README.md and here.

---

## Workspace layout

All pipeline state and artifacts live under one workspace root (default
`runs/default`, i.e. `workspace.DEFAULT_WORKSPACE`; override with
`--workspace`). The tree below is drawn at that default:

```
runs/default/
  state/
    taskgen.sqlite              # engine ledger (schema below)
  tasks/
    <task_id>/
      task_ir.json              # full TaskIR round-trip dump (task_to_json)
      task/                     # destination-bound solver runtime export
        config.yaml
        data_model.yaml
        documentation/
          README.md             # authored specification plus runtime index
          ...                   # Airbyte, destination, source, and job-run guides
        check_job_status.py
        snowflake_credential.json # empty credential placeholder; injected at install
        elt/main.tf             # agent-completed Airbyte Terraform scaffold
        schemas/<table>.csv     # header: column_name,column_description
      answer_key/               # PRIVATE — never shipped with task/
        gold/
          <population>/
            stage1_counts.json  # {table: row_count}
            <mart>.csv          # ordered stage-2 gold
        gt/<mart>.csv           # upstream-shaped ground truth for the graded population
        table.json
        sort_key.json
        flat_files_serving.json   # per-population URL map for the FILES backend
        sources_serving.json      # every table -> backend + rendered artifact + load rule
        evaluation/sql/<mart>.sql
        reference/              # customer_summary.sql (trusted solution) + solution.json
        manifest.json           # sha256 of every answer_key file + TaskIR content hash
      populations/
        <population>/
          rows/<table>.jsonl    # generated rows, canonical order
          rendered/<backend>/...# renderer outputs for this population
      attacks/
        <attack_name>/          # mutated SQL + per-population reward record
      variants/
        <variant>/              # INTERNAL EL/T battery bundle + private reward evidence
      reports/                  # JSON copies of council/gate/difficulty reports
        filters/<filter>.json   # verification/filters.py pre-council filter reports
  transcripts/
    <role>/<key>.json           # recorded provider exchanges (providers.py). Stores
                                # prompt_sha256, system_sha256 (= role_behavior_sha256,
                                # the behaviour-manifest digest), usage, elapsed_ms,
                                # response, raw_attempts and an entry_schema-2 route
                                # block {provider, model, max_tokens, effort,
                                # behavior_sha256, tools_sha256, policy_sha256,
                                # diagnostics_version, entry_schema} — NOT the prompt
                                # text. The filename IS the key: transcript_key_v3
                                # (see providers.transcript_key)
  release/                      # frozen release: public/ + private/ (answer keys AND the
                                # graded populations' rendered source roots) +
                                # release_manifest.json + checksums.sha256
                                # — layout under 'export/release.py' below
  reference/                    # GOAL/measurement material — never candidates
    anchors/
      <anchor_task_id>.json     # TaskIR dumps of imported ELT-Bench anchors
                                # (moved 2026-08 from <workspace>/anchors/, which
                                #  is migrated on load/save, never ignored)
```

`task/` and `answer_key/` are disjoint trees; release packaging (export/
release.py) must be able to ship `task/` without touching `answer_key/`.

---

## Engine state DB (SQLite, `state/taskgen.sqlite`)

The evidence tables (`reports`, `artifacts`, `repairs`) are append-only. The
latest evidence for a task is the row with `MAX(id)` for that `task_id`.
`repair_intents` is the narrow transaction/recovery journal: a row moves once
from `pending` to `committed`, and its recovery-only BLOBs are cleared at that
transition. Wall-clock timestamps are allowed in state, but not in artifacts.

```sql
CREATE TABLE IF NOT EXISTS reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  revision      INTEGER NOT NULL,
  stage         TEXT NOT NULL,     -- engine.STAGE_ORDER, in sequence order:
                                   -- 'intake'|'contamination_pre'|'generate'|
                                   -- 'reference'|'author'|'review'|'attack'|
                                   -- 'gates'|'gates_extract_load'|
                                   -- 'gates_transform'|'calibrate'|
                                   -- 'contamination_post'|'select'|
                                   -- 'release'   (14 values)
  verdict       TEXT NOT NULL,     -- 'pass' | 'fail' | 'blocked' | 'fatal'
  payload_json  TEXT NOT NULL,     -- canonical JSON of the typed report object
  content_hash  TEXT NOT NULL,     -- TaskIR.content_hash() the report binds to
  created_at    TEXT NOT NULL      -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS reports_task ON reports(task_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  rel_path   TEXT NOT NULL,        -- relative to the workspace root
  sha256     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repairs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  revision    INTEGER NOT NULL,    -- revision the repair produced
  route       TEXT NOT NULL,       -- RepairRoute value
  reason      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  fingerprint TEXT NOT NULL DEFAULT '',
                                   -- repair.repair_fingerprint: the state the
                                   -- round STARTED FROM, which is what makes an
                                   -- inert repair provable. '' means the row
                                   -- predates the column (added by
                                   -- engine._REPAIRS_MIGRATIONS as an idempotent
                                   -- ALTER TABLE ADD COLUMN, never a rewrite) and
                                   -- is read as "not recorded", never "unchanged".
  lineage_root_hash TEXT NOT NULL DEFAULT ''
                                   -- content hash of the lineage's initial
                                   -- TaskRevision. repair budgets and inertness
                                   -- use only the live TaskIR's lineage; old rows
                                   -- remain append-only after --reingest. Empty
                                   -- legacy values are recovered from repair
                                   -- intents or bound before overwrite.
);

CREATE TABLE IF NOT EXISTS repair_intents (
  intent_id       TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL,
  target_revision INTEGER NOT NULL,
  route           TEXT NOT NULL,
  reason          TEXT NOT NULL,
  fingerprint     TEXT NOT NULL,
  task_json       TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('pending', 'committed')),
  repair_id       INTEGER NOT NULL UNIQUE,
  report_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL,
  committed_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS repair_intents_task
  ON repair_intents(task_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS repair_intents_one_pending_task
  ON repair_intents(task_id) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS repair_intent_files (
  intent_id     TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  rel_path      TEXT NOT NULL,
  before_exists INTEGER NOT NULL CHECK (before_exists IN (0, 1)),
  before_sha256 TEXT NOT NULL,
  after_exists  INTEGER NOT NULL CHECK (after_exists IN (0, 1)),
  after_sha256  TEXT NOT NULL,
  content       BLOB,
  PRIMARY KEY (intent_id, ordinal),
  UNIQUE (intent_id, rel_path)
);
```

A repair commit first stores its complete target file set and appends the one
`repairs` row plus every applicable downstream `fail` invalidation in a single
SQLite transaction. Only then may live task artifacts move. `Engine.run`
finishes any `pending` intent under the task lock before dispatching a stage;
replay accepts a file only at its recorded before or target hash and otherwise
halts fail-closed. While an intent is pending, `report_is_current` rejects every
prior pass and `final_verdict` is `in_progress`. Once all target hashes and the
target TaskIR are installed, the journal marks the intent `committed` and clears
the stored BLOBs while retaining both hash sets.

Verdict queries: latest verdict per stage = `SELECT * FROM reports WHERE
task_id=? AND stage=? ORDER BY id DESC LIMIT 1`. A project is accepted iff its
latest `gates_extract_load` and `gates_transform` reports are both current,
roster-complete, and carry `accepted=true`. The shared `gates` row is internal
integrity evidence, not a third unit verdict.

`blocked` is the fourth verdict. It indicates a pending human adjudication
(an exhausted repair proposer) or an environmental condition (an
immutable `release/` already holds different content or fails byte
verification; the contamination index does not have `ARMED` coverage in this
workspace; the
installed runtime differs from `uv.lock`; a battery's recorded evidence is not
current, in which case the stage that re-derives it is shadowed so the next
`run()` clears the block without an operator flag). A blocked row spends no
repair round, rejects nothing, and never shadows an accepting battery.
Because `run()` only skips a stage on a pass at the current hash, the stage re-executes
by itself once the condition clears. `Engine.report_is_current(task, stage,
row)` is the single current-evidence predicate: a pass at the current content hash, and
for the two variant-gate stages additionally `roster_staleness() == ''`.

---

## Module contracts

### `models.py` (FINAL — no builder edits)

Key exports: `TaskIR`, `TableSpec`, `ColumnSpec`, `Relationship`,
`BackendAssignment`, `MartSpec`, `MartColumn`, `MartPlan`, `MartOp`,
`PopulationName`, `PopulationSpec`, `ReferenceSolution`, `AttackCase`,
`AttackKind`, `Finding`, `FindingDisposition`, `FindingProvenance`,
`CouncilRole`, `Severity`, `RepairRoute`,
`GateResult`, `AcceptanceReport`, `DifficultyMeasurement`,
`EmpiricalDifficulty`, `TaskRevision`, `TaskStatus`, `Origin`, `Backend`,
`ColumnType`, `JoinType`, `MartOpKind`, `TaskVariant`, `AuditApproval`,
`PoolSelection`, plus helpers `canonical_json`, `sha256_hex`, `derive_seed`,
`task_to_json`, `task_from_json`, `variant_task_id`, `slugify_family`, and type
aliases `Scalar`, `Row`.

`TaskIR.content_hash()` excludes `status` and `revisions`. Every stage that
persists a report must record the content hash it ran against.

`TaskVariant` and `AuditApproval` are standalone declarations. Neither adds a
field to `TaskIR`, so content hashes of tasks authored before Phase B are
unchanged (verified against a pre-round frozen release manifest).

```python
class TaskVariant(str, Enum):          # views over one parent curation record
    FULL = "full"; EXTRACT_LOAD = "extract_load"; TRANSFORM = "transform"

RLVR_TASK_VARIANTS = (TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM)
    # the two required internal certification phases; FULL is legacy diagnostic
    # compatibility. Schema-3 releases expose one combined public parent task.

def variant_task_id(task_id: str, variant: TaskVariant) -> str
    # '<id>' | '<id>__el' | '<id>__t' — suffixes identify internal/private
    # phase evidence. Variants keep the PARENT family_id, so family/cluster
    # split isolation is unchanged.

```

#### Round 4 additions to `models.py` (additive, hash-neutral)

`Origin` includes two additional ingestible pools:

```python
class Origin(str, Enum):
    DBT = "dbt"; SYNSQL = "synsql"; DLT = "dlt"
    SCHEMAPILE = "schemapile"          # NEW — schemapile-perm.json records
    WIKIDBS = "wikidbs"                # NEW — WikiDBs part-N database dirs
    ELTBENCH_ANCHOR = "eltbench_anchor"; SYNTHETIC = "synthetic"; DEMO = "demo"
```

Existing members retain their wire values, and the pinned demo task content
hash remains unchanged, as asserted by `tests/test_models_round3.py` and
`tests/test_models_pools.py`. `origin` is semantic and participates in
`content_hash()`, so changing an existing task's origin creates a new identity.

All five source adapters use this ingest-identity contract:

```python
FAMILY_SLUG_MAX_LEN: int                       # 64

def slugify_family(text: str) -> str
    # raw record name -> a legal family-id SEGMENT. lowercase; every run of
    # non-[a-z0-9] collapses to a single '_' (so '__' can never appear inside a
    # segment); strip; a name that normalizes to nothing (non-latin WikiDBs
    # names) becomes 'x<sha256(text)[:12]>'; over FAMILY_SLUG_MAX_LEN it is
    # truncated + '_<sha256(text)[:12]>'. Deterministic, never empty, never
    # raises — and NOT injective: callers needing uniqueness across a pool
    # check for collisions themselves.

class PoolSelection(CanonicalModel):           # INGEST-TIME value object
    pool: str            # lowercase family-id segment; the family namespace
    selector: str        # the record key VERBATIM as the pool spells it
    origin: Origin
    family_id: str       # namespaced 'pool__family'
    license: str         # SPDX id / license name of THIS record
    attribution: str = ""

    @classmethod
    def for_record(cls, *, pool, selector, origin, license,
                   attribution="", family=None) -> PoolSelection
        # derives family_id = f"{pool}__{slugify_family(family or selector)}"
    def ir_identity(self) -> dict[str, Any]
        # {family_id, origin, license, attribution} — splice into TaskIR:
        #   TaskIR(task_id=..., cluster_id=..., **sel.ir_identity(), tables=...)
        # task_id/cluster_id stay with the adapter: only it knows the
        # subgraph / schema-similarity structure they encode.
```

Construction fails closed: a blank or whitespace-only `license` is rejected,
and `family_id` must be namespaced within the specified `pool`. Family and
cluster split isolation and contamination namespacing depend on this constraint.
`TaskIR` does not reference `PoolSelection`, so constructing one cannot change
a content hash.

### `catalog.py` (Round 4 — the vendored-source catalog)

```python
PINNED_ROOTS: dict[str, str]        # optional sibling-checkout discovery; env wins
def default_sources_config_path() -> Path            # config/sources.yaml

class PoolSource(BaseModel)         # one catalog row
    pool, origin, root, license, license_per_record, attribution,
    provenance_manifest, excluded: tuple[str, ...], notes
    def root_path(self) -> Path
    def record_path(self, selector: str) -> Path
    def provenance(self, selector) -> dict[str, str]   # upstream/commit/tier
    def attribution_for(self, selector) -> str
    def selection(self, selector, *, license=None, attribution=None,
                  family=None) -> PoolSelection

class SourceCatalog(BaseModel)
    pools: tuple[PoolSource, ...]; instruments: dict[str, str]; config_source: str
    def pool(self, name) -> PoolSource        # KeyError on unknown
    def instrument(self, name) -> Path        # non-pool tooling artifacts
    def pool_names(self) -> tuple[str, ...]

def load_source_catalog(path: Path | None = None) -> SourceCatalog
def selection_for(pool, selector, *, license=None, attribution=None,
                  family=None, catalog=None, config_path=None) -> PoolSelection
```

`config/sources.yaml` defines one row per ingestible pool (`dbt`, `dlt`,
`synsql`, `schemapile`, `wikidbs`, `eltbench`) with its vendored root,
`models.Origin`, license terms, attribution and exclusions, plus an
`instruments:` map for tooling that is not a pool (the WikiDBGraph edge/community
CSVs used to pair WikiDBs schemas). Roots interpolate `${ELT_TASKGEN_DATA_ROOT}`
/ `${ELT_TASKGEN_BENCH_ROOT}`. A source checkout may discover same-named
sibling checkouts through `PINNED_ROOTS`; a wheel embeds no personal path and
`PoolSource.root_path()` fails with an actionable error until the variable is set.

Fail closed: unknown pool/instrument raises; an `excluded` record (e.g.
`dlt_shopify`) cannot be selected at all; and a `license_per_record` pool
(SchemaPile, whose records carry their own `INFO.LICENSE`) refuses to produce a
selection unless the caller passes that record's license. A record with no
usable license must be skipped, never ingested under a pool-wide default. A
`root` that does not exist is not a load-time error because the catalog is
metadata;
the adapter that reads the pool raises on the missing path.

The pool key is the family-id namespace, not the `Origin` value. The anchors' row
is keyed `eltbench` (Origin `eltbench_anchor`) because
`adapters/eltbench_anchor.py` has always emitted `eltbench__<db>`, and renaming
the namespace would move 100 family ids and every contamination fingerprint
derived from them.

Adapters are not required to route through the catalog, but the license and
attribution they stamp on a TaskIR must agree with it, and new ingest CLI
commands should default `--root`/`--license` from it rather than hardcoding.

### `demo_fixture.py` (implemented)

```python
DEMO_TASK_ID: str
MART_NAME: str
REFERENCE_SQL: str
HARDCODE_PRIMARY_DIRECTIVE: str            # "directive:hardcode-population-outputs:primary"
COUNTERFACTUAL_LITERAL_ROWS: dict[str, tuple[Row, ...]]
COUNTERFACTUAL_EXPECTED_MART: tuple[Row, ...]
def demo_task() -> TaskIR
def demo_mart_plan() -> MartPlan
```

The demo attack matrix inside `demo_task().attack_cases` is normative; gates
must reproduce it exactly.

### `engine.py`

```python
class StageName(str, Enum): ...            # the 14 ledger stages listed above
STAGE_ORDER: tuple[StageName, ...]         # intake, contamination_pre, generate, reference,
                                           # author, review, attack, gates,
                                           # gates_extract_load, gates_transform, calibrate,
                                           # contamination_post, select, release
                                           # (len == 14 — THE authority on stage count)

class Engine:
    def __init__(self, workspace: Path, *, max_repair_rounds: int = 3): ...
    def register(self, task: TaskIR) -> None            # writes task_ir.json + intake report
    def load_task(self, task_id: str) -> TaskIR         # from workspace task_ir.json
    def save_task(self, task: TaskIR) -> None
    def record_report(self, task: TaskIR, stage: str, verdict: str, payload: BaseModel) -> int
    def latest_report(self, task_id: str, stage: str) -> ReportRow | None
    def record_artifact(self, task: TaskIR, rel_path: str, sha256: str) -> None
    def repair_rounds_used(self, task_id: str) -> int   # current content lineage only
    def pending_repair_intent(self, task_id: str) -> RepairIntentRow | None
    def recover_pending_repair(self, task_id: str) -> TaskIR | None
    def run(self, task_id: str, *, until: str | None = None, pre_run=None) -> TaskIR
        # resumable orchestration of the 15-stage order; re-runs a stage unless
        # report_is_current() holds for it; first recovers a pending repair
        # under the task lock (before pre_run), routes failures through
        # repair.py, enforces max_repair_rounds then rejects.
    def report_is_current(self, task, stage, row) -> tuple[bool, str]
        # THE currency predicate: PASS at the current content hash, and for
        # gates_extract_load / gates_transform additionally
        # variant_battery.roster_staleness(...) == ''. Returns the reason when
        # not current, so the CLI can say WHY a stage re-ran. Any pending repair
        # intent makes every stage non-current until recovery completes.
    def blocked_stage(self, task_id: str) -> ReportRow | None
        # the latest BLOCKED row at the current hash, if the run stopped waiting
    def final_verdict(self, task_id: str) -> str        # 'accepted'|'rejected'|'in_progress'

VERDICT_PASS = "pass"; VERDICT_FAIL = "fail"
VERDICT_BLOCKED = "blocked"                # waiting on a human / the environment
VERDICT_FATAL = "fatal"

class EngineError(Exception): ...
class RepairCommitError(EngineError): ...  # durable PENDING intent; recover/re-run, never another round
class InfrastructureFailure(EngineError):  # task_id, stage, marker
    # The transport failed, not the task. Raised AFTER recording a FAIL row
    # (never FATAL): no repair round is spent, the task is NOT rejected, and a
    # re-run in the same workspace resumes at the failed stage. The CLI maps it
    # to exit 2. Classified from the payload's own error/detail text
    # (_INFRA_FAILURE_MARKERS), so a council finding that mentions metrology
    # without a matching failure marker is not mistaken for a dead provider.
class StageBlocked(InfrastructureFailure):  # task_id, stage, blocked_on; marker = "blocked_on:<reason>"
    # A stage WAITED (VERDICT_BLOCKED: an environment / human-approval hold,
    # stale evidence, another seat's session limit) where only a MEASUREMENT
    # would do — a member of a repair proposer's certification
    # (repair_proposer._revalidate) or of the in-session `certify`. Scored
    # live the same verdict is a resume point (run() records the BLOCKED row
    # and returns: no round, no rejection); inside a certification it is a
    # NO-MEASURE outcome, never a red one: the certifier halts under
    # BLOCKED_STAGE_MARKER_PREFIX + reason, nothing is rejected, no round is
    # spent, `_halt_on_proposer_fault` puts `blocked_on` on the FAIL row, and
    # the ledger resumes at the failed stage once the wait clears (C7).
BLOCKED_STAGE_MARKER_PREFIX = "blocked_on:"; BLOCKED_ON_UNKNOWN = "unknown"
def blocked_on_of(payload) -> str            # data.blocked_on of a BLOCKED payload, lowercased; '' when none
def blocked_stage_marker(blocked_on) -> str  # "blocked_on:<reason>" ("blocked_on:unknown" for none)
def blocked_on_from_marker(marker) -> str    # the reason a blocked_on: marker names; '' for any other marker

@dataclass(frozen=True)
class ReportRow:  # thin typed view of a reports row
    id: int; task_id: str; revision: int; stage: str
    verdict: str; payload_json: str; content_hash: str; created_at: str
```

Consumes: everything. Produces: ledger rows, updated `task_ir.json`.

### `repair.py`

```python
@dataclass(frozen=True)
class ArtifactDiff:
    changed: frozenset[str]   # rel_paths whose sha256 moved between snapshots

def snapshot(workspace: Path, task_id: str) -> dict[str, str]     # rel_path -> sha256
def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> ArtifactDiff
def route_from_diff(diff: ArtifactDiff) -> RepairRoute
    # answer_key/reference/** or answer_key/gold/** changed -> REFERENCE
    # populations/** changed                                 -> POPULATION
    # task/sources/** or rendered/** only                    -> RUNTIME
    # task_ir.json solver_prompt/marts prose only            -> SPECIFICATION
    # (precedence: REFERENCE > POPULATION > RUNTIME > SPECIFICATION)
def route_for_failure(stage: str, payload: BaseModel) -> RepairRoute
    # maps a failed report to a route when no patch exists yet (e.g. smoke
    # failure -> RUNTIME, determinism failure -> RUNTIME, gold mismatch ->
    # REFERENCE, contamination/licensing -> FATAL)
def repair_fingerprint_after_changes(workspace, task, changed_files) -> str
def apply_repair(engine: Engine, task: TaskIR, route: RepairRoute, reason: str,
                 *, fingerprint=None, staged_files=None) -> TaskIR
    # returns the new-revision TaskIR (with_revision); journals staged target
    # bytes and atomically appends the repair plus all route invalidations
    # before installing those bytes
```

The five routes and their rerun sets (`repair.RERUN_STAGES` is authoritative):
`specification` → rerun author+review; `population` → regenerate populations,
gold, attacks, everything downstream; `runtime` → rebuild environments, rerun
reference execution; `reference` → invalidate gold, rerun reference; `fatal` →
reject, record fingerprint.

### `adapters/dbt.py`

```python
class CandidateSpec(BaseModel): ...   # module-owned: raw models/deps/tests/sources

def load_manifest(manifest_path: Path) -> CandidateSpec
def extract_tasks(spec: CandidateSpec, *, pool: str = "dbt") -> list[TaskIR]
    # connected-subgraph extraction; TaskIR.origin=Origin.DBT,
    # family_id=f"{pool}__{package_name}"; rejects trivial cuts (raise ValueError)
```

### `adapters/synsql.py`

```python
class SynSQLRecord(BaseModel):        # module-owned; answer fields quarantined
    db_id: str; sql_complexity: str; question_style: str
    # question/sql/cot/external_knowledge are loaded but NEVER exported:
    # model_dump(exclude=...) enforced via private attrs

def iter_records(data_json: Path) -> Iterator[SynSQLRecord]     # streaming
def load_tables(tables_json: Path) -> dict[str, dict]           # db_id -> raw schema entry
def schema_to_tables(entry: dict) -> tuple[tuple[TableSpec, ...], tuple[Relationship, ...]]
    # uses column_names_original/table_names_original/column_types/
    # foreign_keys[[i,j]]/primary_keys; sqlglot parses ddls for enum/nullable hints
def to_task_ir(db_id: str, tables_json: Path, *, pool: str = "synsql") -> TaskIR
def to_task_ir_with_schema_atoms(...) -> tuple[TaskIR, frozenset[str]]
    # one tables.json parse; immutable atoms are trusted context for the leak guard
def assert_no_answer_leak(task, records, *, trusted_schema_atoms=None) -> None
    # omitted context is fail-closed: no schema-derived exemptions
    # LEAK GUARD: output contains no question/sql/cot/external_knowledge text;
    # a unit test greps every emitted string field against those source strings
```

### `adapters/dlt.py` (stub-level acceptable)

```python
class DltManifest(BaseModel): ...     # endpoints, cursors, write dispositions
def load_connector(spec_path: Path) -> DltManifest
def to_task_ir(manifest: DltManifest, *, pool: str = "dlt") -> TaskIR   # rest-backend tables
```

### `adapters/eltbench_anchor.py`

```python
def import_anchor_task(bench_root: Path, db_name: str) -> TaskIR
    # reads elt-bench/snowflake/<db>/config.yaml (+ data_model.yaml, schemas/)
    # and evaluation/{table.json, sort_key.json}; origin=Origin.ELTBENCH_ANCHOR,
    # family_id=f"eltbench__{db_name}"
def import_all_anchors(bench_root: Path) -> list[TaskIR]
def anchor_store_dir(workspace: Path) -> Path                  # <ws>/reference/anchors
def legacy_anchor_store_dir(workspace: Path) -> Path           # <ws>/anchors (deprecated)
def migrate_legacy_anchor_store(workspace: Path) -> list[str]  # MOVES legacy -> reference/, ALL-OR-NOTHING
def save_anchor_store(tasks: list[TaskIR], workspace: Path) -> None  # reference/anchors/*.json
def load_anchor_store(workspace: Path) -> list[TaskIR]
```

Anchor tasks are measurement-only: selection reads them and release refuses
them. The store lives under `reference/`, not beside `tasks/`, because
ELT-Bench is the target rather than a source. Save and load first migrate a
legacy `<ws>/anchors/` store with a `DeprecationWarning`, ensuring the
contamination index includes legacy data. CLI: `measure-target` (deprecated
alias `ingest-anchor`).

### `generation/populations.py`

```python
def default_populations(task_id: str, scale_hint: dict[str, int]) -> tuple[PopulationSpec, ...]
    # all five PopulationName values, seeds via derive_seed(task_id, name).
    # scale_hint / PopulationSpec.scale is the INTENDED size, not the realized
    # row count — see source_data.realized_row_count below.
def validate_population_coverage(task: TaskIR) -> list[str]
    # returns problem strings; empty list = ready. Checks: all five present,
    # counterfactual has literal_rows or explicit conditions targeting each
    # required AttackKind in task.attack_cases, primary/resampled share scale.
```

### `generation/source_data.py`

```python
GENERATION_POLICY_VERSION: int = 6
# v4 adds one targeted constructed-data boundary: when row A controls a
# nullable bridge-side argmax label, its higher-measure winning row carries
# JSON null and witness_conditions records that fact. v5 appends row L when
# the exact bridge-side measure role is nullable: two real linked rows whose
# measure values are both NULL, distinct from row B's no-link placeholder.
# Row L follows every pre-v5 witness, so their indices and row order do not
# move; a non-nullable measure adds no row. v6 adds a separate, deterministic
# non-NULL missing-parent fact row when the standard shape's exact owner link
# is optional, including when that link is nullable; this distinguishes legal
# dangling keys from SQL NULL. Re-ingest is required to rebuild an already-
# frozen TaskIR under any changed policy; existing TaskIR remains valid and
# byte-stable.
def column_rng(task_id: str, population: PopulationName, table: str, column: str) -> random.Random
    # random.Random(derive_seed(task_id, population.value, table, column))
def realized_row_count(task_id: str, table: str, declared: int) -> int
    # how many rows a table ACTUALLY gets for a declared scale of `declared`:
    # 2-7% off it, sign and magnitude from derive_seed, never a multiple of 10,
    # exact below REALIZED_DIVERGENCE_MIN_SCALE (=50). The declared scale must
    # not be the answer to the extract-load subtask, which is graded on the
    # row-count vector alone (upstream_eval.compare_stage1). Deliberately NOT
    # keyed on the population: primary and resampled declare the same scale and
    # gates._gate_el_data_sensitivity requires their count vectors to agree.
def generate_rows(task: TaskIR, population: PopulationName) -> dict[str, list[Row]]
    # constraint-aware: honors literal_rows verbatim; parents before children;
    # FK values drawn from generated parent keys; required links always valid;
    # optional links may be NULL/dangling per population conditions; enum
    # domains, nullability, business-key uniqueness, temporal ordering enforced.
    # Policy v3 gives PRIMARY/RESAMPLED/STRESS deterministic portable adversarial
    # cases: nine-decimal numerics; >2^53 BIGINT values/ids only where linked
    # children are also BIGINT; non-empty Unicode, mixed-case and null-like text;
    # leap/DST/year-boundary naive DATE/TIMESTAMP values; nested JSON pairs that
    # distinguish an explicit null from a missing key; and stronger tie/skew
    # patterns. PRIMARY and RESAMPLED rotate the fixed cases.
def write_rows(rows: dict[str, list[Row]], out_dir: Path) -> dict[str, str]     # table -> sha256
def render_population(task: TaskIR, population: PopulationName,
                      rows: dict[str, list[Row]], out_dir: Path) -> dict[str, Path]
    # dispatches per BackendAssignment to:
def render_postgres(table: TableSpec, rows: list[Row], out_dir: Path) -> Path   # load .sql
def render_mongodb(table: TableSpec, rows: list[Row], out_dir: Path) -> Path    # .jsonl
def render_rest(table: TableSpec, rows: list[Row], out_dir: Path, *, page_size: int = 100) -> Path
    # paginated fixture dir: page_0001.json ... + index.json
def render_s3(table: TableSpec, rows: list[Row], out_dir: Path) -> Path         # jsonl layout
def render_files(table: TableSpec, rows: list[Row], out_dir: Path) -> Path      # .csv
```

### `generation/mart_plan.py`

```python
MIN_MART_COLUMNS = 6
MIN_MART_COMPUTED = 5
MIN_MART_PASSTHROUGH = 2
def validate_plan(task: TaskIR, plan: MartPlan) -> list[str]        # problems; [] = ok
def detect_semantic_patterns(plan: MartPlan) -> tuple[SemanticPattern, ...]
def semantic_pattern_problems(plan: MartPlan) -> list[str]         # declarations are certified, not trusted
def solver_safe_plan_requirements(task: TaskIR, mart: MartSpec) -> dict[str, object]
    # THE shared closed solver-visible projection used by author prompts and
    # every public export: ordered descriptions, real source inputs, public
    # carried fields, join preservation, parsed condition public identifiers /
    # literal specification values, and semantic parameters only; never raw
    # predicates/operators/SQL, aggregate expressions, generated aliases, or notes
def solver_safe_condition_requirements(task: TaskIR, mart: MartSpec,
                                       op: MartOp) -> dict[str, list[str]]
    # parsed condition projection used above; unparseable condition text is
    # represented only by the op's public semantic description
def solver_safe_mart_requirements(task: TaskIR, mart: MartSpec) -> dict[str, object]
    # the same projection plus grain, keys, typed output columns, and source
    # relationships for data_model.yaml
def solver_safe_plan_summary(task: TaskIR, mart: MartSpec) -> str   # public prose form
def plan_summary(plan: MartPlan) -> str
    # INTERNAL diagnostic only; may expose compiler state and must never feed
    # a solver prompt, README/documentation.md, or data_model.yaml
def attack_surface(plan: MartPlan) -> dict[AttackKind, list[int]]   # kind -> op indexes it applies to
def plan_template_signature(plan: MartPlan) -> str                  # corpus-collapse identity
def aggregate_then_filter(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan
    # grouped rollup followed by inclusive aggregate-result filtering;
    # witnessed and attacked separately from raw-row filtering
def status_cohort_union(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan
    # passing/failing/no-activity status cohorts; FILTER + DISTINCT aggregate +
    # guarded ratio in each branch, joined with UNION ALL
def latest_snapshot(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan
    # latest linked row per parent: timestamp DESC, unique row key ASC;
    # optional byte-exact DEDUPE before the window
def measure_state_distribution(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan
    # present-value versus absent-value numeric-measure cohorts (absent includes
    # missing measures and no activity); each branch uses FILTER, DISTINCT
    # aggregation and a guarded ratio, then the branches use UNION ALL
```

Plan-library builders fail closed when they cannot fund their advertised
semantics and declare a `MartColumnKind` for every emitted target column. The
six/five/two constants are the common measured column-budget contract; the
challenging profile additionally enforces the six-column and complete-kind
parts on every admitted mart, including legacy adapter paths.

### `generation/difficulty_profiles.py` and `generation/challenging_policy.py`

```python
CHALLENGING_DIFFICULTY_PROFILE: GenerationDifficultyProfile
    # synthetic active PRIMARY/RESAMPLED floor 32,768; <=40,000 rows/table,
    # <=100,000 rows/task; raises evidence-backed mart budgets and spreads grains
CHALLENGING_TEMPLATE_SHARE_NUMERATOR = 1
CHALLENGING_TEMPLATE_SHARE_DENOMINATOR = 4
def validate_challenging_cohort(
    tasks: Iterable[tuple[str, TaskIR]],
    *,
    coverage_policy: SemanticCoveragePolicy = PERMISSIVE_SEMANTIC_COVERAGE_POLICY,
) -> ChallengingCohortPolicyReport
    # before reference work: every mart >=6 columns, zero unclassified columns;
    # no multi-mart task repeats one signature throughout; no signature >25%
```

The 25% comparison uses integer products, so exactly one quarter is admitted
without floating-point rounding. This is a deterministic structural policy,
not an empirical solver-difficulty label.

### `generation/coverage.py`

```python
class SemanticCoveragePolicy
    # known template/pattern vocabularies; explicit-tag and structural-
    # certification switches; independent mart-owner and task-owner minimums
def measure_semantic_coverage(
    tasks: Iterable[tuple[str, TaskIR]],
    *,
    policy: SemanticCoveragePolicy = PERMISSIVE_SEMANTIC_COVERAGE_POLICY,
) -> SemanticCoverageReport
def write_semantic_coverage(path: Path, report: SemanticCoverageReport) -> str
    # writes canonical hash-bound JSON and returns its SHA-256 digest
```

The permissive policy measures legacy tasks without moving their identities.
A strict migration can require explicit known identities and independently
certified pattern structure before reference work begins.

### `reference/solution.py`

```python
class LoadedSources(BaseModel): counts: dict[str, int]              # table -> rows loaded

def build_reference(task: TaskIR) -> ReferenceSolution
    # returns task.reference if present (demo path), else constructs from plans
def load_sources_duckdb(task: TaskIR, rendered_dir: Path, con: duckdb.DuckDBPyConnection) -> LoadedSources
    # trusted Extract+Load: parse each backend's rendered format back into
    # DuckDB tables named exactly like TaskIR tables
```

### `reference/duckdb_sandbox.py`

```python
def sandboxed_memory_connection() -> duckdb.DuckDBPyConnection
    # ':memory:', enable_external_access=false, THEN lock_configuration=true
def assert_sandboxed(con) -> None
```

Every site that executes externally supplied SQL must use this connection helper,
including `corpus/calibration._score_attempt` for measured solver SQL and
`reference/independent._execute_population` for the cross-family witness. This
prevents submissions from reading `**/answer_key/**` through `read_csv_auto`
or writing to the host through `COPY ... TO`. External access is disabled while
the configuration is writable, then the configuration is frozen. Trusted
loaders may use the helper as defense in depth. Sites that must attach a
`.duckdb` file, including the `export/eltbench.py` census and materialization,
must not use it.

### `reference/runner.py`

```python
class RunResult(BaseModel):
    population: PopulationName
    stage1_counts: dict[str, int]                 # table -> row count
    mart_rows: dict[str, list[Row]]               # mart -> rows in gold order
def run_reference(task: TaskIR, population: PopulationName, workspace: Path) -> RunResult
    # smoke-check rendered sources; use the compatibility loader for historical
    # stage-1/content checks, then re-load the same bytes through the strict
    # typed projection used by cloud parity before executing reference marts.
    # This keeps newly frozen DECIMAL gold inside DECIMAL(38,9) instead of
    # serializing binary-float aggregation noise. Order by mart keys.
class RunDigests(BaseModel):
    stage1: str                                   # sha256 of the stage-1 surface ONLY
    stage2: str                                   # sha256 of the ordered mart CSVs ONLY
    joint: str                                    # historical whole-run digest (unchanged)
def run_digests(task: TaskIR, result: RunResult) -> RunDigests
    # the three separable digests of one run. stage1 and stage2 cover DISJOINT
    # surfaces: a mart perturbation cannot move stage1, a count perturbation
    # cannot move stage2. Values only (counts, CSV text) — no path, no clock.
def determinism_evidence(task: TaskIR, population: PopulationName, workspace: Path,
                         *, runs: int = 3) -> GateResult
    # destroy/rebuild/rerun; byte-compare canonical CSVs across runs, per
    # component. evidence carries run_<i>_sha256 + run_<i>_stage1_sha256 +
    # run_<i>_stage2_sha256, and — for each component that held still across
    # EVERY run — the citable digests "stage1_digest", "stage2_digest",
    # "run_digest". A component that diverged is NOT published; instead
    # evidence["diverged_components"] and the details name which one moved.
    # Per-variant gates cite a component (EL -> stage1, T -> stage2); only the
    # FULL variant may cite the joint digest.
```

### `reference/gold.py`

```python
class GoldBundle(BaseModel):
    task_id: str
    task_content_hash: str
    stage1: dict[str, dict[str, int]]             # population -> table -> count
    stage2_csv: dict[str, dict[str, str]]         # population -> mart -> canonical CSV text
    file_hashes: dict[str, str]                   # rel_path -> sha256

def freeze_gold(task: TaskIR, results: dict[PopulationName, RunResult],
                answer_key_dir: Path) -> GoldBundle          # writes gold/ + manifest.json
def load_gold(answer_key_dir: Path) -> GoldBundle            # verifies every hash; fail closed
def gold_digests(gold: GoldBundle, population: PopulationName | str) -> RunDigests
    # the same split digests, recomputed from the FROZEN bundle over the same
    # canonical layout the runner uses — so recorded determinism evidence can
    # be checked against what actually ships. Unknown population -> KeyError.
```

### `verification/reference_readiness.py`

```python
REFERENCE_READINESS_EVIDENCE_REL = "reports/reference_readiness.json"
REFERENCE_READINESS_ROSTER: tuple[str, ...]       # deterministic local checks
CHALLENGING_REFERENCE_READINESS_ROSTER: tuple[str, ...]
    # REFERENCE_READINESS_ROSTER + "challenging-mart-contract"
def ensure_reference_readiness_evidence(
    task: TaskIR, workspace: Path, gold: GoldBundle
) -> tuple[Path, ...]
def _effective_lineage_gate(task: TaskIR, gold: GoldBundle) -> GateResult
    # compiled SQL remains authoritative for transform lineage. A table absent
    # from every compiled mart is admitted as extract/load-only only when a
    # MartOpKind.SOURCE operation explicitly declares it and frozen stage-1
    # vectors bound to this task contain a valid count for it in every
    # population, positive in at least one graded population. Undeclared,
    # unbound or ungraded dead tables stay red.
def run_reference_readiness(
    task: TaskIR, workspace: Path, gold: GoldBundle,
    *, record: bool = True, challenging: bool = False,
) -> AcceptanceReport
    # challenging=True also requires every mart to have >=6 target columns and
    # a certified production kind for every target column
```

This roster is intentionally provider-free and certifies only deterministic
reference-stage structure. It neither performs nor waives admission review.
Full and transform acceptance still require the current hash-bound,
cross-family independent reconstruction described below; extract-load instead
uses its independent bundle load and per-format artifact census witnesses. An
extract/load-only lineage classification therefore does not establish transform
lineage. Later EL gates must still reconcile every declared table's rendered
artifact and frozen count and independently discover a complete load plan.

### `review/council.py`

```python
class Provider(Protocol):
    def complete(self, role: CouncilRole, prompt: str) -> str

class ProviderProtocolError(RuntimeError):
    # raised when a provider response is not valid JSON, has no 'findings'
    # list, or contains an unparseable finding. NEVER degraded into a
    # synthetic "unparseable" finding: the review stage fails.

def author_prose(task: TaskIR, provider: Provider) -> str
    # IR + solver_safe_plan_summary -> solver-visible prose; the shared
    # projection is also what public data_model/documentation exports render.
    # Caller stores the result via
    # task.model_copy(update={"solver_prompt": prose})  (semantic edit => new hash)
def author_prose_session(task, provider, *, tools, policy, contamination_index=None,
                         coverage_requirement=None, worker=None, clock=None,
                         record=None) -> str
    # Roadmap Phase 1 item 1.A (SoT T1 AUT), BESIDE the untouched author_prose:
    # ONE harness-driven bounded session through provider.run_session on the
    # author's view plus the session protocol
    # (prompts.semantic_author_session_view). The model's every turn is
    # submit_prose(text) or abort(reason_code) — the only two tools on the
    # wire; `tools` must hold the harness validator check_prose and never
    # check_structure or leak_findings (ValueError otherwise; the leak scan
    # stays at review). The harness runs check_prose on EVERY submitted draft
    # — one prose_fidelity.check_prose_fidelity call on
    # task.model_copy(update={"solver_prompt": draft}), projected to codes
    # only — and answers a red draft with the codes while
    # policy.limits.max_revisions remain; a green draft ends the session.
    # contamination_precheck runs once, on the FINAL draft, after the
    # terminal, through the worker and the D1 gatekeeper (assert_value_free).
    # Returns the accepted draft, or on a limit stop the last submitted draft
    # (the author stage's unchanged fidelity gate decides, as for one-shot).
    # Raises ValueError — today's empty-output route, a stage FAIL — when the
    # session abstained or ended without any draft. Halts (SessionFault,
    # DiagnosticTripwire, SessionProtocolError, SessionPolicyViolation,
    # BudgetExceededError, TranscriptMissingError) propagate for the engine
    # to classify as infrastructure (C7). `record`, when given, receives
    # AUTHOR_SESSION_RECORD_KEYS = (result, revisions, drafts, precheck, terminal).
def run_council(task: TaskIR, provider: Provider) -> list[Finding]
    # runs the four critic roles; NEVER returns an acceptance; prose leaking
    # reference SQL or gold values is itself a FATAL finding; unparseable
    # provider output raises ProviderProtocolError (fail closed).
    # BYTE-IDENTICAL since Phase 0 (test_run_council_source_is_byte_identical_to_phase_0):
    # one provider.complete(role, view) per seat; whether that call ran a bounded
    # session is the provider's business (providers.RoutedProvider.complete).
def findings_from_session(task, role, result, session, id_suffix) -> list[Finding]
    # Phase 4 (the Phase 3 hand-off): _parse_findings -> critic_validators.
    # void_uncompilable_proposals -> screen_findings over ONE critic session's
    # final payload — the ONLY path from a critic SessionResult to the screen
    # (test_findings_from_session_is_the_only_path_from_a_session_to_the_screen).
    # result.final is the normalized findings text (str), an auto-submitted
    # validator-green draft (a payload mapping, validated as run_council would)
    # or None (a limit stop with no green draft, or a caught policy violation):
    # an EMPTY findings list, scored as no findings (SoT T4). `session` is the
    # CriticSession whose finding_checks recorded the harness's compile results.
def screen_findings(task: TaskIR, findings: list[Finding]) -> list[Finding]
    # deterministic post-parse screen; never deletes or rewords a finding.
    # EXEMPT: Finding.provenance is CODE (the leak detectors are the contamination checks).
    # NOT exempt: severity — a provider may claim FATAL, and one it files with
    # disposition "withdrawn" is VOIDed like any other finding (R02: nothing
    # written in summary or detail withdraws a finding). A provider FATAL with only weak-evidence signals is NOTED and
    # STAYS FATAL: a rejection reason has no compiled mutant by nature, so
    # evidence-absence must never silence one.
```

`leak_findings` runs three code-only detectors:

1. the literal fast path — fragment containment over normalized text;
2. the AST path (`ast_leak_findings_by_source`) — sqlglot-canonical,
   identifier-anonymized fingerprints of every substantial SELECT in the
   private reference/attack SQL, taken both as written and after
   `eliminate_subqueries` folds inline derived tables back into CTEs, matched
   against parsed SQL-like spans of the prose. Paraphrased,
   alias/CTE/column-renamed, reformatted, and CTE-inlined copies are fatal
   findings;
3. the semantic-signature path (`semantic_leak_findings_by_source`) — the
   base tables, source columns, function set and literals of the private SQL.
   No inlining or restructuring rewrite changes any of them, so a copy that
   shares no query shape at all (`DISTINCT` rewritten as `GROUP BY`, or a grouped
   CTE rewritten as a correlated scalar subquery) is still fatal. Exact
   equality on all four components, behind a non-triviality floor.

English prose stating the same business rule does not match. A span must first
parse into a structurally substantial query. Each source is
reported at most once, by the strongest detector that fired on it.

The `review` stage runner (`cli.make_review_runner`) also requires the
`shortcut_attacker` role to return a finding or an executable probe. If both
counts are zero, the stage fails because it has no supporting evidence.

### `review/prompts.py` (Round 2 — role system prompts)

```python
ROLE_SYSTEM: dict[str, str]           # the five council roles ONLY
def role_system_prompt(role_name, *, agents_config=None) -> str | None
NO_TOOLS_SENTENCE                     # the SHARED_PREFIX sentence a harness-validated seat drops
HARNESS_VALIDATED_SENTENCE            # what replaces it: still no tool, file or query; the ONE
                                      # compile correction the harness itself may return
HARNESS_VALIDATED_PROMPT_ROLES = ("population_adversary", "shortcut_attacker")
def harness_validated_prompt_enabled(role_name, *, agents_config=None) -> bool
```

The population adversary's and shortcut attacker's prompts are derived from
their `session:` blocks in the agents document used for routing (Phase 4,
roadmap Table 8 `review/prompts.py` row; SoT T1.1). With
`enabled: true` the "no tools, no files" sentence is replaced by
`HARNESS_VALIDATED_SENTENCE`. Both seats ship enabled; setting either block to
`enabled: false` restores bytes exactly equal to `ROLE_SYSTEM[role]`, so the
disabled prompt bytes, `system_prompt_sha256`, behaviour digest and transcript
keys are the role's current one-shot identity; flipping the key derives and
hashes the harness-validated identity
(`test_no_tools_sentence_leaves_pop_and_shc_prompts_only_when_enabled`).
Every other role keeps `SHARED_PREFIX` verbatim whatever its block says.

The module defines system prompts for `semantic_author`, `ambiguity_critic`,
`population_adversary`, `shortcut_attacker`, and `feasibility_reviewer`.
Backends send them as the system message. The user message remains exactly the
view built by `council._view_for`, so prompts do not widen an information
barrier. `independent_implementer` has no entry because
`reference/independent.py` builds its complete prompt. Unknown critic roles use
generic findings-tool instructions. Prompt edits invalidate transcripts: the
transcript key is `transcript_key_v3(role, policy, [user turn])` =
`sha256(role_behavior_sha256(role) + "\n" + tools_sha256 + "\n" + policy_sha256
+ "\n" + canonical_json(messages))` (see `providers.transcript_key`), and
`role_behavior_sha256` is the digest of the role's behaviour manifest (system
prompt, wire `tools[]`, tool-choice policy, loop limits, correction text,
`SCHEMA_RETRIES`, API version, role-wired validator and projection versions,
sandbox pin). A role's recorded exchanges become invalid when any of these
inputs change, but remain valid when another role changes. Use `--record` after a
prompt, tool, limit or pin change. The committed fixtures were re-keyed once by
`tools/migrate_transcript_fixtures.py` (offline, provably same prompt and
instructions); the originals stay under `tests/fixtures/transcripts_legacy/`
and a live re-record of the critic fixtures is still owed.

### `verification/structural_completeness.py` (deterministic reference gate)

```python
GATE_NAME = "structural-completeness"
def check_structural_completeness(task: TaskIR) -> list[str]  # absences, NAMED
def structural_completeness_gate(task: TaskIR) -> GateResult
```

Pure static analysis of the TaskIR (no model, no RNG, no clock, no workspace):
every table named by a mart plan op is published or bound by an earlier op;
every column named by an op resolves; a source-to-source join is backed by a
declared relationship; primary/business key columns and both endpoints of every
relationship exist; and the public `## Source tables` block
(`documentation/README.md` in combined exports, `documentation.md` in internal
split variants) plus each `schemas/<table>.csv`, rendered by the exporter's own
functions, publish the same tables and columns as each other and as the IR, and
never print a key or relationship over a column the same block omits.
`cli._structural_failure_detail` consumes these results. Both live-spend stage
runners, `make_author_runner` and `make_review_runner`, call it before any
provider call. A structurally incomplete task is therefore refused without
paying the semantic author or four critics, and failures route to
`SPECIFICATION`. These checks implement the structural part of the feasibility
review. `review/metrology._RETIRED_STRUCTURAL_VARIANTS` identifies the two
specimens moved into `tests/test_structural_completeness.py`. Mart grain remains
prose and is not parsed; deleting a grain dimension fails when an operation
names it.

### `review/prose_fidelity.py` (Round 2 — deterministic completeness gate)

```python
GATE_NAME = "prose-fidelity"
RULE_TERM_COVERAGE: float             # 0.75
COLUMN_DESC_COVERAGE: float           # 0.5
def check_prose_fidelity(task: TaskIR) -> list[str]   # missing items, NAMED
def prose_fidelity_gate(task: TaskIR) -> GateResult
```

Pure text analysis (no model, no RNG, no clock) of `task.solver_prompt`
against every MartSpec: the mart name, all grain terms, every key column,
every output column name plus its description substance, and every plan-op
rule (all identifier tokens plus >= RULE_TERM_COVERAGE of the rule's
significant terms) must be represented. Empty prose fails for every mart. The
safe predicate-like declarative equivalents use
the same local polarity check as literal operation words: `not preserving` for
`LEFT` or `not carried` for project is a contradiction when that outcome is
affirmed nowhere else in the passage. Object/noun equivalents remain excluded
from negation matching, so `cancelled orders are not kept` is not mistaken for
a negated `FILTER` operation. The author stage runner (cli.make_author_runner)
runs this check on the freshly authored prose, including the prose-unchanged
resume path. A failure fails the stage with the missing
items named, routed to `SPECIFICATION` repair. Both the author and review
stage runners also consult the metrology live-admission record: a
live-capable provider without a valid `council.live_admitted` record fails
either stage closed before any live call (replay-only providers are exempt).
The record is resolved by `metrology.admission_record_path`:
`$ELT_TASKGEN_ADMISSION` if set, otherwise `<workspace>/state/`. It is read in
place rather than copied. It is bound to its own path and carries the evidence that earned it
(seed, pool, per-seat metrics, thresholds) under a digest, is re-derived
against the current `metrology:` bar on every read, and is revoked by a
tombstone rather than deletion. `metrology.admission_status` returns the
verdict together with a reason naming which of those failed.

Revocation applies only to future live stages and does not invalidate completed
stages. The admission that authorized a stage is therefore recorded as
provenance:

```python
ADMISSION_MODE_ADMITTED   = "admitted"
ADMISSION_MODE_REPLAY_ONLY = "replay_only"
ADMISSION_MODE_NO_ROUTING  = "no_routing"

class AdmissionStatus:                     # + routing_fingerprint, evidence_sha256, seed
    def provenance(self, mode: str) -> dict[str, str]
        # admission_mode / admission_record_path / admission_routing_fingerprint
        # / admission_evidence_sha256 / admission_seed
```

The author stage merges those five keys into its `StagePayload.data`, the review
stage into `ReviewPayload.admission` (defaulted, so pre-existing ledger rows
still load), and `RoutedProvider` stamps them onto every transcript it records
live. An auditor can list tasks produced under a given `evidence_sha256`
without inferring that relationship from timestamps. Legacy transcripts
without an `admission` key replay unchanged.

The author row's provenance keys in `cli.py` distinguish prose produced by that
author run from prose changed by a later repair:

```python
AUTHOR_PROSE_SHA_KEY = "prose_sha256"         # sha256 of the prose an author PASS produced
AUTHOR_SOURCE_KEY = "source"                  # WHERE it came from:
AUTHOR_SOURCE_PRESERVED = "repair_patch_preserved"   # kept prose a certified repair patch
                                              # committed (STICKY: never re-authored away)
AUTHOR_SOURCE_REVISED = "authored_revised"    # the bounded revision session
                                              # (council.author_prose_session; Phase 1 item 1.A)
AUTHOR_REVISIONS_KEY = "revisions"            # revision requests the harness made in it
AUTHOR_DRAFTS_KEY = "drafts"                  # drafts check_prose checked
AUTHOR_SESSION_TERMINAL_KEY = "session_terminal"; AUTHOR_SESSION_SHA_KEY = "session_sha256"
AUTHOR_SESSION_REPORTS_REL = reports/sessions  # the session record's directory under tasks/<id>/
def _author_session_block(provider) -> dict | None
    # THE ROLLBACK RULE of `roles.semantic_author.session`: the block read from
    # the agents document THIS provider's routing was loaded from, or None —
    # and the stage then calls council.author_prose byte for byte — unless the
    # provider can run a session AND the block is `enabled` AND
    # `max_revisions > 0` (validators.author_session_enabled). Either key
    # alone (`enabled: false` or `max_revisions: 0`) is the rollback. The
    # shipped block is enabled and records the bounded-session provenance keys.
def _author_prose_for(engine, task, provider) -> tuple[str, dict[str, str]]
    # (prose, the provenance data): council.author_prose and {} on the one-shot
    # path; else author_prose_session under validators.author_policy(
    # author_limits(block)), the workspace's contamination index armed for
    # the final-draft precheck, the record persisted at
    # tasks/<id>/reports/sessions/ (_persist_author_session; outside the repair
    # snapshot) and {source: authored_revised, revisions, drafts,
    # session_terminal[, session_sha256]} merged into the author row.
```

A one-shot row carries none of the session keys. Setting
`session.enabled: false` leaves an author row byte-identical to the legacy
one-shot form; the same gate (`registry._session_enabled` →
`validators.author_session_enabled`) keeps the author's tools off the wire
manifest and out of the behaviour digest in that compatibility mode.

### `review/providers.py` (Round 2 — the real multi-agent layer)

```python
def role_behavior_manifest(role_name) -> dict
    # THE behaviour document one role runs under (Phase 0.E; SoT T7):
    # system_prompt_sha256; tools[] and tool_choice_policy EXACTLY as
    # AnthropicBackend._payload sends them (the forced report_findings /
    # report_triage object for a schema role); harness_validators (critic-wired
    # validator specs, empty while every seat is one-shot); policy_sha256
    # (session.SessionPolicy.sha256: allowlist, per-tool caps and schemas,
    # refusal and nudge text, stuck-detector thresholds, limits); loop_limits
    # (the role's `session:` block of config/agents.yaml, verbatim);
    # correction_text_sha256; schema_retries; api_version; validators
    # {code, binaries} and projections (role-wired only);
    # sandbox (the pinned `metrology.sandbox` declaration, never observed state).
def role_behavior_sha256(role_name) -> str      # sha256(canonical_json(manifest))
def transcript_key_v3(role_name, policy, messages) -> str
def transcript_key(role_name, prompt) -> str    # = transcript_key_v3 over the role's
                                                # declared policy and the one user turn
def transcript_entry_schema(entry) -> int       # 0 legacy, 1 bare route block, 2 today;
                                                # readers tolerate lower numbers and
                                                # never upgrade a stored entry
def session_policy_for(role_name) -> session.SessionPolicy
def role_manifest_limits(role_name) -> dict     # the `loop_limits` the manifest hashes: the
                                                # declared block when enforced; a disabled
                                                # runner block folds to {"enabled": false}
                                                # (author, proposer) or to {} for the two
                                                # WITNESS roles (WITNESS_RUNNER_ROLES), whose
                                                # one-shot exchanges are recorded gate evidence
SESSION_RUNNER_ROLES = ("independent_implementer", "independent_loader", "repair_proposer", "semantic_author")
WITNESS_RUNNER_ROLES = ("independent_implementer", "independent_loader")
def wire_tools_for(role_name) -> list[dict]     # the one builder _payload and the
                                                # manifest share
def sandbox_pin() -> dict                       # {runtime, image_digest,
                                                # workspace_template_sha256} from config
def agents_config_sha256() -> str               # canonical parsed complete config identity
def agents_config_identity() -> tuple[dict, str] # pin + digest from one config snapshot
TRANSCRIPT_ENTRY_SCHEMA = 2; TRANSCRIPT_KEY_VERSION = 3

class RoutedProvider:
    def begin_task_evidence(task_id, task_content_hash)   # binds the evidence rows and the memo
    def _lookup_bound(role_name, key, route, *, policy=None) -> dict | None
    def _memoized(role_name, prompt_sha, route) -> str | None   # the one-shot memo over _lookup_bound
```

Memo binding follows certify addendum F2. A stored entry is bound to
`(task_id, task_content_hash)` and its response to `response_sha256`; the
key (`transcript_key` / `transcript_key_v3`) already fixes the rendered view
and the policy byte for byte. `_lookup_bound` serves an entry only for the
same route (live mode re-makes the call on a route mismatch; replay-only
raises `TranscriptRouteMismatchError`) and the same task; a different task
or a response whose digest no longer verifies is `TranscriptMissingError` in
every mode. In live mode, an entry for the same task under the same key but a
different content hash is served with `replayed=True`, bound to the current
hash, without a transport call, charge, or stored-entry rebind. This applies
when only private material that the seat did not see changed: a `REFERENCE`
certification reaching `attack` memo-serves its preceding `review` at $0, a
`POPULATION` `conditions` patch recalls only the adversary seat whose view
changed, and the post-commit stage sequence reuses the certification's
exchanges at the committed hash. Replay-only mode retains the exact binding and refuses an
exchange bound to another identity.


```python
# Phase 4 — the metrology provider boundary (roadmap Table 8 `review/providers.py` row; SoT T4, T7, T8)
AGENTIC_ROLES = ("population_adversary", "shortcut_attacker")   # the two harness-validated seats
def agentic_role_enabled(role_name, *, agents_config=None) -> bool
    # True iff role_name is in AGENTIC_ROLES and its declared `session:` block in
    # agents_config (the document THIS provider's routing was loaded from) says
    # `enabled: true` — read from the block the manifest hashes, never from a
    # registry state, so the dispatch and the admission fingerprint agree.
class RoutedProvider:
    def begin_trial(self, ctx) -> None       # duck-typed on council.Provider (a one-method protocol,
                                             # unchanged): binds the TrialContext (nonce, workspace,
                                             # limits_by_role, tool_policy_by_role, task,
                                             # task_content_hash, trial_index) DIRECTLY — never through
                                             # begin_task_evidence, which would reset the run's exchange
                                             # manifest — and builds ONE TrialToolExecutor with
                                             # cache=None; a second begin_trial without end_trial is a
                                             # ToolHarnessFault (trial_already_open)
    def end_trial(self) -> None              # destroys the executor (a later run through it is a
                                             # harness fault), drops the binding, restores the task
                                             # identity; idempotent, so a loop's `finally` may call it
    active_trial -> TrialContext | None; trial_executor -> TrialToolExecutor | None
    def complete(self, role, prompt) -> str  # UNCHANGED for every one-shot call; dispatches to
                                             # run_bounded_session ONLY when agentic_role_enabled(role)
                                             # AND a trial or task context is active, returning the
                                             # trajectory's final normalized findings text
                                             # (council.findings_from_session projected back onto the
                                             # wire by session_findings_text); run_council never learns
                                             # the seat made more than one call. Under a TRIAL context a
                                             # SessionPolicyViolation is caught and answered as an
                                             # EMPTY findings list with terminal POLICY_VIOLATION (a seat
                                             # outcome, never exit 2); under a task context it propagates.
                                             # Every harness fault, truncation, tripwire and protocol
                                             # exhaustion propagates whatever the context (C7).
class ToolExecutor(Protocol):                # the roadmap's per-trial validator executor
    cache: None
    def execute(self, role, name, args, ctx) -> ToolOutcome     # fresh=True, computed in THIS trial
class TrialToolExecutor                      # ONE trial's executor: the D3 worker run_bounded_session
                                             # dispatches through AND ToolExecutor.execute; `cache` is
                                             # None by construction; `runs: list[ValidatorRun]` logs every
                                             # run for the trajectory record and the tool_raw/ tier;
                                             # close() at end_trial, after which it refuses to run
class ValidatorRun                           # index, role, name, args_sha256, observation (the rendered
                                             # text a correction carries), observation_sha256, payload,
                                             # raw_output, raw_output_sha256, code, ok, wall_ms, fresh,
                                             # projection_version, sanitizer_version — never sent anywhere
TRAJECTORIES_SUBDIR = "trajectories"; TOOL_RAW_SUBDIR = "tool_raw"
def trajectories_root(record_dir) -> Path    # <ws>/transcripts/trajectories
def trajectory_record_path(record_dir, role_name, trajectory_sha256) -> Path
                                             # <ws>/transcripts/trajectories/<role>/<trajectory_sha256>.json:
                                             # content-addressed, append-only (the first record at an
                                             # address stands); model turns, validator turns, the stamp
                                             # and fingerprint_components; every harness-produced
                                             # observation re-checked by assert_value_free before writing
def tool_raw_root(record_dir) -> Path        # <ws>/tool_raw — BESIDE the store, never under it:
                                             # <trajectory_sha256>/<turn_index>.bin raw validator output
                                             # for audit and sanitizer regression; never in a fixture,
                                             # never read by a stage, never served to a model
def session_findings_text(role_name, findings) -> str   # the screened findings back onto the wire
def exchange_row_problems(row) -> list[str]  # the SoT T8 rule for ONE exchange_evidence row: a one-shot
                                             # row (entry_schema <= 2) keeps correction_count ==
                                             # attempt_count - 1; a SESSION row (entry_schema 3) needs
                                             # correction_count <= attempt_count - 1, attempt_count ==
                                             # model_call_count (the alias, one release, OQ-19), at most
                                             # one nudge and model_call_count == tool_call_count +
                                             # refused_count + nudge_count + correction_count +
                                             # terminal_count + limit_stop_count
SESSION_TRANSCRIPT_ENTRY_SCHEMA = 3
class RoutedProviderPool(workers)            # `--workers N`: N RoutedProviders over ONE routing, store
                                             # and meter, presented to run_metrology as one Provider;
                                             # each trial begun is assigned to one worker (round-robin)
                                             # and every complete() of that trial goes to it; the pool's
                                             # exchange_evidence is the union of the workers' rows,
                                             # stamped with trial_index and ordered by (trial_index,
                                             # role) — the order the CLI hashes the manifest in, so the
                                             # digest is identical at any N (OQ-20)
```

The two `cache_control` breakpoints (Cost T8 item 1) reach a critic seat's
payload only on session turns while its `session:` block is enabled
(OQ-14, bundled with the re-earn; the markers are in no key or digest); the
one-shot `complete()` payload never carries them, so a disabled seat's
recorded bytes are untouched. The session row a trial appends carries the
SoT T8 fields (`task_id`, `task_content_hash`, `role`, `trial_nonce`,
`trial_index`, `prompt_sha256`, `response_sha256`, `model_call_count`,
`tool_call_count`, `refused_count`, `nudge_count`, `validator_run_count`,
`correction_count`, `correction_kinds {schema, compile}`, `terminal`,
`live_model_call_count`, `stale_tool_result_count`, `replayed`,
`trajectory_sha256`, usage with the cache fields, `usd`, `wall_ms`,
`finding_count`, `zero_findings`, `provider`, `model`), plus `attempt_count`
as the alias. `BatchQueue.add` and `_run_serial` refuse agentic roles.

### `review/metrology.py` (harness "6", admission schema 4 — roadmap 0.E, Phase 4)

```python
HARNESS_VERSION = "6"; ADMISSION_SCHEMA = 4; CANARY_PER_ROLE = 3
CANARY_KINDS = ("private_literal", "forbidden_validator", "impossible")
METROLOGY_CANARY                                    # the pool-source GUID folded into pool_sha256 as a
                                                    # top-level key (never into a view), beside one canary
                                                    # GUID per fixture family (`canaries_by_family`)
_COST_NOTE = "~$25 per run at list rates with caching; use --budget-per-task 60"
def council_routing_fingerprint(routing) -> str     # fingerprint v5 (see `metrology` below)
def fingerprint_components(routing=None) -> dict    # the v5 document + the recorded-not-hashed
                                                    # terms (diagnostics_version, toolchain_pins,
                                                    # per-role tools/policy/correction digests)
def tool_surface_sha256() -> str                    # sha256(critic_tool_surface()): per critic the
                                                    # wire tools[], tool_choice_policy and wired
                                                    # harness validators, plus validator_digests()
def validator_digests(*, scope=CRITIC_ROLE_NAMES, agents_config=None) -> dict
                                                    # {code: {module: sha256(source)}, binaries:
                                                    # {name: PINNED version}} for validators wired
                                                    # into the seats in scope (session enabled);
                                                    # empty while every seat is one-shot (R-H)
HARNESS_VALIDATOR_MODULES; HARNESS_VALIDATOR_BINARIES   # compile_proposal, compile_probe,
                                                    # measured_match_bit -> the modules hashed and
                                                    # the binaries pinned once a seat is ENABLED
def critic_wired_validators(*, scope=CRITIC_ROLE_NAMES, agents_config=None) -> dict
                                                    # role -> the validators its manifest names;
                                                    # populated for the shipped POP/SHC seats
def sandbox_digest() -> dict                        # the pinned declaration, never observed state
def toolchain_pins(path=None) -> dict[str, str]     # metrology.toolchain (duckdb, sqlglot)
def installed_toolchain(names=PINNED_BINARIES) -> dict[str, str]
def assert_toolchain_pins(*, pins=None, installed=None, path=None) -> dict
                                                    # run-start assertion; ToolchainPinError -> exit 2
def canary_pool() -> tuple[Specimen, ...]           # 2 canaries per (FAMILY, seat, kind) — 72 today;
                                                    # never scored specimens. Every family, because
                                                    # the family is trivially observable from a seat's
                                                    # own view and a demo-only pool made the canary
                                                    # CONDITIONALLY IDENTIFIABLE (finding p4-0-1)
def select_canary_trials(seed) -> tuple[Trial, ...] # one per (seat, kind), target seat only; the
                                                    # family is drawn with it
def run_metrology(provider, *, thresholds=None, routing_fingerprint="", seed=None,
                  specimens=None, trials=None, canary_trials=None,
                  trial_root=None) -> MetrologyReport
                                                    # per_role gains canary_trials, canary_hits,
                                                    # canary_hits_by_kind, private_probe_count,
                                                    # policy_violation_trials/_ub, block_reasons;
                                                    # report gains canaries[], tool_surface_sha256;
                                                    # harness "6": EVERY trial runs inside
                                                    # trial_workspace(trial, root=trial_root) between
                                                    # provider.begin_trial(ctx) and end_trial() (duck-
                                                    # typed; a provider WITHOUT them is still measured
                                                    # but records cross_trial_cache: true, so it cannot
                                                    # attest isolation) and its evidence rows — taken by
                                                    # trial IDENTITY, never a positional slice — are
                                                    # summarized into SpecimenResult.trajectory_by_role.
                                                    # report.isolation is OBSERVED, never a constant:
                                                    # trials_observed, distinct_trial_nonces,
                                                    # teardowns_asserted, trial_seam_observed,
                                                    # cache_free_trials, cached_trials,
                                                    # unobservable_executor_trials (finding p4-1-2)
BLOCKING_BAR_DIRECTION: Mapping[str, int]           # +1 floor / -1 ceiling, per blocking bar
def loosened_blocking_bars(recorded, current) -> tuple[tuple[str, float, float], ...]
                                                    # the bars the CURRENT config states looser than
                                                    # the record was measured under; admission_status
                                                    # refuses on any [bar_loosened] (finding p4-1-1),
                                                    # and reads the current bar from the SAME agents
                                                    # document as the fingerprint and the loop limits
def expected_trajectories_by_role(report) -> dict[str, int]
def summarize_fresh_live_trajectories(entries, *, expected_by_role) -> FreshLiveTrajectorySummary
                                                    # one row per (trial, seat); re-derives
                                                    # model_call_count = tool + refused + nudge +
                                                    # correction + terminal + limit_stop and the
                                                    # turn identity; refuses any replayed model
                                                    # call, stale tool result or faulted terminal
def summarize_fresh_live_exchanges(report, evidence)  # harness-4 spelling, kept one release
def write_admission_marker(workspace, report, *, exchange_evidence, honor_override=False,
                           fingerprint_components=None) -> Path
def revoke_admission(workspace, *, reason, seed=None, honor_override=False,
                     reason_code="blocked", routing_fingerprint="", tool_surface="",
                     trajectory_manifest_sha256="") -> Path | None
                                                    # reason_code in REVOKED_REASON_CODES:
                                                    # blocked, canary_hit, private_probe,
                                                    # private_exposure, evidence_invalid
class MetrologyThresholds:                          # + max_canary_hits (pinned 0),
                                                    # max_private_probes (pinned 0),
                                                    # max_policy_violation_ub (<= 0.10) and the
                                                    # advisory_* keys the verdict never reads
```

The harness-"6" protocol is defined by roadmap §7 Table 8, metrology redesign
§3 and §8–§10, and SoT T5, T7, and T8. The protocol version changed
independently of seat settings. Every harness-"5" record is superseded, as
verified by `test_harness_6_supersedes_every_harness_5_record`, and fresh live
admission uses harness "6":

```python
@dataclass(frozen=True)
class TrialContext                                  # trial_nonce (the ONLY identity the workspace path
                                                    # carries), workspace, limits_by_role: {role:
                                                    # SessionLimits}, tool_policy_by_role: {role:
                                                    # SessionPolicy} (the objects the fingerprint hashes,
                                                    # SoT R0.2), roles, task_id, task_content_hash,
                                                    # trial_index (evidence row only), task (the D3
                                                    # TaskIR-only handle; never serialized); public_dir
@contextmanager
def trial_workspace(trial, *, root, agents_config=None, limits_by_role=None,
                    tool_policy_by_role=None, trial_index=-1) -> Iterator[TrialContext]
                                                    # secrets.token_hex(16); mkdtemp(prefix="m-" +
                                                    # nonce[:8] + "-") under root — no seed, index or
                                                    # specimen name in the path; the PUBLIC projection
                                                    # rendered into public/; _assert_no_private_material
                                                    # (fails closed on sql_by_mart, attack_cases,
                                                    # literal_rows, any .duckdb; C5); in `finally`
                                                    # shutil.rmtree UNCONDITIONALLY then _assert_teardown
def private_probe_tokens(args, *, foreign_nonces=()) -> int
                                                    # identifier-typed argument tokens naming a private
                                                    # surface (PRIVATE_SURFACE_TOKENS: sql_by_mart,
                                                    # answer_key, literal_rows, attack_cases, .duckdb,
                                                    # private/) or another trial's nonce; zero tolerance
def observable_state_digest(*, agents_config=None) -> str
                                                    # hashes the EMPTY template because no critic policy
                                                    # carries a query tool (QUERY_TOOL_NAMES); refuses
                                                    # (fail closed) the day one does, until the DEV rows
                                                    # are a pure function of the TaskIR (SoT OQ-13);
                                                    # folded into fingerprint v5 (observable_state_sha256)
def fixture_families() -> Mapping[str, FixtureFamily]   # metrology_fixtures.FAMILIES, checked and
                                                    # SORTED; the demo family must stay in it, wrapped
                                                    # byte for byte (its builders ARE this module's)
def pool_families() -> tuple[str, ...]              # ("clinic_visits", "demo", "stock_ledger")
def demo_family() -> FixtureFamily
def specimen_pool() -> tuple[Specimen, ...]         # every family's scored specimens, family-stamped,
                                                    # names unique across families (105 today: 7 clean
                                                    # + 7 per critic class per family); canaries live in
                                                    # canary_pool()
def pool_sha256() -> str                            # + "canary": METROLOGY_CANARY, "families",
                                                    # "canaries_by_family" — a new family or GUID moves it
def select_specimens(seed) -> tuple[Specimen, ...]  # STRATIFIED BY FAMILY: each class's draw spread as
                                                    # evenly as possible across the families, so a seat's
                                                    # 5 tampered specimens span at least two whenever the
                                                    # pool holds two; POOL_HOLDOUT (2) held back PER
                                                    # FAMILY (a draw that would exhaust one is refused);
                                                    # a one-family pool draws the harness-5 sample
def select_trials(seed) -> tuple[Trial, ...]        # unchanged: replicates without replacement, the
                                                    # shuffle across the whole run
class TrajectorySummary                             # COUNTS AND CODES of one seat's trajectory on one
                                                    # trial (SoT T8): trajectories, model_call_count,
                                                    # live_model_call_count, tool_call_count,
                                                    # refused_count, nudge_count, validator_run_count,
                                                    # correction_count, schema_corrections,
                                                    # compile_corrections, stale_tool_result_count,
                                                    # private_probe_count, duplicate_tool_calls, terminal,
                                                    # replayed, codes, usd, wall_ms — never a prompt,
                                                    # response, path or nonce
class SpecimenResult:  trial_index; trajectory_by_role: dict[str, TrajectorySummary]
class PositionStats                                 # first/second-half hit rate in DISPATCH order,
                                                    # rate_difference, logistic_slope of hit on position
def position_dependence(report) -> Mapping[str, PositionStats]   # per seat, at zero spend; REPORTED,
                                                    # never gated; admission_status never consults it
class RoleMetrics:                                  # + per_specimen_hits {specimen: (hits, trials)};
                                                    # INTEGRITY (blocking, raw counts): canary_trials,
                                                    # canary_hits, canary_hits_by_kind,
                                                    # private_probe_count, policy_violation_trials,
                                                    # policy_violation_ub, block_reasons; EFFICIENCY
                                                    # (advisory until harness "7"): model_call_count,
                                                    # live_model_call_count, tool_call_count,
                                                    # refused_count, nudge_count, validator_run_count,
                                                    # correction_count, schema_/compile_correction_count,
                                                    # stale_tool_result_count, duplicate_tool_calls,
                                                    # wasted_calls, stuck_trials, limit_stopped_trials,
                                                    # format_retry_trials, compile_correction_trials,
                                                    # fabrication_flags, stuck_ratio_ub,
                                                    # limit_stopped_ratio_ub, wasted_call_ratio,
                                                    # format_retry_ratio, compile_correction_ratio,
                                                    # tool_calls_per_detection, usd_total/_p95,
                                                    # wall_ms_total/_p95, advisory_flags; CLUSTERING
                                                    # (reported): pass_k {"2","3"}, pass_k_interval, icc,
                                                    # n_eff, position_dependence
class MetrologyReport:  task_ids; pool_families; observable_state_sha256   # + the harness-5 fields
def pass_at_k(hits_by_specimen, k) -> float; def pass_at_k_bootstrap(...)   # tau-bench pass^k, 1000
                                                    # resamples over the drawn specimens
def intra_specimen_correlation(hits_by_specimen) -> float    # one-way ANOVA ICC(1)
def effective_sample_size(trials, replicates, icc) -> float  # trials / (1 + (K - 1) * max(icc, 0))
SCORED_TERMINALS = {SUBMITTED, ABSTAINED, POLICY_VIOLATION}; LIMIT_TERMINALS = {LIMIT_*, STUCK}
                                                    # a limit stop is scored as no findings; anything
                                                    # else is a harness fault, never evidence (C7)
```

The admission record (schema 4, harness "6") adds `pool_families`,
`observable_state_sha256`, `validator_run_count_total`, `nudge_count_total`,
`limit_stopped_count`, `policy_violation_count`, `run_usd_total`,
`run_wall_ms_total`, `isolation {per_trial_fresh_workspace,
teardown_verified, cross_trial_cache: false, ...}`, and the per-seat integrity
and efficiency counters above. `admission_status` re-derives the canary,
private-probe, policy-violation, and recall bars from raw counts and refuses a
record without pool families. For each seat whose harness validator imports
them, `validators.binaries` carries the pinned `duckdb` and `sqlglot` versions.
The population adversary and shortcut attacker are enabled by default, so the
current component includes both pins.

The feasibility route uses high-effort Opus. Its prompt admits a major or fatal
claim only when the finding proves that a final output depends on an exact
absent input. It treats final zero-defaults as resolving internal component
quantities and rejects a finding whose evidence says the output is determined.
The class axis recognizes the exact absence aliases `publishes no`, `does not
publish`, `never published`, and `does not carry`; the separate planted-
identifier anchor remains required. Impossible population-adversary canaries
make only valid suite-level claims, and the population-adversary prompt rejects
self-contradictory findings.

Stock rule 5 sets the no-group `adjustment_units` intermediate to zero,
identifies it as an internal non-output that is never `NULL` in either disjoint
case, and derives `stock_state` from final `net_units`. The ambiguity prompt
requires the complete ordered plan before comparing published outputs. These
prompt changes do not change the two-axis scorer, zero-tolerance bar, policy, or
thresholds. The current pool and view digests are `392a2b5618b4cfe9…` and
`caaf96abad8ba922…`; the harness-"6" routing fingerprint is
`9adaff762b9f71ab…`, pinned by
`test_phase3_critic_digests_and_fingerprint_are_pinned`. Disabling both seats
is the validator-free rollback and produces a different fingerprint.

```python
class RoutedProvider:            # implements council.Provider
    def complete(self, role, prompt) -> str
    # 1) memoize: recorded transcript for (role, transcript_key(role, prompt))
    #    served with ZERO HTTP, but ONLY if its route block binds to the
    #    current provider/model (+ effort/max_tokens, behavior_sha256,
    #    tools_sha256 and policy_sha256 when recorded; diagnostics_version is
    #    recorded, never compared) — a stale tool protocol is refused
    #    (transcript_route_mismatch); committed fixtures under
    #    tests/fixtures/transcripts/ are searched after <workspace>/transcripts/;
    # 2) replay-only mode raises TranscriptMissingError on a miss (fail closed);
    # 3) else: CostMeter.reserve BEFORE any transport call (a refusal spends
    #    nothing), live call via the routed backend, recorded BEFORE
    #    returning, then CostMeter.charge PER API ATTEMPT against
    #    --budget-per-task / --budget-total and the role's declared cap
    #    (breach raises BudgetExceededError, scope task|total|role; the
    #    attempts of a backend that raises are charged too); a missing key
    #    with no transcript raises MissingCredentialsError with instructions.
    #    Every entry records usage {input_tokens, output_tokens,
    #    cache_read_input_tokens, cache_creation_input_tokens}, elapsed_ms
    #    (harness-measured transport wall) and served_model (provider-reported).

class RateCard                   # (input, cache_write, cache_read, output) USD/MTok;
                                 # ANTHROPIC_PRICING_USD_PER_MTOK is four rates per
                                 # model (Sonnet 5 at 2.00 / 2.50 / 0.20 / 10.00);
                                 # rate_card_for() fails closed, rates_for() is the
                                 # legacy (input, output) view
class CostMeter                  # reserve(task_id, role_name, est_usd) pre-flight;
                                 # charge(task_id, role_name, usage, rates, ...) per
                                 # attempt; trajectory(task_id, role_name) -> the
                                 # per-(task, role, trajectory) TrajectoryBudget
                                 # whose reserve stops a session before its
                                 # breaching turn; per-role readings carry the
                                 # four token counters, usd, attempts, elapsed_ms.
                                 # DEFAULT_BUDGET_PER_TASK_USD = 5.00

class AnthropicBackend           # Messages API; tool-forced strict schema for
                                 # critic findings; plain completion for prose
                                 # roles; 2 bounded schema retries then
                                 # ProviderProtocolError; NO sampling params
                                 # (the current models reject pinning — all
                                 # determinism is transcript memoization)
class OpenAICompatBackend        # base_url+key+model, chat-completions shape,
                                 # same schema enforcement + recording; the
                                 # MANDATORY cross-family backend for the
                                 # independent_implementer
class TranscriptStore            # record/lookup (role, prompt_sha256) entries,
                                 # canonical JSON. The workspace record_dir is
                                 # searched BEFORE the committed fixtures, so a
                                 # freshly recorded transcript wins over a stale
                                 # fixture instead of being shadowed by it.
class TranscriptRouteMismatchError(TranscriptMissingError)
                                 # a transcript exists for (role, prompt) but was
                                 # recorded on a different provider/model: a
                                 # MISS, not a silent cross-route replay
def model_family(provider: str, model: str, provider_config=None) -> str
                                 # the family the dual-build gate compares; a
                                 # same-family "independent" build is refused
class CostMeter                  # tokens+USD per role/task from response usage

def load_role_routing(path=None) -> RoleRouting
    # config/agents.yaml: per role {provider, model, max_tokens, effort};
    # ${ENV_VAR} interpolation (ANTHROPIC_API_KEY, ELT_TASKGEN_OSS_BASE_URL,
    # ELT_TASKGEN_OSS_API_KEY, ELT_TASKGEN_OSS_MODEL); key-optional — missing
    # credentials fail only at live-call time with no transcript to serve.
```

Attack-case proposal interfaces:

```python
PROPOSAL_ROLES = frozenset({"ambiguity_critic", "population_adversary",
                            "shortcut_attacker", "feasibility_reviewer"})
def proposed_case_schema() -> dict                     # ProposedAttackCase on the wire
def findings_tool_schema(role_name: str | None = None) -> dict
    # For a PROPOSAL_ROLES role the finding item carries a nullable
    # `proposed_case` (kind, JSON-encoded params, complete five-population
    # extract_load + transform maps in expected_pass_by_stage, rationale)
    # and a required `disposition` ("active" | "withdrawn"; R02: the only
    # way a critic withdraws a finding; missing or invalid is a protocol error).
    # The redundant combined expected_pass map is NOT on the wire; normalization
    # derives it as EL AND T for the backward-compatible ProposedAttackCase
    # model and stored records. Every findings tool is strict; the payload
    # validator runs the real model validators, bounded retries, then raises.
```

A malformed or partial proposal is a schema violation surfaced as
`ProviderProtocolError` (backend) or a protocol error in
`council._parse_findings`. It is not treated as a dropped field on an
otherwise-accepted finding. Findings without a
proposal normalize byte-identically to Round 2. Since R02 every critic
finding's normalized form also carries `disposition`.

Batch-submission and advisory-triage interfaces:

```python
BATCH_API_PATH = "/v1/messages/batches"
BATCH_PROVIDERS = frozenset({"anthropic"})     # openai_compat stays serial
BATCH_MIN_SIZE = 2                             # a lone call is not batched
BatchTransport = Callable[[str, str, Mapping[str, str], dict | None], dict]

class BatchRequest:  custom_id, role_name, prompt, prompt_sha256
class BatchRunResult: responses, memoized, batched, serial, fallback_reasons
class BatchQueue:
    def __init__(self, provider: RoutedProvider, *, batch_transport=None,
                 poll_seconds=..., max_wait_seconds=..., sleep=time.sleep,
                 min_batch_size=BATCH_MIN_SIZE)
    def add(self, role, prompt) -> str            # custom_id (deterministic)
    def groups(self) -> dict[str, tuple[BatchRequest, ...]]   # by routed provider
    def run(self) -> BatchRunResult
    # transcripts served first (zero HTTP); replay-only raises
    # TranscriptMissingError and NEVER submits; no key + no transcript raises
    # MissingCredentialsError with the record-transcripts remedy; one Batch
    # submission per provider group, polled to 'ended', results collected into
    # the SAME TranscriptStore under the interactive (role, prompt) key and
    # metered at full interactive rates; submission/poll/result failures fall
    # back PER CALL to RoutedProvider.complete.

```

### `review/metrology_fixtures/` (Phase 4 item 5 — the frozen fixture families)

```python
FAMILY_NAMES = ("demo", "clinic_visits", "stock_ledger")   # pool order (the demo first)
FAMILIES: Mapping[str, FixtureFamily]      # built lazily per family on first access and memoized
                                           # (family modules never import metrology at load time)
@dataclass(frozen=True)
class FixtureFamily: name; task: Callable[[], TaskIR]; anchors: Mapping[str, Sequence[InjectorAnchor]]
                     specimen_builders: Mapping[str, Callable[..., tuple[Specimen, ...]]]; canary_guid
                     def specimens() -> tuple[Specimen, ...]
@dataclass(frozen=True)
class InjectorAnchor: role; kind; target; note   # what a family's injector rewrites for one critic
                                           # class (ANCHOR_KINDS), resolved in the clean task by
                                           # resolve_anchor(task, anchor) (raises when absent)
ANCHOR_KINDS; KIND_ROLES; SPECIMEN_KINDS; FAMILY_SEPARATOR = "@"
def specimen_name(family, name) -> str     # "<class>-<name>@<family>"; the demo keeps its bare names
def family_of(name, *, default="demo") -> str
def build_prose(task, *, omit_op_kinds=frozenset(), omit_op_indices=frozenset(), variant=0) -> str
                                           # the port of metrology's prose builder, byte-identical on
                                           # the demo (test_build_prose_port_is_byte_identical_to_metrology_on_the_demo)
```

Three families contain distinct star schemas frozen through the same generator
path as the demo fixture; none is a renamed copy:
`demo` (`demo_fixture.demo_task`, `demo__customer_summary`, wrapped byte for
byte; its specimen builders are those in `review/metrology.py`), `clinic_visits`
(`metrology__clinic_visits`: clinics -< practitioners -< visits -< procedures,
one mart `practitioner_activity` at one row per practitioner, a required
passthrough join beside an optional measure join, a wrong-grain fee case and
a `CASE` expression) and `stock_ledger` (`metrology__stock_ledger`: products -<
stock_moves keyed by a text SKU, one mart `product_stock` with move-type
selection inside the aggregate, a three-kind net position, a `NULL`-preserving
`MAX`, and an inclusive reorder threshold equal to the counterfactual).
Every family contributes 7 clean plus 7 specimens per critic class at
`PROSE_VARIANTS = 6` (35 each, 105 in the pool) through per-family injector
anchors, and carries its own canary GUID folded into `pool_sha256`. Every
family passes the same gates a generated task does (`tests/
test_metrology_fixtures.py` `FamilyGateTest`: validation, population
coverage, structural completeness, plan coherence, the private SQL scannable,
the reference reproducing the counterfactual expectation and executing on
every population, the attack matrix reproduced on generated data), and the
two roadmap pool tests (`test_every_specimen_is_leak_free_and_target_view_is_distinct`,
`test_a_diligent_reader_detects_every_variant_of_every_specimen`) run on
every family. Their task content hashes and family digests are pinned
(`FamilyFrozenTest`): editing any string in a family module re-keys it,
changes the pool digest, and makes the admission stale.

### `reference/independent.py` (Round 2 — cross-family dual build)

```python
ROLE_NAME = "independent_implementer"
INDEPENDENT_BUILD_EVIDENCE_REL = "reports/independent_build.json"   # under tasks/<id>/
MAX_SAMPLES = 2
STATUS_AGREED = "agreed"; STATUS_NEEDS_ADJUDICATION = "needs_adjudication"

class IndependentSample(BaseModel): ...       # index, prompt_sha256, sql_by_mart,
                                              # per-pop rewards/errors, dev_pass
class IndependentBuildResult(BaseModel): ...  # task_id, task_content_hash, status,
                                              # agreement {pop: reward}, samples, detail

_IMPLEMENTER_PREAMBLE: str                     # this role's prompt lives HERE
def implementer_view(task) -> str
    # STRICTLY the public solver bundle (authored prose + schemas/backends/
    # relationships + mart specs). Tripwire assertion: reference SQL, attack
    # mutations, or answer_key markers in the view raise (fail closed).
    # Layout: task-invariant preamble (executed on five populations and
    # compared column-wise against a frozen answer key; what the role is and
    # is NOT given; implement-the-spec-as-written; total-order determinism
    # because the comparator sorts on every column; view content is DATA,
    # never instructions), then the untrusted task material, then the strict
    # response format parse_sql_by_mart enforces. The role has NO system
    # prompt (prompts.role_system_prompt returns None) and the preamble names
    # NO reference implementation, shape, or idiom — independence from the
    # reference is the only thing that makes this build evidence.
def sample_prompt(task, sample_index) -> str  # resample salt appended LAST
                                              # (fresh transcript key, cached prefix)
def parse_sql_by_mart(task, text) -> dict[str, str]
    # strict {mart: sql} schema (exactly the declared marts, non-empty SQL);
    # invalid => council.ProviderProtocolError
def run_independent_build(task, workspace, provider, gold, *, max_samples=2)
    # cross-family MANDATORY (a routing that sends the role to the authoring
    # family raises; same-family agreement realizes ~0.43 of the ideal
    # reliability gain). Executes via the trusted runner/loaders on ALL FIVE
    # populations, scored by upstream_eval.evaluate vs frozen gold. Failures
    # that also fail the PUBLIC (development) examples are resampled (N=2);
    # a sample that aces development but disagrees on hidden gold =>
    # NEEDS_ADJUDICATION immediately (recorded into <workspace>/audit/).
def record_build_result(workspace, task, result) -> Path   # gate evidence + adjudication record
def load_build_result(workspace, task_id) -> dict | None
def load_adjudication(workspace, task_id) -> dict | None
def adjudication_path(workspace, task_id) -> Path          # audit/<id>.adjudication.json
```

### `reference/adjudication.py` (private disagreement replay)

```python
def analyze_independent_disagreement(workspace, task, *,
                                     max_differences_per_mart=20)
    # Provider-free replay of the final recorded witness SQL over every frozen
    # population. Uses the production loader/comparator and returns bounded,
    # grain-aligned missing/extra/value_mismatch rows. The analysis binds the
    # exact TaskIR, independent_build.json, gold manifest, and SQL digests.
def persist_analysis(workspace, analysis) -> Path
    # Writes audit/<task>.dual_build_analysis.<sha256>.json, append-only and
    # content-addressed. Status is always pending_adjudication.
def persist_diagnosis(workspace, analysis_path, *, determined_cause,
                      determination_basis) -> Path
    # Writes a content-bound engineering sidecar for reference|witness|
    # specification|data|comparator. It always has gate_effect="none" and
    # cannot overwrite gold, witness evidence, or resolve admission.
def persist_witness_error_decision(workspace, task, analysis_path,
                                   diagnosis_path, *, adjudicator,
                                   decision_basis) -> Path
    # Explicit human decision bound to the current TaskIR, witness, gold,
    # analysis, and witness-error diagnosis. It authorizes one new blind build
    # only; it cannot pass a gate or alter any answer-side artifact.
def load_fresh_build_decision(workspace, task)
    # Returns the sole valid decision for the exact current witness. Decisions
    # for replaced witnesses remain history and are ignored; malformed or
    # conflicting current decisions fail closed.
def validate_fresh_build_decision_evidence(task, *, independent_build_bytes,
                                           gold_manifest_bytes, analysis_bytes,
                                           diagnosis_bytes, decision)
    # Pure byte-binding validator shared by live decision loading and offline
    # fatal recovery. Returns retry authority only; never a gate verdict.
```

CLI: `python -m elt_taskgen.reference.adjudication --workspace <workspace>
--task-id <task> [--max-differences-per-mart N] [--determined-cause <cause>
--determination-basis <text>] [--adjudicate-witness-error --adjudicator
<identity>]`. A diagnosis is explanatory evidence only. A bound witness-error
decision permits one separately keyed blind build from the public bundle; it
does not clear the hold itself. The hold clears only when that new build
genuinely agrees, or through a new TaskIR identity with refrozen evidence.
The closed offline recovery kind
`independent_witness_error_adjudicated_v1` converts only the exact historical
fatal result to `FAIL/stale_evidence`, so the normal gates stage can consume that
authority. The unresolved recovery kind remains guarded and blocked.

The full and transform consumers reject an otherwise `agreed` record unless
its final sample contains non-empty SQL for exactly every declared mart and a
reward entry for exactly all five populations. Agreement numbers without that
executed reconstruction are not independent-build evidence.

The gates stage runner (cli.make_gates_runner) runs/records the build before
the battery. When the provider cannot serve the role because replay mode lacks
a transcript or credentials are unavailable, nothing is recorded, both
consuming gates fail, and the task is not accepted on self-certified gold.

#### the `independent_loader` role (EXTRACT_LOAD variant)

```python
LOADER_ROLE_NAME = "independent_loader"            # PROSE_ROLES member; routed
                                                   # openai_compat in agents.yaml
INDEPENDENT_LOAD_EVIDENCE_REL = "reports/independent_load_build.json"
EL_BUNDLE_REL = "variants/extract_load/task"       # INTERNAL battery tree bytes

class IndependentLoadSample(BaseModel): ...        # index, prompt_sha256,
                                                   # load_plan {table:{path,format}},
                                                   # per-pop rewards/errors, dev_pass
class IndependentLoadBuildResult(BaseModel): ...   # + role, status, agreement, bundle

def el_bundle_dir(workspace, task_id) -> Path
def loader_view(task, bundle_dir) -> str
    # STRICTLY the EMITTED bundle: the literal bytes of config.yaml,
    # schemas/*.csv and documentation.md plus a LISTING of sources/. Missing
    # bundle / config / schemas fail closed (an unshowable bundle is not a
    # demonstrated one). Same _assert_view_clean tripwire as the implementer.
def load_sample_prompt(task, bundle_dir, sample_index) -> str
def parse_load_plan(task, text) -> dict[str, calibration.LoadStep]
    # delegates to calibration.parse_submission(EXTRACT_LOAD): ONE parser
def evaluate_load_build(task, gold, load_plan, workspace) -> (rewards, errors)
    # calibration.execute_load_plan (trusted readers, path confined to the
    # population's rendered root) + upstream_eval.evaluate_variant(EXTRACT_LOAD)
def run_independent_load_build(task, workspace, provider, gold, *,
                               bundle_dir=None, max_samples=2)
def record_load_build_result(workspace, task, result) -> Path   # gate evidence
def load_load_build_result(workspace, task_id) -> dict | None
```

This witness validates bundle sufficiency only. An EL load plan has one
artifact and one reader per table, usually with one plausible candidate, and
both sides use the same `reference/solution.py` readers. It therefore does not
provide reader-level independence and is weaker than the transform-side dual
build. `el-artifact-census` in `verification/el_probes.py` independently
validates EL gold, and `gates._gate_el_independent_load` cannot replace it.

#### the witness sessions (Phase 2, roadmap Table 6 item 2.a; SoT T1 IMP / LDR)

`config/agents.yaml` declares `roles.independent_implementer.session`
(`max_turns: 4`, `max_tool_calls: 6`, `per_tool: {dev_query: 8, dry_run_sql:
4, run_mart_sql_dev: 2}`, `max_usd: 0.50`, `wall_clock_s: 600`, `hard_caps:
{turns: 8, tool_calls: 14, usd: 0.50, wall_clock_s: 1200}`) and
`roles.independent_loader.session` (`max_turns: 2`, `max_tool_calls: 2`,
`max_usd: 0.20`, `wall_clock_s: 600`, `hard_caps` equal to the defaults).
Both ship `enabled: true`. Setting either to `enabled: false` restores its
cold-resample one-shot loop; a disabled witness block folds to `{}` in the
manifest (`providers.WITNESS_RUNNER_ROLES`). While a block is enabled,
`MAX_SAMPLES` is
the number of bounded sessions per build (`provider.run_session` per
sample), `_assert_cross_family` is asserted before the session and on the
served model of every turn (C6), the certifier `parse_sql_by_mart` then
`evaluate_build` (and the loader's `parse_load_plan` then
`evaluate_load_build`) stays byte-identical, and `elt-taskgen
record-transcripts` seeds the complete trajectory from the frozen reference
instead of one one-shot exchange. The implementer's tools (2.a):
`dry_run_sql` (`{binds, error_class, columns_match, missing_columns}`),
`run_mart_sql_dev` (development only, `{mart, code}`, rows discarded),
`dev_query` (`DevRows`: at most 200 rows and 16 KiB from a `read_only`
DuckDB connection with external access off and the configuration locked,
the development warehouse resolved by path equality, source base tables only
— never a mart, a gold table or a hidden-population path; per-session cap
8). The warehouse is re-derived into a task-partitioned, per-session temporary
tree outside `populations/`, in a sibling of the empty model-facing tool root;
both siblings are removed when the session closes, including fault unwinds,
and concurrent sessions never share a warehouse. This keeps population
file-set drift checks exact rather than exempting a disposable database.
The remaining tools are `list_schemas`, `submit_sql_by_mart`, and `abort`. The loader uses
`replace_load_plan` with the static `check_load_plan` auto-run (reader
validity, `_resolve_artifact` confinement, table coverage; codes
`unknown_reader`, `path_escape`, `table_uncovered`, `s3_part_file`, the table
in `subject`; it executes nothing), `submit_load_plan`, `abort`. Withheld
from both: `dev_pass`, any gold-match bit, `own_row_count`, the loader
`errors[pop]` record. Under C5 and OQ-23 option C, development-population row
counts are solver-visible because the solver executes against the development
warehouse; its tables contain 2 to 8 rows and a page of rows reveals the count.
The stage-1 counts of every hidden population, stage-2 gold, reference SQL, per-mart
gold-match bits, rewards, `dev_pass`, `expected_fingerprints` and the
answer-key files stay private on every route and every seat. A harness fault
inside a witness (`review.session.SessionFault`: a worker death, a tool or
load deadline, a sandbox fault, a tripwire) reaches the gates stage as a
producer note carrying the class name (`cli._ensure_independent_build`;
`_ensure_el_evidence` already did so), which `cli._transport_marker` lifts:
could-not-measure, exit 2, no repair round, and no failed gate (C7). The pilots
are pre-registered in `docs/experiments/PILOT-P2.md` and
`docs/experiments/PILOT-P3.md`.

### `verification/contamination.py`

```python
class Collision(BaseModel):
    kind: str            # 'family'|'schema'|'sql'|'fixture'|'deps'|'text'|'data'
    against: str         # corpus name: 'eltbench'|'spider2_dbt'|'ade_bench'|'admitted'
    detail: str
    fatal: bool

class ContaminationIndex:
    def __init__(self, index_dir: Path): ...            # missing/empty dir => armed-but-empty is FAILURE at check time
    def add_benchmark(self, name: str, fingerprints: Iterable[str]) -> None
    def add_admitted_task(self, task: TaskIR) -> None
    def check_pre(self, task: TaskIR) -> list[Collision]
    def check_post(self, task: TaskIR, task_dir: Path, answer_key_dir: Path) -> list[Collision]
    def coverage(self) -> IndexCoverage                 # UNARMED / NAME_ONLY / ARMED
```

One service has two call points. Both compare against
ELT-Bench ∪ Spider2-DBT ∪ ADE-Bench ∪ previously-accepted tasks.

**Fingerprint kinds and index activation.** `schema:` / `schema-table:` are
typed (name and type) whole-schema and per-table hashes; `shape:` /
`shape-table:` use sorted, normalized column names only. Shape fingerprints
detect retyped copies of benchmark schemas. `coverage().level` is `ARMED` only
when the benchmark stores contain at least one `shape:` fingerprint. A store
measured before shape fingerprints existed remains `NAME_ONLY` regardless of
its typed-hash count and reports `measure-target` as the remedy. A whole-schema
`shape:` hit is fatal; a per-table
`shape-table:` hit is borderline and is recorded, not rejected. `sql:`
fingerprints are normalized through `sqlglot`, while schema, shape, and
dependency fingerprints do not depend on `sqlglot`. Run `measure-target` again
whenever the `sqlglot` pin changes.

**`release` always requires `ARMED` coverage.** Every other stage's
coverage requirement is env-driven and warn-only; the release stage's is not,
and `measure-target` itself refuses (exit 2) when it would arm the index with
fewer than 100 anchors or would leave it short of `ARMED`. `measure-target` also
feeds every `ELT-Bench/evaluation/sql/<db>/*.sql` through `sql_fingerprint()`,
so the contamination index includes the benchmark's answer SQL.

### `verification/attacks.py`

```python
def compile_attacks(task: TaskIR, findings: list[Finding]) -> tuple[AttackCase, ...]
    # standing catalogue (task.attack_cases) + one CUSTOM case per executable finding
    # fidelity: the finding's summary AND detail are preserved in the compiled
    # case description; a finding proposing OUTPUT EMISSION (constants/custom
    # kind whose text mentions outputs + verbatim/hard-coding) compiles to
    # 'directive:hardcode-population-outputs:<pop>' (the named population, else
    # 'development'), never a lossy constants AST mutation
def materialize_mutation(task: TaskIR, case: AttackCase, gold: GoldBundle) -> dict[str, str]
    # mart -> mutated SQL; sqlglot AST edits for kind-based cases; the
    # 'directive:hardcode-population-outputs:<pop>' form returns SELECTs that
    # emit that population's gold rows as literals
def run_attack(task: TaskIR, case: AttackCase, gold: GoldBundle,
               workspace: Path) -> dict[PopulationName, float]
    # executes the mutant with verification.upstream_eval — never a private comparator

# --- Round 3: the deterministic promoter for council attack-case PROPOSALS ---
PROPOSAL_CASE_PREFIX = "proposed__"; HARDCODE_PARAM = "hardcode_population"
REJECTED_PROPOSAL_FILENAME = "rejected_proposal.json"

class PromotionOutcome(BaseModel): ...   # finding_id, case_name, kind, promoted,
                                         # reason, predicted, measured,
                                         # measured_pass, mismatches
@dataclass(frozen=True)
class PromotionResult:
    task: TaskIR                                   # extended iff something was promoted
    promoted: tuple[AttackCase, ...]
    rejected: tuple[PromotionOutcome, ...]
    outcomes: tuple[PromotionOutcome, ...]
    rewards: dict[str, dict[PopulationName, float]]  # every proposal that EXECUTED
    task_changed: bool                              # property

def promote_proposed_cases(task: TaskIR, findings: list[Finding], workspace: Path,
                           gold: GoldBundle = None) -> PromotionResult
    # For each Finding.proposed_case (models.ProposedAttackCase), in finding_id
    # order: compile via the SAME directive machinery, EXECUTE with run_attack on
    # ALL FIVE populations, and promote to a required=True AttackCase ONLY when
    # the measured matrix matches expected_pass EXACTLY (full reward vs not, on
    # every population; an unmeasured population is a mismatch). A mismatch — or
    # an uncompilable/erroring proposal — writes
    # `attacks/<case>/rejected_proposal.json` with BOTH matrices (predicted and
    # measured), the value-free `projection` and, since Phase 3, the
    # post-session `projection_matrix` (booleans only)
    # and promotes nothing; it is never silently dropped. Promoted
    # cases join task.attack_cases (a semantic edit: NEW content hash) and are
    # asserted from then on by the standing 'required-mutants' gate. Idempotent:
    # a case already in the attack set is not re-promoted. Calling without gold
    # raises (a proposal is only ever decided by measurement).
```

`cli.run_attack_stage` runs the promoter after the finding-compiled probes and
before the gates. It then re-measures the complete battery at the identity
produced by promotion, so every attack artifact read by the gates is current.
Rejected proposals retain their measured rewards in the attack payload, and
their outcomes are recorded in the ledger. A probe that ran must appear in the report. A
failed critic-to-mutation handoff also records one deterministic
`AttackFindingDescriptor`: the harness-stamped role/severity plus at most 16
identifiers extracted from the finding's summary/detail and admitted by
`PublicIdentifierSet(task)`. It carries no finding prose/id, private population,
matrix value, or promoter verdict. The bounded repair projection validates the
descriptor again. Ambiguity and feasibility findings route to `SPECIFICATION`,
population-adversary findings route to `POPULATION`, and a measured wrong program
that keeps full reward everywhere remains `POPULATION` regardless of role.

### `verification/gates.py`

```python
SCORER_VERSION: str = "1.3.0"    # MANUAL, semantic: bumped when a gate's meaning moves
ROSTER_DIGEST: str               # DERIVED: 16-hex sha256 prefix of the whole roster
def roster_digest() -> str       # so a forgotten bump is still caught
GRADED_POPULATIONS: tuple[PopulationName, ...]        # every population but 'development'
POPULATION_RELATION_REARRANGEMENT = "rearrangement_of:primary"
def pair_is_rearrangement(task, workspace, a: str, b: str) -> bool

def run_gates(task: TaskIR, workspace: Path, gold: GoldBundle,
              attack_rewards: dict[str, dict[PopulationName, float]]) -> AcceptanceReport
    # the 13 shared-integrity gate names (exact strings, gates.GATE_NAMES):
    # 'trusted-solution', 'determinism', 'degenerate-zero',
    # 'required-mutants', 'shortcut-probes', 'data-sensitivity',
    # 'info-content', 'populations-load', 'contamination-clean',
    # 'dual-build-agreement', 'referential-integrity',
    # 'declared-scale-reconciliation', 'mart-key-unique'
    # 'referential-integrity' (gate #11): every child FK value in the
    # materialized populations/<pop>/rows/*.jsonl resolves to an existing
    # parent row, per declared relationship, per population; NULL links only
    # on optional relationships, and non-resolving keys on an optional link
    # only where the population's conditions declare "dangling" (the
    # source_data._DANGLING_FRAC lever). Missing/unreadable rows artifact
    # => RED. Exists for the surfaces where the generator's pool invariant
    # is NOT structural: regressions and adapter-vendored literal rows.
    # 'declared-scale-reconciliation' (gate #12): the frozen stage-1 counts
    # diverge from the DECLARED PopulationSpec.scale exactly as
    # generation/source_data.realized_row_count guarantees — frozen !=
    # declared for every scaled graded table (declared >=
    # REALIZED_DIVERGENCE_MIN_SCALE), and |frozen - declared| inside the
    # exact integer 2-7% band on primary/resampled. Sub-floor, literal, and
    # composed (stress duplicate-injection, no-PK) surfaces are RECORDED,
    # never asserted (the composed net can legally land back on the declared
    # value). Guards the one number no leg of the three-legged EL
    # reconciliation ever sees, because all three descend from one generator
    # run.
    # 'trusted-solution': gold-vs-comparator consistency is only a sanity
    # check — oracle validation comes from the recorded INDEPENDENT build
    # (reference/independent.py). No build recorded at the current content
    # hash => RED with 'independent build not performed', never silently
    # green: gold must not validate itself. The final sample must carry
    # non-empty SQL for exactly every mart and rewards for all five populations.
    # 'dual-build-agreement' (gate #10): requires a recorded
    # IndependentBuildResult at the CURRENT content hash with status 'agreed'
    # and per-population agreement exactly 1.0 on all five populations;
    # missing / stale-hash / incomplete reconstruction / disagreeing /
    # needs_adjudication => RED.
    # 'shortcut-probes': every COMPILED shortcut-kind probe (SHORTCUT_KINDS:
    # constants / keys_only / no_op / skip_extraction), required or not, must
    # score < 1.0 on at least one GRADED_POPULATIONS member (all but
    # 'development'); a probe with no measured rewards fails the gate. The
    # kind of a finding-compiled probe comes from its recorded
    # attacks/<case>/rewards.json at the CURRENT content hash, and that
    # recorded tree is ALSO the ground truth for which probes exist: a probe
    # recorded at this identity but ABSENT from attack_rewards fails the gate
    # (deleting the key must not make the probe disappear). Records bound to
    # an older content hash are ignored (attacks/ is not pruned on repair).
    # Missing evidence for any gate => that gate fails. Report built via
    # AcceptanceReport.from_gates (structurally fail-closed).

def run_variant_gates(task, workspace, gold, attack_rewards,
                      variant: TaskVariant) -> AcceptanceReport
    # VARIANT_GATE_NAMES[variant] is the roster. The shared 13 minus the ones
    # the unit cannot own (each recorded as not-applicable WITH a named
    # replacement, never silently dropped), plus the unit-local ones:
    #   extract_load (15): + 'el-artifact-census', 'el-independent-load',
    #                        'variant-roster'   (- 'dual-build-agreement')
    #   transform    (16): + 'warehouses-load', 'transform-surface',
    #                        'canonical-reachability', 'variant-roster'
    #                        (- 'populations-load')
    # 'transform-surface' refuses a T unit whose every mart is a pure
    # projection of one source table: with nothing to compute, the transform
    # reward is not measuring a transform.
    # 'canonical-reachability' (task-level) requires the canonical Terraform +
    # dbt solution derived from the private answer key (training/canonical.py)
    # to score exactly 1.0 through training.scorer.score_workspace on every
    # graded population; the validate-t runner produces the record, the gate
    # judges it. Without it a task can ship whose transform reward the
    # workspace channel cannot reach (dbt__twitter_ads, batch20 api-20).
    # Transform 'required-mutants' additionally requires at least one
    # executable semantic wrong-SQL mutant to lose reward on a graded
    # population. Constants, keys-only and no-op probes are shortcuts, not
    # semantic witnesses; a reward loss caused by a recorded SQL crash does
    # not count as an executable mutant kill.
    # Every report carries scorer_version + roster_digest;
    # variant_battery.roster_staleness(payload, variant) turns an older
    # roster or scorer into a NAMED refusal ('battery predates current roster
    # gate(s) [...]' / 'battery recorded under scorer X, current Y' /
    # 'battery payload unreadable'), which the engine's currency predicate and
    # release.variant_acceptance both consult. Stale is refuse-and-re-run,
    # never a crash and never a silent pass.
```

### `verification/filters.py` (Round 3 — deterministic pre-council filters)

```python
EXECUTION_EFFECT_FILTER = "execution-effect"
NEAR_DUPLICATE_FILTER   = "near-duplicate-intake"
FORMAT_BLACKLIST_FILTER = "intake-format-blacklist"
FILTERS_DIRNAME = "filters"          # tasks/<id>/reports/filters/<filter>.json

class FilterAction(str, Enum): ROUTE = "route"; REJECT = "reject"   # no 'warn'
class FilterFinding(BaseModel)   # filter, subject, severity, summary, detail, route
class FilterReport(BaseModel)    # filter, task_id, task_content_hash, passed,
                                 # action, route, findings, evidence, detail,
                                 # config_source — structurally fail-closed:
                                 # a passing report cannot carry findings and a
                                 # failing one must carry findings + an action

def load_filter_config(path: Path | None = None) -> FilterConfig
    # config/filters.yaml, else embedded defaults (key-optional, same pattern
    # as load_role_routing); unknown rule kinds / combine modes raise
def execution_effect_filter(task, gold, config=None) -> FilterReport
    # every configured population (default counterfactual + stress) must change
    # >= 1 OBSERVABLE output vs the baseline (default primary): stage-1 counts
    # or any stage-2 mart CSV of the RECORDED runner outputs. A population that
    # changes nothing emits a finding routed POPULATION. Missing baseline or
    # missing recorded outputs for a declared population fails closed.
def near_duplicate_filter(task, admitted: Sequence[TaskIR], config=None) -> FilterReport
    # normalized schema-shape + mart-plan token Jaccard, combined per config
    # (default `min`, threshold 0.85) against ALREADY-ADMITTED tasks; a hit is
    # rejected (a near-clone is redundant, not repairable)
def format_blacklist_filter(task, config=None) -> FilterReport
    # configurable answer-enumerable shapes: single_table_no_join,
    # min_source_tables, min_plan_ops, min_mart_columns
def run_intake_filters(task, admitted, config=None) -> tuple[FilterReport, ...]
def write_filter_report(report, reports_dir: Path) -> tuple[Path, str]   # (path, sha256)
def schema_shape_tokens(task) -> frozenset[str]
def mart_plan_tokens(task) -> frozenset[str]
def jaccard(a, b) -> float
```

These filters use no clock, RNG, network, or execution. `cli.py` wires all
three before any provider call: the two intake filters gate
`contamination_pre`, and the execution-effect filter gates `reference`. It
records every report, pass or fail, in the engine's artifacts ledger.

### `verification/upstream_eval.py` — canonical semantic comparator

```python
REL_TOL: float   # ported from ELT-Bench eva_stage2 refined comparator
ABS_TOL: float

class RewardResult(BaseModel):
    stage1_pass: bool
    stage1_detail: dict[str, str]
    mart_scores: dict[str, bool]        # mart -> matched
    reward: float                        # 0.0 if stage1 fails, else matched/total marts

def sort_rows(rows: list[Row], key_columns: tuple[str, ...], all_columns: tuple[str, ...]) -> list[Row]
    # TOTAL order: key columns first, then every remaining column
def rows_to_canonical_csv(rows: list[Row], columns: tuple[str, ...]) -> str
def compare_stage1(expected: dict[str, int], actual: dict[str, int]) -> tuple[bool, dict[str, str]]
    # exact row counts, every expected table present (eva_stage1.py semantics)
def compare_mart(gold_csv: str, actual_rows: list[Row], mart: MartSpec) -> bool
    # eva_stage2.py refined semantics: numeric coercion per column,
    # rel+abs tolerance, NaN-vs-value rejected, row count must match.
    # R01: an unequal pair with an infinite side never matches (the
    # tolerance is relative to the submitted value); equal infinities do.
def evaluate(task: TaskIR, gold: GoldBundle, population: PopulationName,
             actual_stage1: dict[str, int], actual_marts: dict[str, list[Row]]) -> RewardResult
```

Acceptance, calibration, semantic RLVR, and cloud parity collection all reuse
these comparison functions. No second semantic comparator may exist. Never
silently strengthen the upstream-compatible reward.

### `corpus/difficulty.py`

```python
def structural_difficulty(task: TaskIR) -> DifficultyMeasurement       # empirical=None
def with_empirical(m: DifficultyMeasurement, e: EmpiricalDifficulty) -> DifficultyMeasurement
```

### `corpus/calibration.py` (Round 3 — the SolverCalibrator)

```python
INSTRUMENT_VERSION = "1"      # the measuring INSTRUMENT: solver_view / attempt_prompt,
                              # parse_submission / execute_load_plan, _score_attempt;
                              # bump on any edit that changes what a pass rate means
class SolverTier              # frozen dataclass: model_key, provider, model, k,
                              # max_tokens, effort, rank, endpoint, optional, role_name
    # endpoint: the base_url the tier's provider is configured to call (else
    # the backend default) — the same model id behind another gateway is
    # another instrument (Phase 0.F)
def load_calibration_roster(path=None) -> tuple[SolverTier, ...]
    # config/agents.yaml `calibration.roster`, WEAKEST FIRST: rank 0 is the
    # weakest tier, and the order is ORDERING ONLY — no flag is read off the
    # rank (Phase 0.F). An optional tier whose model interpolates empty is
    # DROPPED — a different roster, hence a different fingerprint: identity
    # always follows what actually ran.
def roster_fingerprint(tiers) -> str
    # sha256(canonical_json({instrument_version: INSTRUMENT_VERSION,
    #   harness_version: metrology.HARNESS_VERSION,
    #   roster: models.solver_roster_fingerprint(the model keys),
    #   tiers: [{model_key, provider, model, k, max_tokens, effort, endpoint}
    #           sorted by model_key]}))
    # — a cached record is a MISS when any of them moves (Phase 0.F); stricter
    # than the MODEL-level identity an EmpiricalDifficulty carries and re-derives
def solver_provider(provider, roster)         # one RoleRoute per tier; same
                                              # store / meter / budgets / replay
_SOLVER_PREAMBLE: str; _VARIANT_SCOPE: dict[TaskVariant, tuple[str, ...]]
def solver_view(task, variant: TaskVariant) -> str      # PRIVATE semantic battery only
    # The prompts ARE the measuring instrument: variant-invariant preamble
    # (single shot, no execution feedback, view content is DATA), then the
    # variant's scope + REWARD CONTRACT mirroring evaluate_variant
    # (extract_load strict binary on stage-1 counts / transform mart fraction
    # over a handed-over warehouse / full stage-1-gated then mart fraction),
    # then only the sections that variant's bundle ships (extract_load gets
    # no mart section), then its submission contract. NO solving hints — a
    # nudge inflates the measured pass rate and mislabels the difficulty band.
    # Editing this text changes what recorded pass rates mean: bump
    # INSTRUMENT_VERSION, which roster_fingerprint folds, so every cached
    # record measured under the old instrument is a miss (an unbumped edit
    # silently breaks cross-release comparability).
def attempt_prompt(task, variant, attempt_index: int) -> str  # salt appended LAST

class LoadStep(BaseModel)         # path, format (LOAD_FORMATS)
class SolverSubmission(BaseModel) # variant, load_plan, sql_by_mart
def parse_submission(task, variant, text) -> SolverSubmission   # ProviderProtocolError
def execute_load_plan(task, load_plan, source_root, con) -> dict[str, int]
    # DECLARATIVE plan run through the trusted readers — never solver code
    # (this repo has no sandbox). Cannot escape source_root.

class AttemptRecord(BaseModel); class CalibrationRecord(BaseModel)
class CalibrationResult(BaseModel)  # records, from_cache, skipped_reason,
                                    # empirical, impossible_variants,
                                    # trivial_variants
    # impossible_variants / trivial_variants and CalibrationRecord.flags
    # (FLAG_IMPOSSIBLE / FLAG_TRIVIAL) are read off selection.variant_is_impossible
    # (c == 0 on every tier) and selection.variant_is_trivial (EVERY tier aced
    # k/k) — the ONE definition each (Phase 0.F); never the roster rank
def calibrate_variant(task, gold, workspace, provider, roster, variant, *,
                      refresh=False) -> tuple[CalibrationRecord, bool]
def calibrate_task(task, gold, workspace, provider, *, roster=None,
                   variants=DEFAULT_VARIANTS, refresh=False,
                   agents_config=None) -> CalibrationResult
    # Per-variant measurement, cached by (content hash, variant, roster
    # fingerprint) under tasks/<id>/calibration/. A provider that cannot serve
    # a solver role at all (no keys, no transcripts) STOPS the campaign and
    # returns a VISIBLE skip reason — no number is ever invented.
def empirical_from_records(task, roster, records) -> EmpiricalDifficulty | None
def feasibility_findings(task, records) -> tuple[Finding, ...]
def record_feasibility_review(workspace, task, findings) -> Path | None
    # c == 0 on every tier => FLAG_IMPOSSIBLE => the calibrate stage FAILS with
    # route SPECIFICATION; that rerun IS the feasibility re-review.
```

Attached to the measurement only via `difficulty.with_empirical` (never
`model_copy`), which re-checks the content-hash binding and refuses stale
evidence. `corpus/selection.py` adds `EMPIRICAL_BAND_EDGES`,
`empirical_band_of`, `empirical_pass_rate`, `empirical_exclusion`, `band_for`:
band filtering on measured rates, never score blending. An uncalibrated task
uses structural-only behaviour. Its
`variant_is_impossible` / `variant_is_trivial` are the one definition of
"empirically impossible" / "empirically trivial" (Phase 0.F): calibration's
flags read them, so the evidence file and selection cannot disagree about a
variant. `INSTRUMENT_VERSION` invalidates cached records that used the retired
"weakest tier aced k/k" flag.

### `review/repair_proposer.py` (Round 3 — bounded, route-scoped repair)

```python
ROLE_NAME = "repair_proposer"                 # PROSE role (no findings tool)
ROUTE_ALLOWLIST: dict[RepairRoute, tuple[str, ...]]   # paths rel. tasks/<id>/
ROUTE_IR_PATHS:  dict[RepairRoute, tuple[str, ...]]   # task_ir.json fields
    # RUNTIME allows NOTHING (a rebuild is mechanical); answer_key/gold/** is
    # in NO route (gold is refrozen from the reference, never hand-patched);
    # attack_cases / status / revisions are in NO route. REFERENCE is
    # `reference.sql_by_mart.*` ALONE (Phase 1 finding 3-0): the reference's
    # dialect / implementation_id / load_notes / provenance / version are
    # VERIFIER_INERT_IR_PATHS — read by no certifier — and in no route.
VERIFIER_INERT_IR_PATHS: tuple[str, ...]              # the five reference metadata fields

def view_for_route(task, route, failure) -> str        # RUNTIME/FATAL raise;
    # the assembled evidence passes the D1 gatekeeper
    # (_assert_evidence_value_free: closed keys, identifiers and codes only,
    # every projection row re-checked by assert_value_free) — a trip is
    # DiagnosticTripwire, a harness fault, never a delivery; the finished
    # view's private-material tripwire (_assert_scope: literal fragments and
    # the anonymized-AST match) is a DiagnosticTripwire too (source
    # repair_view, codes VIEW_PRIVATE_FRAGMENT_CODE / VIEW_PRIVATE_AST_CODE):
    # LEAK_TRIPWIRE, both proposers halt, no attempt, no round
VIEW_PRIVATE_FRAGMENT_CODE = "view_private_fragment"; VIEW_PRIVATE_AST_CODE = "view_private_ast"
def failure_detail(payload, *, route=None, task=None, stage=None) -> str
    # PROJECTED and bounded on EVERY route: a gate battery becomes
    # {failing_gates, codes} through project_gate_battery; on POPULATION
    # (task given) `projections` adds project_gate_details' per-case /
    # per-relationship booleans, each row serialized by
    # serialize_for_transport and re-checked by assert_value_free; an attack
    # payload becomes {failing_gates: [], codes: {attack: <code>}} on EVERY
    # route (the promoter's per-proposal verdict codes — inert, inapplicable,
    # no_kill_predicted, mismatch, fidelity_failed, unknown_kind — are
    # post-session records for rejected_proposal.json only, never
    # a view; `proposals` is outside _EVIDENCE_KEYS); a StagePayload
    # {codes: {stage: <code>}}; a CalibratePayload the impossible/trivial
    # variant names and `skipped`; a ReviewPayload the (role, severity)
    # pairs; anything else {codes: {stage: "unprojected"}} — never a dump,
    # never details, evidence, counts, rewards, DuckDB text or paths
    # The ROUTE itself is never a model's opinion: cli._proposal_failure_route
    # derives the attack-stage FAIL route from the promoter's PromotionOutcome
    # (POPULATION only when a confirmed exploit MEASURED full reward on every
    # population, else SPECIFICATION; never FATAL) and ignores Finding.route_hint
def project_rejection(exc: PatchRejected) -> Diagnostic  # source=rejection, RejectionCode
    # every PatchRejected raise site attaches `code` (a RejectionCode value):
    # scope_route_mismatch, scope_path_outside_allowlist,
    # scope_field_outside_allowlist, scope_path_escape, patch_artifact_missing,
    # patch_artifact_unreadable, patch_anchor_not_found, patch_anchor_ambiguous,
    # patch_noop, revalidation_red_<stage>, discrimination_weakened
def patch_schema() -> dict; def parse_patch(text) -> RepairPatch
def propose_patch(task, route, failure, provider, *, attempt=0) -> RepairPatch
def apply_patch_text(text, patch) -> str
    # Textual replace/insert/delete retain their original STRING-leaf rules.
    # replace_json is JSON-artifact-only: old/new are bounded strict canonical
    # JSON text, the locator resolves exactly once, old must equal the current typed
    # value (so 1, 1.0 and true differ), and a malformed/noncanonical/missing/
    # no-op edit fails closed. Typed anchors are capped at 256 KiB each and at
    # eight replace_json edits per patch. This makes string -> null and existing
    # literal-row array repairs expressible without bypassing POPULATION scope or its mandatory
    # reference+attack discrimination re-measurement.
def trial_workspace(workspace)                          # contextmanager
def validate_scope(trial, task_id, patch, before, after, *, before_ir) -> ArtifactDiff
    # THE anti-reward-hack boundary: a diff that changed nothing is patch_noop;
    # a task_ir.json-only diff whose moved fields are ALL verifier-inert
    # (VERIFIER_INERT_IR_PATHS) is patch_noop too — the content hash would
    # rotate with nothing a certifier re-measures (finding 3-0); then the route
    # derived from the CHANGED ARTIFACTS (repair.route_from_diff, SHARPENED by
    # the moved task_ir.json fields since that one file carries prose +
    # reference + populations + attack cases) must equal the patch's claim;
    # then path allowlist; then field allowlist.
def revalidation_stages(route, failed_stage) -> tuple[str, ...]
class TrialVerdict(NamedTuple): green; failing_stage; discrimination_weakened; diff  # diff: DiffResult = repair.ArtifactDiff
def trial_phase(engine, task, stage, patch, trial, *, before, before_ir) -> TrialVerdict
    # THE CERTIFIER HALF of attempt_patch (certify addendum RC §3.3): apply the
    # patch on `trial`, validate the diff against the `before` snapshot (the
    # trial's own for a fresh trial, the LIVE snapshot taken at session INIT for
    # a held editing trial), re-validate the invalidated stages on the trial's
    # OWN ledger (_revalidate: every member's outcome is recorded there at the
    # new hash and the wired currency prerequisites a route's rerun set lacks
    # are prepended — CURRENCY_PREREQUISITES, F1 — so the real attack / gates
    # runners find the PASS rows they demand), run the discrimination guard.
    # Commits NOTHING. A red verdict is RAISED (RevalidationFailed /
    # DiscriminationWeakened with the TrialVerdict as `.verdict`), so a
    # returned verdict is green. The empirical `calibrate` runner is replaced
    # by the structural one on the trial (_trial_stage_runners).
    # NO-MEASURE outcomes are never a rejection and never a round (C7): a
    # member raising a harness fault (_INFRA_EXCEPTION_NAMES / SessionFault) is
    # re-raised, a nested plain ProviderProtocolError or another seat's
    # role-cap trip is re-raised as InfrastructureFailure, a raising runner
    # whose class COULD NOT MEASURE (certify.COULD_NOT_MEASURE_EXCEPTION_NAMES:
    # DuckDB OOM / IO, OSError, a locked ledger, MemoryError, an EngineError)
    # is wrapped as ToolHarnessFault (class only) and raised as
    # InfrastructureFailure (marker ToolHarnessFault), while ANY OTHER raising
    # runner — a defect the patch produced: duckdb ParserException on SQL the
    # model broke, a mutant that lost its surface, the reference runner's
    # divergence ValueError — is RevalidationFailed(revalidation_red_<stage>)
    # exactly like a RETURNED non-PASS, the scored outcome the live stage sequence
    # records (certify's classify_runner_exception rule split by CLASS; Phase
    # 3 review finding 1-0 — and the bounded _submit projects a certifier
    # exception no PatchRejected class named as revalidation_red_unknown), a
    # member whose payload carries an infrastructure marker raises
    # InfrastructureFailure, a member that WAITED (VERDICT_BLOCKED) raises
    # engine.StageBlocked under `blocked_on:<reason>` — the disposition
    # Engine.run gives the same verdict live — and a currency PREREQUISITE
    # outside the route's own rerun set (`review` before `attack` on the
    # REFERENCE / POPULATION routes) that answers non-PASS raises StageBlocked
    # under `blocked_on:prerequisite_not_current_<stage>`
    # (BLOCKED_ON_PREREQUISITE_PREFIX): a wait, never revalidation_red_review,
    # which survives only where review is a route member (SPECIFICATION).
    # Both proposer modes halt on these with the marker copied.
    # The discrimination guard arms on discrimination_guard_armed(*,
    # literal_rows_moved, population_moved, matrix_before): literal rows always
    # (fail closed), any other population material (_moved_population_material:
    # conditions, scale, the list, a materialized population file) over a
    # MEASURED baseline; armed, LITERAL_ROWS_PROOF_STAGES (reference, attack)
    # join the set whatever the failed stage and the superset rule runs; the
    # deletion-count rule stays literal-rows-specific.
def discrimination_guard_armed(*, literal_rows_moved, population_moved, matrix_before) -> bool
BLOCKED_ON_PREREQUISITE_PREFIX = "prerequisite_not_current_"
def _commit(trial, workspace, changed: frozenset[str]) -> None
    # Byte-only helper for direct certifier/test composition. Production
    # Engine._handle_failure does not use it: the scoped journal capability
    # sends the validated target bytes through apply_repair/commit_repair.
def attempt_patch(engine, task, stage, patch) -> TaskIR
    # trial copy -> apply -> diff-validate -> re-validate green. In production,
    # commit one journaled repair round; in direct certifier/test use, compose
    # trial_phase + _commit. Anything short of green leaves the real workspace
    # BYTE-IDENTICAL.
class RepairProposer(provider, *, max_attempts=None, config_path=None)
    def repair(engine, task, stage, route, failure) -> RepairOutcome
class RepairOutcome(record, task=None, adjudication=None, disposition="",
                    infrastructure="", limit="", limit_scope="")   # additive, defaulted (Phase 0.C)
    # disposition in {committed, needs_adjudication, blocked_limit, halted}, derived
    # from record.committed when left ''; `halted` carries the engine's infrastructure
    # marker; `blocked_limit` names the limit kind and, for limit == LIMIT_USD only,
    # `limit_scope` in BUDGET_LIMIT_SCOPES = {role, task, total} (required there,
    # forbidden elsewhere). `halting_marker(exc)` decides what the attempt loop must
    # NOT swallow (walks __cause__ / __context__ like the engine: a harness fault
    # halts; a plain ProviderProtocolError — a malformed patch — is still a retried
    # attempt; a ROLE-scope budget trip is not a halt by itself).
    # `budget_limit_scope(exc)` -> "role" | "task" | "total" | "": RoleCapExceeded by
    # NAME (ROLE_CAP_EXCEPTION_NAME) or `scope == "role"` is the session's own
    # max_usd (SoT T4 LIMIT_USD) and repair() reports it as blocked_limit / usd /
    # role; a task/total (or unknown) scope halts. Inside attempt_patch's
    # re-validation a nested harness fault is re-raised, a nested plain
    # protocol error or another seat's role-cap trip is re-raised as
    # InfrastructureFailure, and a member that WAITED (VERDICT_BLOCKED) is
    # engine.StageBlocked (`blocked_on:<reason>`) — never wrapped into
    # RevalidationFailed, never a round.
LIMIT_USD = "usd"; LIMIT_SCOPE_ROLE = "role"; BUDGET_LIMIT_SCOPES; ROLE_CAP_EXCEPTION_NAME
def proposer_attempt_budget(config_path=None) -> int   # agents.yaml repair.max_attempts
def repair_adjudication_path(workspace, task_id); queue_adjudication(...)
def load_repair_adjudication(workspace, task_id)
    # proposal attempts exhausted / explicit abort => STATUS_NEEDS_ADJUDICATION
    # in <workspace>/audit/. Engine records VERDICT_BLOCKED with
    # blocked_on=human at the failed stage: no forced patch, repair row, revision
    # or invalidation. The record is bound to the current content hash and goes
    # stale when a repair moves the identity.

# Phase 1 item 1.P — the bounded proposer (certify addendum Design H §2-§3)
REPAIR_PROPOSER_MODES = ("one_shot", "bounded"); DEFAULT_REPAIR_PROPOSER_MODE = "one_shot"
    # DEFAULT_REPAIR_PROPOSER_MODE is the programmatic compatibility fallback;
    # the CLI parser supplies `bounded` by default.
class AgenticRepairProposer(provider, *, max_attempts=None, config_path=None, worker=None,
                            clock=time.monotonic, verify_tools=False,
                            submit_deadline_s=SUBMIT_DEADLINE_S, certify_runners=None,
                            certify_worker=None)   # certify.resolve_worker_kind: process | thread
    def repair(engine, task, stage, route, failure) -> RepairOutcome   # RepairProposerLike
    # Selected by the CLI by default (`--repair-proposer-mode bounded`);
    # `--no-repair-proposer` disables the proposer. Per failure: at most
    # repair.max_attempts SESSIONS on the routes
    # repair.routes_bounded names (SPECIFICATION, REFERENCE and, since Phase 3,
    # POPULATION; a route the key omits is refused before any model call with
    # status STATUS_ROUTE_NOT_BOUNDED and the engine takes its ordinary
    # round), each holding ONE trial_workspace
    # open as the editing trial (validators.ProposerSession) and run through
    # provider.run_session with the eight proposer tools (read_view, read_field
    # minus literal_rows, apply_edit_trial — scope decided from the LOCATOR
    # before any byte is read, one code for a hit and a miss outside the route,
    # and a replace/delete anchor refused as patch_anchor_not_found on any field
    # whose text the view does not show (validators.anchor_is_visible) —
    # check_scope, check_cheap, certify, submit_patch, abort) under
    # roles.repair_proposer.session plus the document's top-level `session:`
    # defaults (validators.session_defaults_for, un-hashed). INIT calls
    # CostMeter.reserve(session.max_usd + repair.nested_ceiling_usd[route])
    # (a refusal is blocked_limit / usd / task: BLOCKED, no round, which the
    # engine halts on); repair.max_usd_per_failure bounds a failure's sessions.
    # submit_patch ends the session and the harness hands the accumulated
    # single-artifact patch to the UNCHANGED attempt_patch exactly once, outside
    # the session clock and USD cap, under SUBMIT_DEADLINE_S (a cooperative
    # supervisor around the wired runners: an expired deadline raises
    # ToolDeadlineExceeded out of trial_phase before _commit can be reached);
    # its rejection travels to the next session's view as a RejectionCode
    # (session_view), never as text. A limit stop without a validator-green
    # draft is blocked_limit (never a round; with one, the draft is
    # auto-submitted); abort queues adjudication with the workspace
    # byte-identical; every harness fault, inside the session or inside the
    # certifier, halts (C7). Each session is persisted at session_record_path
    # (`<ws>/tasks/<id>/reports/sessions/repair_proposer.<stage>.<sha>.json`)
    # with its executed certify Diagnostics; a replay-only run serves them for
    # the same trial bytes without re-executing a runner (recorded_certify_results),
    # a verify_tools replay re-executes and must match the recorded digest.
def session_view(task, route, failure, *, previous=None, index=1, max_sessions=1, salt=0) -> str
    # view_for_route(..., instructions=<session tool instructions>) plus, from the
    # second session on, "PREVIOUS SESSION k OF n: ... [rejection] <code> ok=false"
    # and, on a salted re-run after a limit stop, a SESSION RE-RUN line.
def view_for_route(task, route, failure, *, instructions=_PATCH_INSTRUCTIONS)  # default byte-identical
    # POPULATION: the conditions block is population_conditions_block(task) — every hidden
    # population's condition line the projector's gate refuses prints as CONDITION_WITHHELD
    # (Phase 3 review findings 0-0 / 0-2); the adversary's council view is unchanged.
CONDITION_WITHHELD = "[withheld: private material]"
def withheld_population_conditions(task, *, package=None) -> frozenset[tuple[int, int]]
    # (population index, condition slot) pairs projection.population_condition_private_shape
    # refuses: for a HIDDEN population (every one but DEVELOPMENT, OQ-23 option C) a standalone
    # number that is not its own declared scale, a count vector, a key tuple, a digit run equal
    # to a private scalar (private_scalars(route=POPULATION), widened by `package`) or to a hidden
    # population's REALIZED count (projection.hidden_realized_counts, gold-free); for every
    # population a measured value, a path, a secret, executor text
def condition_path_is_withheld(path, withheld) -> bool   # populations.<i>.conditions(.<j>) names one
def population_conditions_block(task, *, package=None) -> list[str]
    # council._population_summary(task, with_conditions=True, condition_text=...) with the
    # withheld lines substituted; ProposerSession.withheld_conditions holds the LIVE task's set
    # once at INIT, read_field answers field_withheld on such a slot and a replace / delete anchor
    # over it answers patch_anchor_not_found for a hit and a miss (insert stays open)
def validate_attack_flag_limits(*, attack_enabled, max_oracle_bits, wall_clock_s, max_certify,
                                certify_deadline_s, config_path=None) -> None
    # Phase 3 review finding 1-2 (fail closed, ValueError naming the key): with the flag on the
    # proposer block must declare max_oracle_bits >= max_certify * CERTIFY_ATTACK_ORACLE_BITS and
    # wall_clock_s >= max_certify * the certify deadline (certify addendum §4.6: 12 bits, 2 700 s);
    # AgenticRepairProposer.__init__ runs it, so the shipped `max_oracle_bits: 4` can never trip
    # LIMIT_ORACLE at the first flagged certify's PERMIT
class RepairSettings(max_attempts, routes_bounded, nested_ceiling_usd, max_usd_per_failure,
                     certify_attack_enabled=False, certify_deadline_s=None)
def repair_settings(config_path=None) -> RepairSettings   # agents.yaml repair.* (defaults DEFAULT_*)
DEFAULT_ROUTES_BOUNDED = ("specification", "reference", "population")   # Phase 3; the rollback
    # is the key without `population` (today's one-shot POPULATION proposer returns)
DEFAULT_CERTIFY_ATTACK_ENABLED = False        # repair.certify.attack_enabled (Phase 3, see certify.py)

# Phase 3 item 1 — the POPULATION route (roadmap Table 7; SoT T3 `read_field` / `check_cheap` rows)
    # ROUTE_IR_PATHS[POPULATION] = populations.*.conditions(.*), populations.*.scale.*,
    # populations.*.literal_rows(.**); read_field reads those plus validators.PUBLIC_SCHEMA_PATHS
    # MINUS validators.LITERAL_ROWS_PATHS on EVERY route (a read or write naming literal rows is
    # ForbiddenArgument, never a projection; OQ-21 stays declined); a literal-rows patch commits
    # ONLY through the unchanged attempt_patch behind the discrimination guard.
def witness_problem_codes(task) -> tuple[str, ...]   # mart_plan._witness_problems over the witnesses
    # the counterfactual declares, projected to projection.WITNESS_PROBLEM_CODES (witness_unknown,
    # witness_no_bridge, witness_role_missing, witness_no_second_hop); codes only, never a sentence
def check_population_cheap(trial, task) -> Diagnostic   # the POPULATION member of check_cheap
    # (validators.CheapCheckTool): validate_population_coverage projected to
    # projection.POPULATION_CHEAP_CODES (missing_population, scale_drift, no_scale_no_rows,
    # counterfactual_untargeted; anything else the generic population_problems) plus
    # witness_problem_codes; code = the first present in precedence order, every class a flag,
    # population_ok / witness_ok the whole answer; never a count or a population name
AgenticRepairProposer.attack_enabled -> bool  # repair.certify.attack_enabled OR the proposer block's
    # own session.certify.attack_enabled (hashed with the block, so a pilot arm carries it)
DEFAULT_NESTED_CEILING_USD = {specification: 0.56, population: 0.12, reference: 0.05}
DEFAULT_MAX_USD_PER_FAILURE = 2.00; SUBMIT_DEADLINE_S = 1800.0; STATUS_ROUTE_NOT_BOUNDED
class ProposerStep(index, kind, tool, code, refused, state_epoch, observation_sha256)
    # ProposerAttempt gains (defaulted, additive): terminal, rejection_code,
    # abort_reason, session_sha256, session_salt, steps, usd_session,
    # usd_certification; RepairAttemptRecord gains usd_session, usd_certification
    # (the proposer role's meter delta over its sessions and the task meter's
    # delta across attempt_patch — the nested certification spend, reported).
def session_record_dir(workspace, task_id); session_record_path(workspace, task_id, stage, sha)
def recorded_certify_results(workspace, task_id, stage, *, task_content_hash, policy_sha256, tools_sha256)
```

`engine.Engine(..., repair_proposer=...)` / `set_repair_proposer()` wire the
engine's proposer path; with none wired the Round-1 mechanical repair path is
unchanged. The CLI wires the bounded proposer by default; live use requires
`roles.repair_proposer` in config/agents.yaml (fail-closed routing), and
`--no-repair-proposer` is the explicit offline/cost option. Both proposer
classes implement `RepairProposerLike`; `--repair-proposer-mode one_shot`
restores the compatibility `RepairProposer` byte-for-byte. The bounded profile
is documented at `--budget-per-task 7.00` (`DEFAULT_BUDGET_PER_TASK_USD` stays
5.00).

### `review/tools/projection.py` and `review/tools/registry.py` (Phase 0.B — the projection layer)

This is the only path from a certifier result to model-bound bytes. The
value-aware projector runs where gold is available and builds a typed
projection. `serialize_for_transport` is the only producer of model-bound
bytes. The gatekeeper, `assert_value_free`, has no gold and checks those bytes
before transport. A violation raises `DiagnosticTripwire`, a
`review/session.py` `SessionFault` at the `sanitizer` boundary (`LeakTripwire`
is an alias binding of the same class): nothing is delivered, the stage halts
as a harness fault (exit 2, reward `None`, `engine._INFRA_EXCEPTION_NAMES`
classifies it by name), and never rejects the task.

```python
DIAGNOSTICS_VERSION = "2"                    # recorded in evidence, never hashed ("2" since Phase 3)
class DiagnosticSource(str, Enum): gate, rejection, promotion, prose, compile, dual_build
class Diagnostic(CanonicalModel):            # frozen, extra="forbid"; NO int/float/free text
    source: DiagnosticSource; ok: bool; code: str   # code in the source's closed vocabulary (_CODE_RE)
    subject: str = ""; flags: Mapping[str, bool] = {}; names: tuple[str, ...] = ()
    sha256 -> str                            # digest of the transport bytes (observation_sha256)
    def render() -> str                      # fixed template over the validated fields; never executor text
class DiagnosticText(CanonicalModel)         # one sentence, text-allowlisted sources only (prose); sha256, render()
class DevRows(CanonicalModel)                # columns + at most 200 rows (dev_query only); sha256, render()
class PublicIdentifierSet(task)              # public tables/columns/marts/keys/backends/gates/
                                             # stages/routes/case names/"development"; never a
                                             # hidden population, a literal value or a count
class RejectionCode(str, Enum)               # trust-boundary row 13 vocabulary
def project(source, raw, *, task, package=None) -> Diagnostic
def project_gate_battery(report) -> list[{gate, passed, code}]   # the triage view builder
def project_dual_build(record) -> Diagnostic                    # {ok, code}; no population identity
def project_promotion(outcome) -> dict                          # rejected_proposal.json record (humans)
def project_prose_problems(problems, *, task) -> tuple[DiagnosticText, ...]
def project_compile(cases) -> Diagnostic                        # {compiles, kind, is_directive}
def private_scalars(task, package=None, *, route=None) -> frozenset[str]   # projector-only canary set
    # on RepairRoute.POPULATION the DECLARED population scales (editable IR
    # fields the POPULATION view prints as `table~scale`) are excluded;
    # literal-row counts, the frozen stage-1 counts of every HIDDEN population
    # and gold mart row counts stay private on every route. DEVELOPMENT
    # stage-1 counts are NOT private scalars (OQ-23 option C, 2026-09-03):
    # they are solver-visible by construction — the DEVELOPMENT warehouse is
    # what the solver executes against and a page of its 2-to-8-row tables IS
    # the count — so `dev_query` may return them; nothing hidden may
def hidden_realized_counts(task) -> frozenset[str]   # source_data.realized_row_count of every HIDDEN
    # population's declared scale (a deterministic function of the IR: known without gold, private
    # on every route); DEVELOPMENT left out (option C); a collapsing band contributes nothing
CONDITION_STANDALONE_NUMBER = "standalone_number"
def population_condition_private_shape(text, *, task, population, package=None) -> str | None
    # the detector code that WITHHOLDS one population condition line from the POPULATION repair
    # view (repair_proposer.withheld_population_conditions; Phase 3 review findings 0-0 / 0-2):
    # every population — measured_value, path, secret_literal, executor_text; a HIDDEN population
    # further — count_vector / key_tuple, standalone_number (any standalone number that is not
    # that population's own declared scale), private_scalar (a digit run outside a public
    # identifier equal to private_scalars(task, package, route=POPULATION) | hidden_realized_counts)
def serialize_for_transport(diag, *, task, package=None, route=None) -> str   # forwards route
def transport_sha256(obj) -> str                                # == obj.sha256 == sha256(serialize_for_transport(obj))
def assert_value_free(payload: bytes, *, task, route=None) -> None   # raises DiagnosticTripwire
def project_in_worker(source, raw, *, task, package=None, timeout_s=30.0)  # projector in a spawned child;
    # a crash in the child is ToolHarnessFault (class name only), the deadline is ToolDeadlineExceeded
class DiagnosticTripwire(SessionFault): boundary = "sanitizer"; terminal = "LEAK_TRIPWIRE"; detector; code; payload_sha256
LeakTripwire = DiagnosticTripwire                                  # alias binding, one name in the engine's set
```

Detectors run in this order: schema (unknown field, non-boolean flag, numeric field,
code outside its vocabulary, identifier outside the public set, unknown kind
or version, non-canonical bytes); private material (the council's SQL
shingles and anonymized-AST fingerprints, route-aware; the measured-value
shapes `primary=0.25`, `"primary": 1.0`, `got 1.0`, `agreement 0.75`,
`expected N rows`); numbers (any standalone number or digit run outside a
public identifier in a `Diagnostic`; count-vector `t=N` and key-tuple `('k', 3)`
shapes in a `DiagnosticText`; the projector's canary refuses any digit run
equal to a private scalar — hidden-population scales, frozen stage-1 counts,
gold mart row counts, with plan-rule ordinals exempt); paths and secrets
(absolute paths, `answer_key/`, `private/`, `populations/`, `attacks/`,
`.duckdb`, `oracle/`, `rendered/`, `.workspace-runtime`, `runs/`,
`_credential.json`, `.tfstate`, DuckDB/Python/Terraform error shapes, key
literals). `DevRows` is exempt from the number rules only.

```python
class ToolCost(oracle_bits=0, wall_s=10.0, per_session=None, idempotent_read=False,
               idempotent_poll=False, no_cost_codes=frozenset())
    # per_session: the per-tool ceiling the runner refuses at PERMIT
    # (`per_tool_cap`, no worker spawned); no_cost_codes: the outcome codes the
    # tool answers at NO cost — a "cheap refusal" (04 §1 PERMIT --> REFUSE):
    # no oracle bit charged, no executed call spent, still a tool call
def permit_refusal(tool, ctx, args) -> str  # the PERMIT-time no-cost refusal code a tool's
    # optional `permit(ctx, args)` hook answers, '' to dispatch (or no hook); an
    # undeclared code (not in cost.no_cost_codes) or a crashing hook is a
    # ToolHarnessFault (undeclared_refusal_code / permit_hook_failed; text withheld)
class ToolContext(root, task_id, role)      # population is the DEVELOPMENT constant
    # root refused under any runs/ directory, under a release root, or credential-shaped
def path_under_runs(path) -> bool           # a `runs` component anywhere above the path
def path_under_release_root(path) -> bool   # release_manifest.json on the path or an ancestor;
                                            # both shared with cli.cmd_runtime_prepare (A22)
def check_tool_path(ctx, candidate) -> Path # relative, confined by resolved-path equality;
    # denies runs/, answer_key/, gold/, populations/, attack_cases/, attacks/, private/,
    # secrets/, oracle/, rendered/, .workspace-runtime/, *_credential.json, *.duckdb,
    # *.tfstate*, profiles.yml, .env
class Tool(Protocol): name; input_schema; cost; permitted_roles; run(ctx, args) -> Diagnostic | DevRows
    # optional, outside the structural check: permit(ctx, args) -> str (read by permit_refusal)
class ToolRegistry:
    @classmethod for_role(role) -> ToolRegistry   # the explicit registration when set, else the
                                                  # role's DECLARED tools (a runner role) or DECLARED
                                                  # harness validators (a critic seat: registry.
                                                  # _DECLARED_VALIDATOR_ROLES, Phase 3) while its `session:`
                                                  # block is enabled (the author's gate: max_revisions > 0
                                                  # too), else EMPTY
    @classmethod declared_for_role(role) -> ToolRegistry   # the declared tools, ungated
    def wire_tools() -> list[dict]                # {name, description, strict, input_schema}
    def manifest_sha256() -> str                  # tools_sha256
    def dispatch(ctx, name, args)                 # ToolLookupError: unknown_tool / tool_not_permitted / role_mismatch
                                                  # (`.as_policy_fault()` -> ToolProtocolFault / ToolNotPermitted);
                                                  # a tool that raises or returns a non-projection -> ToolHarnessFault
```

`verification/attacks._record_rejected_proposal` writes, beside the raw
`PromotionOutcome` dump, the value-free `projection` (`project_promotion`) and,
since Phase 3, the post-session `projection_matrix`
(`critic_validators.project_proposal_matrix`: `{finding_id: {promoted,
per_population: {pop: {predicted, measured_pass}}, fidelity_ok}}`, booleans
only); neither is ever returned to a session.

### `review/tools/certify.py` and the proposer's `certify` tool (Phase 1 item 1.P — certify addendum Design H §2, §3.1)

`certify` reports whether the provider-free stages in a repair route's rerun set
pass on the edited trial. A repair proposer may call it twice per session. It
runs `generate` and `reference`, which use no provider and require no model-stage
ledger pass, on a disposable nested copy of the held editing trial. A spawned
worker (`multiprocessing` `spawn`) has its own process group and the semantic
scorer's RSS envelope and watchdog. It builds the runner dictionary from the
CLI's module-level stage functions. Only the copy path, task ID, and stage names
enter the worker; only a `Diagnostic` dump or typed fault descriptor returns.
At the deadline, the supervisor kills the complete process group before
removing the copy (finding 3-1). The call is charged through `certify_calls` and
oracle bits when the worker starts.

This operation is neither `trial_phase` nor `_revalidate` and commits nothing.
`attempt_patch` runs once at `submit_patch`, outside the session clock. Phase 3
optionally adds `attack` behind `repair.certify.attack_enabled`, which defaults
to false and applies only to `POPULATION` and `REFERENCE` routes.
`cli.run_attack_stage` uses no provider, but the attack runner requires a
current `review` pass and the worker cannot rerun the council. The attack member
is therefore admitted only when the live ledger has a `review` pass at the live
hash (`memo_served_review`) and the four `council.render_view` outputs are
byte-identical between the live task and edited trial (`critic_views_changed`).

If `attack` is requested after a critic view changes, including after a
`conditions` patch changes the adversary view, permit checks refuse the complete
call as `certify_refused_review_view_changed` with zero bits and no worker. A
literal-rows patch does not change a view. In that case, `reference` and
`attack` join the proof-stage set regardless of the failed stage, and
`discrimination_weakened` reports the copy's re-measured matrix against the
live baseline. The memo's pass row is written to the copy's ledger at the copy
hash immediately before the member runs, and all side effects remain on the
copy. With the feature disabled, the Phase 1 stage set, two-bit cost, and
300-second limit remain byte-identical.

```python
TOOL_NAME = "certify"; PROVIDER_FREE_STAGES = ("generate", "reference")
CERTIFY_DEADLINE_S = 300.0; CERTIFY_ORACLE_BITS = 2; MAX_CERTIFY_PER_SESSION = 2
# Phase 3 (`repair.certify.attack_enabled`; certify addendum §3.1, §4.6, §4.8; roadmap Table 7)
CERTIFY_ATTACK_STAGE = "attack"; ATTACK_ROUTES = (RepairRoute.POPULATION, RepairRoute.REFERENCE)
CERTIFY_ATTACK_DEADLINE_S = 1200.0; CERTIFY_ATTACK_ORACLE_BITS = 6   # max_oracle_bits 12 admits two
CODE_RED_ATTACK = "certify_red_attack"; CODE_REFUSED_REVIEW_VIEW_CHANGED = "certify_refused_review_view_changed"
ATTACK_NO_COST_REFUSAL_CODES = NO_COST_REFUSAL_CODES | {CODE_REFUSED_REVIEW_VIEW_CHANGED}
CERTIFY_ATTACK_STAGE_PREREQUISITE = "review"; ATTACK_CONTEXT_KEYS = ("review_payload", "matrix_before", "rows_before", "guard")
ATTACK_CONTEXT_ROWS_KEY = "rows_guard"        # the deletion-count rule applies (literal rows moved)
def critic_views_changed(live_task, trial_task) -> tuple[str, ...]   # the seats whose render_view moved
def memo_served_review(workspace, task) -> dict | None   # the live `review` PASS payload at the live hash
def discrimination_baseline(workspace, task) -> dict     # {matrix, rows}: the live guard baseline
def attack_member_context(workspace, task, *, literal_rows_moved, population_moved=False) -> dict | None
    # ATTACK_CONTEXT_KEYS (+ rows_guard); `guard` armed by trial_phase's exact predicate
    # (repair_proposer.discrimination_guard_armed: literal rows always, conditions / scale over a measured
    # baseline); None when no review PASS at the live hash (the member is left to the submit-time
    # certifier, never faked)
CERTIFY_MEMORY_LIMIT_MB = 512                 # DuckDB limit of the worker's gold connections
CERTIFY_WORKER_RSS_LIMIT_MB = 2048            # = SemanticLimits().worker_rss_limit_mb (rlimits + watchdog)
CERTIFY_WORKER_PROCESS = "process"; CERTIFY_WORKER_THREAD = "thread"; CERTIFY_WORKER_KINDS
def resolve_worker_kind(worker, runners) -> str   # None -> process for the production dict,
    # thread for an INJECTED runner dict (a fixture's spies cannot cross a process boundary)
CODE_GREEN = "certify_green"
CODE_REFUSED_NO_PROVIDER_FREE_STAGE = "certify_refused_no_provider_free_stage"
CODE_REFUSED_UNCHANGED_TRIAL = "certify_refused_unchanged_trial"
NO_COST_REFUSAL_CODES = {the two refusals}   # projection.CERTIFY_CODES adds certify_red_<stage>
def provider_free_stages(route, failed_stage, *, attack_enabled=False, literal_rows_moved=False)
    # -> tuple[str, ...]: revalidation_stages filtered to the members (`attack` on ATTACK_ROUTES under
    # the flag; with literal_rows_moved the proof stages reference + attack join whatever the failed
    # stage); () on every SPECIFICATION failure and on RUNTIME / FATAL
def model_stages_deferred(route, failed_stage, *, attack_enabled=False, literal_rows_moved=False) -> bool
    # a member is left to the submit-time certifier
def provider_free_stage_runners(*, attack_enabled=False) -> dict   # {generate: cli.run_generate, reference:
    # _with_execution_effect_filter(run_reference_stage)} plus {attack: cli.run_attack_stage} under the
    # flag, checked by assert_no_provider_handle
def provider_handles_in(runner) -> tuple[str, ...]; def assert_no_provider_handle(runners)
    # a runner reaching a provider-shaped object (complete / _turn / run_session) is a wiring
    # defect: ToolHarnessFault(provider_handle_in_worker), never something to run
COULD_NOT_MEASURE_EXCEPTION_NAMES = {MemoryError, OSError, OperationalError, FatalException,
                                     InternalError, InterruptException, EngineError}   # by MRO name
def runner_exception_could_not_measure(exc) -> bool   # SessionFault, an engine infrastructure class
    # (chain-walked, message prefixes included) or one of the names above
def classify_runner_exception(exc) -> BaseException | None   # Phase 3 review finding 1-0 (C7 the
    # right way round): a SessionFault / an engine infrastructure class re-raised as is; an OS /
    # storage-engine / engine fault ToolHarnessFault carrying the CLASS name only — both halt;
    # ANY OTHER exception (duckdb ParserException on SQL the model broke, a mutant that lost its
    # surface, the reference runner's divergence ValueError) is None: the caller answers
    # certify_red_<stage>, the scored outcome the live stage sequence records as a FAIL row
def run_provider_free_stages(copy, task_id, stages, *, runners=None, model_stages_deferred=False,
                             attack=None) -> Diagnostic   # `attack` = attack_member_context data:
    # the memo's PASS row is recorded on the copy's ledger before the member runs, the projector
    # becomes project_certify_attack and the guard bit is measured when armed; without it `attack`
    # in `stages` is stage_not_provider_free as in Phase 1
    # Engine(copy, max_repair_rounds=0); per member: run, save_task, record_report on the COPY's
    # ledger; the first non-PASS is certify_red_<stage>; an empty set is the no-cost refusal.
    # NO-MEASURE outcomes are a ToolHarnessFault(stage_could_not_measure), never a red verdict
    # the model would read as its edit being rejected: a member whose payload carries an
    # infrastructure marker (cause_type = the marker) and a member that WAITED
    # (VERDICT_BLOCKED; cause_type = "blocked_on:<reason>", `.blocked_on` set)
class CertifyReceipt(diagnostic, stages, copy_path, elapsed_s, worker)   # stays with the SESSION, never the model
def certify_disposable_copy(held_trial, task_id, stages, *, runners=None, deadline_s=CERTIFY_DEADLINE_S,
                            clock=time.monotonic, model_stages_deferred=False, worker=None,
                            rss_limit_mb=CERTIFY_WORKER_RSS_LIMIT_MB) -> CertifyReceipt
    # a nested trial_workspace(held_trial), run_provider_free_stages in the spawned worker
    # (worker="process": _certify_worker_main under os.setsid, rlimits, an interruptible DuckDB
    # scope; _supervise_certify_worker polls the pipe, `clock` and the RSS watchdog) or in the
    # in-process cancellable thread (worker="thread": interrupt_scope at the deadline, a grace,
    # a reaper); the held trial is byte-unchanged and the copy is gone on return. A deadline is
    # ToolDeadlineExceeded and an RSS breach SandboxFault(memory_limit) — both raised only AFTER
    # the worker (its whole process group) was killed and the copy removed; a worker that exits
    # without a message is SandboxFault(worker_failed); a runner exception comes back as the
    # typed fault the worker classified (never its text); a closure handed to the process
    # worker is ToolHarnessFault(runner_not_transportable) before any copy is opened
```

The tool (`review/tools/validators.py` `CertifyTool`, the RPR row of the
permission matrix): no arguments; `cost = ToolCost(oracle_bits=2, wall_s=300,
per_session=MAX_CERTIFY_PER_SESSION, no_cost_codes=NO_COST_REFUSAL_CODES)` —
2 bits per executed call (`max_oracle_bits: 4` admits two). A third request is
refused by the runner's permit-time `per_tool_cap`, without a worker or bit
charge. `OracleCapExceeded` remains the fail-closed check for direct dispatch
and applies when a role declares a lower `max_certify`. Before dispatch,
`permit(ctx, args)` returns a no-cost refusal if the route has no provider-free
stage or if `state_epoch` is unchanged since the previous certification. The
model receives a `Diagnostic` from `project_certify` with `source="certify"`,
`ok`, a
`CERTIFY_CODES` code, `flags={model_stages_deferred, discrimination_weakened
(always False in Phase 1)}`, and no gate name, payload, or count.
`CertifyTool(attack_enabled=True)` is the flagged instance of
`repair.certify.attack_enabled` (`validators.PROPOSER_TOOLS_ATTACK`,
`proposer_policy(..., attack_enabled=)`): `cost = ToolCost(oracle_bits=6,
wall_s=1200, per_session=2, no_cost_codes=ATTACK_NO_COST_REFUSAL_CODES)`, and
its projector `projection.project_certify_attack` over `CERTIFY_CODES +
CERTIFY_ATTACK_CODES` with the guard bit active (`ok` requires a pass without
weakened discrimination). A critic view change at the new
hash drops the member (`ProposerSession.attack_member_admitted` =
`attack_member_available` and no `critic_views_changed`). This drops the
`attack` member rather than the complete call when Phase 1 members remain.
Those members still run on the copy as a paid six-bit call, and the
projection answers `model_stages_deferred=True`; `permit` answers
`certify_refused_review_view_changed` (0 bits, no worker) only when no
member remains (`attack_member_dropped_for_view` with an empty stage set),
so enabling the flag does not remove a capability available when it is off
(Phase 3 review finding 1-1; certify addendum O3). The session owns the member
set for each call through `ProposerSession.attack_member_admitted`,
`certify_stages`, `certify_deferred`, `attack_context`, and
`literal_rows_moved`, so the two never disagree inside
one session. Every
executed call's projection is recorded on the session record and a
replay-only run serves it for the same trial bytes without re-executing a
runner (`repair_proposer.recorded_certify_results`).

### `review/tools/critic_validators.py` (Phase 3 item 2 — the critic seats' harness validators, default-on)

The population adversary (POP) and shortcut attacker (SHC) use
`harness_validated` (SoT T1.1): no model-initiated tool, the forced
`report_findings` on the wire, `tool_choice` unchanged, no `abort` (abstention
is an empty findings list). The harness runs one validator on every submitted
payload and answers a failed result with a compile correction while
`max_compile_corrections` (1) and the shared `SCHEMA_RETRIES` total allow.
Both seats ship `session.enabled: true` (`roles.population_adversary.session`,
`roles.shortcut_attacker.session` in `config/agents.yaml`; the adversary's
`measured_match_bit: false` beside it). Their validators are harness-only, so
they enter the behavior manifest and harness dispatch but never the model's
wire `tools[]`. Setting a seat to `enabled: false` removes the validator from
the manifest, `validator_digests`, and tool surface
(`test_disabled_critic_tools_do_not_enter_the_wire_manifest_or_fingerprint`).
The declared block is hashed verbatim (SoT R0.2), so changes affect the two
seats' behaviour digests, policy digests, transcript keys, and
`council_routing_fingerprint`. These values are pinned as literals by
`test_phase3_critic_digests_and_fingerprint_are_pinned`; enabling either seat
requires Phase 4 admission again. `compile_proposal`'s claim-fidelity
step is guarded like the promoter's (`except Exception` -> `uncompilable`,
`kind_operation_mismatch`) and never becomes a harness fault. Roadmap Table 7
refers to this module as `validators.py`.

```python
ADVERSARY_ROLE = "population_adversary"; ATTACKER_ROLE = "shortcut_attacker"; CRITIC_VALIDATOR_ROLES
COMPILE_PROPOSAL_TOOL = "compile_proposal"; COMPILE_PROBE_TOOL = "compile_probe"
MEASURED_MATCH_BIT_TOOL = session.MEASURED_MATCH_BIT_VALIDATOR; MEASURED_MATCH_BIT_FLAG = "measured_match_bit"
CRITIC_VALIDATOR_VERSION = "3"                # folded into policy_sha256 through each tool's `version`
GRAMMAR_CODES = (proposal_missing, param_unknown, param_conflict, variant_invalid, kind_operation_mismatch, severity_incompatible, uncompilable, claim_missing_identifier)
SAME_MUTANT_FLAG = "same_mutant_as_earlier_proposal"   # the in-payload duplicate bit; never duplicate-of-catalogue
SCREEN_LEXICON                                # FindingScreenStatus values and names: never in any projection
POST_SESSION_PROJECTION_NAMES = (project_proposal_matrix, proposal_matrix, predict_check, mutant_applicable)
                                              # verbs no manifest of any role may carry
class CriticSession(*, task, role=ADVERSARY_ROLE, gold=None, workspace=None)   # harness-side state
    def install_payload(payload) -> tuple[Finding, ...]   # the council's own _parse_findings, untouched
    def finding_at(index, tool) -> Finding; current_draft() -> dict | None; context(root) -> CriticToolContext
    finding_checks: tuple[Diagnostic, ...]   # the per-finding compile_proposal diagnostics of the LAST
                                             # installed payload (index-aligned with `findings`); never sent
UNCOMPILABLE_AFTER_CORRECTIONS = "uncompilable_after_corrections"   # FindingScreen signal, post-session
def void_uncompilable_proposals(findings, session, result=None) -> tuple[Finding, ...]   # POST-SESSION,
    # never a tool: the findings whose proposed_case the harness compiled red on the ACCEPTED submission
    # (result.red_validators_at_submit names compile_proposal; with no result the recorded checks decide)
    # cause ProviderProtocolError after bounded correction at every severity; invalid executable
    # content is never VOIDed, skipped, or charged to the task. screen_findings is untouched;
    # council.findings_from_session (Phase 4) applies this to _parse_findings's output before the screen.
class CriticToolContext(ToolContext): session   # non-identity; never serialized
def compile_proposal(ctx, {"finding_index": int}) -> Diagnostic   # POP: {compiles, grammar_code?,
    # missing_identifiers, duplicate_of?} as source=compile, code compiles|uncompilable, subject = the
    # attack KIND, ONE grammar flag, the PUBLIC names the claim failed to name, SAME_MUTANT_FLAG; wraps
    # what council._proposal_mutant_key runs (attacks._proposal_structured_payload / _proposal_mutation)
    # and validate_proposal_claim_fidelity; never materialize_mutation, never the key
def compile_probe(ctx, {"finding_index": int}) -> Diagnostic      # SHC: {compiles, kind, is_directive} —
    # project_compile over council._compiled_mutant_key's compile_attacks(bare, [probe]); never the key,
    # never an inert / inapplicable sentence, never the screen lexicon
def fold_compile_proposal(diagnostics) -> Diagnostic   # payload-level: red iff any proposal is red
def fold_compile_probe(diagnostics) -> Diagnostic      # payload-level: a problem ONLY when NO probe compiles
def measured_match_bit(ctx, {"finding_index": int}) -> Diagnostic   # POP, flag F, default off: {exact_match}
    # from promote_proposed_cases on a scratch copy of the population rows under ctx.root with the
    # session's gold handle (D3 gold-bearing worker); source=promotion, code promoted|mismatch
def project_proposal_matrix(outcomes) -> dict   # POST-SESSION, never a tool: {finding_id: {promoted,
    # per_population: {pop: {predicted, measured_pass}}, fidelity_ok}}, booleans only; written to
    # rejected_proposal.json (`projection_matrix`) by attacks._record_rejected_proposal
class CompileProposalTool   # harness_only validator, ToolCost(oracle_bits=3, wall_s=20), POP
class CompileProbeTool      # harness_only validator, ToolCost(oracle_bits=3, wall_s=20), SHC
class MeasuredMatchBitTool  # harness_only validator, ToolCost(oracle_bits=1, wall_s=300, per_session=1), POP
CRITIC_VALIDATORS; CRITIC_VALIDATOR_NAMES; def critic_validator(name)
def declared_validators(role, block=None) -> tuple   # compile_proposal (+ measured_match_bit under the
    # flag) for POP, compile_probe for SHC, () otherwise; UNGATED (ToolRegistry.for_role applies `enabled`)
def critic_registry(role, block=None) -> ToolRegistry   # the ungated registry a pilot session dispatches through
def critic_limits(role, block=None, *, agents_config=None) -> SessionLimits   # the block VERBATIM (R0.2)
def critic_policy(role, limits=None, *, session_salt=0, agents_config=None) -> SessionPolicy
    # submit report_findings, abort_tool "", mode harness_validated, the one-shot wire (wire_tools_for)
def critic_validator_worker() -> InProcessValidatorWorker   # the last submitted payload as the draft
```

`registry._DECLARED_VALIDATOR_ROLES` (POP, SHC) is separate from
`_DECLARED_ROLES`; `ToolRegistry.for_role` adds
`_declared_validators(role)` only while the seat's block is enabled, and
`declared_for_role(POP / SHC)` stays `()`; every validator is `harness_only`,
so `providers.session_tool_choice` keeps the forced tool;
`providers._harness_validators_for` names `SessionLimits.
active_harness_validators` only while `limits.enabled`;
`metrology.HARNESS_VALIDATOR_MODULES` / `HARNESS_VALIDATOR_BINARIES` hash the
validator code and pin `duckdb` / `sqlglot` only for enabled seats. The
pilots that decide the flip are `docs/experiments/PILOT-P4.md` and
`docs/experiments/PILOT-P5.md`. `council.run_council` is untouched; since
Phase 4 `RoutedProvider.complete` runs the seat's bounded session itself
(`providers.AGENTIC_ROLES`, `agentic_role_enabled`) only while the seat's
block says `enabled: true` and a trial or task context is active, using these
objects (`critic_limits`, `critic_policy`, `CriticSession.context(root)`, the
trial's `TrialToolExecutor` as the worker) and hands the final payload to
`council.findings_from_session`; setting a seat to `enabled: false` prevents
session dispatch and restores its one-shot path.

### `review/tools/credential_sweep.py` (Phase 0.F — credential hygiene, threat row A22)

This harness-side check prevents credential-shaped files from appearing in
locations read by the ledger, exporter, or tools. A file is credential-shaped by name
(`*_credential.json`, `credentials.json`, `.env*`, `profiles.yml`,
`*.tfstate*`, `*.tfvars*`, INI credential files, key material) or by value
(a JSON, YAML, `.env`, Terraform/HCL or INI-shaped file whose secret-shaped
keys — `password`, `secret`, `token`, `api_key`, `access_key`, `private_key`,
... — hold a non-empty string that is neither a placeholder (`""`, `${VAR}`,
`{{ env_var(...) }}`, `${{ secrets.X }}`, `<template>`, `[REDACTED]`, `***`)
nor one of the exporter's public source-fixture values). Values are read to
be judged and are not retained. A finding carries only the path, size, name
rule, and offending key names.

```python
PUBLIC_SOURCE_FIXTURE_VALUES = {"testelt", "test"}   # export/eltbench.py writes these into every public bundle
class CredentialFinding(path, size_bytes, name_pattern, secret_keys, opaque=False)
    live -> bool                                # secret_keys or opaque (unvettable: key material, binary, oversized)
def sweep_credential_shaped_files(root, *, live_only=True, max_bytes=1 MiB, exclude=None) -> list[CredentialFinding]
    # symlinks never followed; sorted by path; live_only=False also lists placeholder-only name matches
def is_secret_key(key) -> bool; def is_placeholder_value(value) -> bool; def is_public_fixture_value(value) -> bool
def format_findings(findings) -> str            # paths, sizes, rules, key names; never a value
```

`tests/test_credential_sweep.py` pins the sweep on a synthetic tree, the
model-facing attempt workspace (`test_attempt_workspace_contains_no_live_credential`)
and the operator's drives (`test_runs_tree_carries_no_credential_shaped_files`,
gated behind `ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1` until the owner's rotation and
purge of `runs/runtime_canary_*/**/live/`; vendored `dbt_packages/` trees are
excluded there). `cli.cmd_runtime_prepare` refuses a `--work-dir` under `runs/`
or a release root through `_refuse_confined_work_dir` before any credential or
the release is read (`test_runtime_prepare_refuses_work_dir_under_runs_or_release`).

### `review/session.py` (Phase 0.C — the fault taxonomy; the runner is Phase 1)

Faults are typed by the boundary that catches them, never by message text
(SoT T6). Harness-side faults are infrastructure: `engine._INFRA_EXCEPTION_NAMES`
carries exactly the seven canonical names (`SessionFault`, `ProviderFault`,
`ToolHarnessFault`, `ToolDeadlineExceeded`, `SandboxFault`, `DiagnosticTripwire`,
`TaskDefectFault`), `engine._infra_marker_for` matches them through the MRO,
and the disposition is C7: exit 2, reward `None`, no repair round, never a task
rejection. Model-caused faults (`PolicyFault`) carry one fixed model-visible
code each, are the label-eligible family, and no name of theirs is ever in
that set. Two wrappers carry a session halt into the engine through the
`ProviderProtocolError` name it already classifies. Every class records the
SoT T4 terminal it maps to as the string `terminal`.

```python
class SessionFault(RuntimeError):            # boundary in {provider, tool, sandbox, sanitizer, task}
    boundary: str; failure_class: WorkspaceFailureClass; terminal: str; code: str
class ProviderFault(SessionFault)            # provider; TRANSIENT_INFRASTRUCTURE; PROVIDER_FAULT
class ToolHarnessFault(SessionFault)         # tool; HARNESS_DEFECT; HARNESS_FAULT — names the exception CLASS, never its text
    @classmethod from_exception(tool, exc, *, code="harness_exception")
class ToolDeadlineExceeded(SessionFault)     # tool; TRANSIENT_INFRASTRUCTURE; HARNESS_FAULT; (tool, *, deadline_s)
class SandboxFault(SessionFault)             # sandbox; TRANSIENT_INFRASTRUCTURE; HARNESS_FAULT
class TaskDefectFault(SessionFault)          # task; TASK_DEFECT; HARNESS_FAULT
class PolicyFault(RuntimeError):             # code (a _CODE_RE code); tool; detail; ends_session; security_event
class ToolProtocolFault(PolicyFault)         # one of PROTOCOL_FAULT_CODES; a correction; 3 in a row -> PROTOCOL_EXHAUSTED
class ToolNotPermitted(PolicyFault)          # "tool_not_permitted"; POLICY_VIOLATION + security event
class OracleCapExceeded(PolicyFault)         # "oracle_cap_exceeded"; LIMIT_ORACLE (a limit, not a violation)
class WriteOutsideSurface(PolicyFault)       # "write_outside_surface"; POLICY_VIOLATION + security event
class ForbiddenArgument(PolicyFault)         # "forbidden_argument"; POLICY_VIOLATION + security event
class SessionProtocolError(ProviderProtocolError)    # PROTOCOL_EXHAUSTED: (role, *, faults=3, last_code="")
class SessionPolicyViolation(ProviderProtocolError)  # POLICY_VIOLATION: (fault, *, role=""); refuses OracleCapExceeded
INFRASTRUCTURE_FAULT_NAMES; POLICY_FAULT_NAMES; PROTOCOL_FAULT_LIMIT = 3; PROTOCOL_FAULT_CODES

# Phase 3 item 3 — the correction channel (roadmap Table 7; SoT T1.1 harness_validated, T8)
SCHEMA_RETRIES = PROTOCOL_FAULT_LIMIT - 1     # pinned equal to providers.SCHEMA_RETRIES (2): the
    # corrections of ANY kind one session may issue; schema and compile corrections SHARE them
MEASURED_MATCH_BIT_VALIDATOR = "measured_match_bit"   # declared by the POP block's flag, never by
    # `harness_validators`; SessionLimits.measured_match_bit / .active_harness_validators
ValidatorHook = Callable[[TaskIR, Mapping[str, Any]], Diagnostic | None]   # None when the payload passes
def run_bounded_session(..., payload_validators: Sequence[ValidatorHook] = ()) -> SessionResult
    # (through RoutedProvider.run_session's **runner_kwargs) the caller's TASK-AWARE hooks, run on every
    # submitted payload after the schema check (providers.validate_payload_for never reads the task):
    # a red Diagnostic is serialized by the projector, re-checked by the D1 gatekeeper and delivered by
    # its own render() on a compile-kind correction — never CORRECTION_TEXT plus a problem sentence —
    # while max_compile_corrections AND the shared SCHEMA_RETRIES total allow, else the payload is
    # accepted as submitted; every run is a validator turn; a hook that raises or returns a
    # non-Diagnostic is ToolHarnessFault (C7). SessionResult.correction_kinds = {schema: n, compile: n}.
VALIDATOR_RED_AT_SUBMIT_CODE = "validator_red_at_submit"   # the terminal turn's outcome_code when the
    # payload was accepted with a validator / hook still red (the budget was spent)
SessionResult.red_validators_at_submit: tuple[str, ...]    # their names; .submitted_with_red_validators
    # (int) is the SoT T8 counter on the SESSION evidence row (the one-shot row is byte-identical);
    # critic_validators.void_uncompilable_proposals reads them post-session
```

`MODEL_CALL_DEADLINE_VERSION = "interruptible-model-call-v1"` is part of the
admission fingerprint. On POSIX worker processes, each model call runs under
the session's remaining wall-clock deadline, which interrupts a blocking SSL
read; bounded sessions invoked outside the process main thread fail closed
before transport because that deadline cannot be enforced safely there.

The repair-proposer seat: `RepairOutcome.disposition` is one of `committed`,
`needs_adjudication`, `blocked_limit`, `halted`. `Engine._propose_patch` maps
an actual `needs_adjudication` record to `VERDICT_BLOCKED`
(`blocked_on = human`, no repair row, revision, invalidation, fatal row or
rejection), `blocked_limit` to `VERDICT_BLOCKED`
(`blocked_on = session_limit:<kind>`, a `session_salt`, at most
`engine.MAX_SESSION_LIMIT_RERUNS = 2` salted re-runs, no round, no fatal row;
`Engine.session_limit_reruns` counts them at the current identity), and
`halted` to `InfrastructureFailure` with the outcome's `infrastructure` marker
copied onto the `FAIL` row; an exception raised out of the proposer that the
engine classifies as infrastructure halts the same way.
A certification member that returned `VERDICT_BLOCKED` becomes `engine.StageBlocked`
— `halted` under `blocked_on:<reason>`, the reason copied onto the row as
`blocked_on` — never a rejection code and never a round, the disposition the
same verdict has live. `cli._transport_marker` lifts the seven names out of
producer notes exactly as it lifts the transport names.

### `corpus/selection.py`

```python
class Quotas(BaseModel):
    size: int
    difficulty_bands: dict[str, float]     # band -> target fraction
    backend_mix: dict[Backend, float]
    origin_mix: dict[Origin, float]
    val_fraction: float

class SelectionResult(BaseModel):
    train: tuple[str, ...]                 # parent task_ids
    val: tuple[str, ...]
    rejected: dict[str, str]               # task_id -> reason
    variants: dict[str, tuple[str, ...]]   # each selected parent -> (EL, T)

def select(candidates: list[TaskIR], measurements: dict[str, DifficultyMeasurement],
           anchors: list[TaskIR], quotas: Quotas,
           accepted_variants: dict[str, Collection[str]]) -> SelectionResult
    # both required semantic phase acceptances plus family/cluster and split
    # isolation are enforced here; deterministic and fail closed. The variants
    # establish one parent's RLVR readiness; they are not separate public units.

# THE ONE definition of "empirically impossible" / "empirically trivial"
# (Phase 0.F): corpus/calibration.py reads FLAG_IMPOSSIBLE / FLAG_TRIVIAL off
# these same predicates, so the evidence file and the selection never disagree.
def variant_is_impossible(calibration: VariantCalibration) -> bool
    # zero successes on EVERY pinned solver tier
def variant_is_trivial(calibration: VariantCalibration) -> bool
    # EVERY pinned solver tier aced all k of its attempts — never "the weakest
    # tier aced": no roster rank survives into a DifficultyMeasurement, and a
    # single failed attempt on any tier keeps the variant measurable
def variant_empirical_exclusions(m: DifficultyMeasurement) -> dict[str, str]
    # variant value -> rejection reason from MEASURED evidence; pair-level
    # eligibility rejects either failure
```

### `export/eltbench.py`

```python
def export_task(task: TaskIR, gold: GoldBundle, task_dir: Path, answer_key_dir: Path) -> None
    # writes the upstream-shaped public bundle + private evaluation artifacts
    # (layout in 'Workspace layout' above); atomic staging; every-column ORDER BY
def evaluation_sql(mart: MartSpec) -> str      # SELECT ... ORDER BY keys, then ALL remaining columns

# --- Private semantic RLVR variants over the SAME frozen artifacts ---
def emit_variant(task, gold, variant: TaskVariant, out_dir: Path, *,
                 populations_dir: Path | None = None,
                 flat_files_base_url: str | None = None,
                 rest_base_url: str | None = None) -> None
    # out_dir/task/ = battery input (leak-checked), out_dir/reward.json = PRIVATE.
    # These legacy-shaped trees support curation/calibration and are NEVER
    # copied to schema-3 public/. The parent task/ is the sole public source.
    #   FULL         -> reward.json only (bundle IS the parent task/ tree)
    #   EXTRACT_LOAD -> config.yaml + schemas/ + documentation.md (+ dev sources);
    #                   NO data_model.yaml
    #   TRANSFORM    -> data_model.yaml + schemas/ + documentation.md
    #                   (t_documentation, NOT the parent's public spec) +
    #                   warehouse/<pop>.duckdb (SOURCE TABLES ONLY, row counts
    #                   cross-checked against frozen stage-1 gold; needs
    #                   populations_dir).
    #                   Emitting TRANSFORM also RECORDS the warehouse census.
def reward_manifest(task, gold, variant, *,
                    population_relations: dict[str, str] | None = None,
                    memorization_evidence: dict[str, Any] | None = None) -> dict
    # evaluator entrypoint + answer-key inputs; see the reward.json table below
def materialize_warehouse(task, gold, population: str, rendered_dir: Path, db_path: Path) -> None
def el_documentation(task) -> str
def t_documentation(task, populations=()) -> str
    # private semantic T contract: a provided DuckDB warehouse and
    # one standalone SELECT per mart. It describes an internal battery input,
    # not the schema-3 solver-facing task, whose transform runs through dbt in
    # the selected real destination.

# --- serving: what the benchmark harness stands up for cloud EL certification ---
DEFAULT_FLAT_FILES_BASE_URL = "http://elt-files:8080"   # env ELT_TASKGEN_FLAT_FILES_BASE_URL
DEFAULT_REST_BASE_URL       = "http://elt-api:5005"     # env ELT_TASKGEN_REST_BASE_URL
SOURCES_SERVING_MANIFEST = "sources_serving.json"
def assert_serving_manifest_complete(task, config, manifest, rendered_dirs) -> None
    # fail closed at export: every table named in any config.yaml connector stanza
    # has exactly one manifest entry, its backend agrees with task.backend_for, and
    # its rendered artifact exists under every rendered dir supplied.

# --- private oracle warehouse, promoted from an export assertion to EVIDENCE ---
WAREHOUSE_CENSUS_EVIDENCE_REL = "reports/warehouse_census.json"  # under tasks/<id>/
WAREHOUSE_CENSUS_KIND = "warehouse-census"
CENSUS_VERSION: str      # bumped when the census CHANGES MEANING; the gates
                         # refuse a recorded census at any other version and
                         # ask for an offline re-run, never grade under it
def warehouse_census(db_path: Path) -> dict
    # {"tables": {relation: {"row_count", "row_digest"}}, "census_digest"} over
    # EVERY relation in the file (a smuggled answer_key relation must MOVE the
    # digest, not hide behind a table filter). Read-only; never reads a
    # storage byte.
def census_digest(tables) -> str        # sha256 of sorted (table,row_count,row_digest)
def warehouse_census_path(task_root: Path) -> Path
def record_warehouse_census(task, gold, *, populations_dir, warehouse_dir,
                            task_root) -> Path
    # censuses each SHIPPED warehouse, then materializes a SECOND one into a
    # throwaway file and censuses that: census_digest + rebuild_census_digest
    # from two clean builds (one build is a claim, two are evidence).
```

Do not compare `.duckdb` bytes. DuckDB version headers and storage-page
allocation can differ between builds of identical data. The reproducibility
witness is instead sorted `(table, row_count)` data plus a per-table row-
multiset digest. It is independent of physical row order and detects changed
values, dropped rows, and extra relations. `warehouses-load`,
`data-sensitivity`, and the transform part of `determinism` consume it.

```python
def assert_public_tree_clean(task, public_dir: Path) -> None
    # legacy/internal variant-tree check; for *.duckdb the relation list is
    # checked instead (any mart / gold-named / non-source relation raises)
def assert_public_runtime_shape(public_dir: Path) -> None
def assert_public_runtime_tree_clean(task: TaskIR, public_dir: Path) -> None
    # schema-3 parent-bundle checks: require the original ELT-Bench runtime
    # scaffold and reject DuckDB plus retired load-plan/standalone-SQL concepts
REWARD_MANIFEST = "reward.json"; WAREHOUSE_DIRNAME = "warehouse"
EL_DOCUMENTATION_FILENAME = "documentation.md"
```

The two private phase rewards use the existing semantic comparison functions;
no third comparator exists. `reward.json` names them by import path. The
private batteries are directly executable in DuckDB. Cloud collectors reuse
the same comparators with rows/counts read from the selected destination:

| variant | evaluator | mode |
|---|---|---|
| `extract_load` | `upstream_eval.compare_stage1` | **strict binary** (1.0 iff every source table exact, else 0.0; per-table detail recorded). Upstream-faithful and therefore **count-only**: values are never inspected by the reward. |
| `transform` | `upstream_eval.compare_mart` | fraction of fully-correct marts (stage 1 is provided, not scored) |

`upstream_eval.evaluate` remains available for shared/legacy composite
diagnostics, but `full` is not an active semantic phase and no full reward
ships. The combined parent directory is a runtime export, not
`TaskVariant.FULL`.

`reward.json` keys, beyond `evaluator`/`mode`/`parent_task_id`:

| key | unit | meaning |
|---|---|---|
| `populations_required` | both | always `"all"` — the reward is over every graded population named here, all-or-nothing. Scoring `primary` alone is not the contract. |
| `expected` | EL | population -> `answer_key/gold/<pop>/stage1_counts.json` |
| `sources` | EL | population -> `populations/<pop>/rendered`, the private source-service fixtures used to deploy the graded environment (a workspace resolves it under `tasks/<parent>/`, a release under `private/<parent>/`) |
| `serving` | EL | `answer_key/sources_serving.json` — how a harness stands the sources up |
| `warehouse` | T, workspace only | population -> `warehouse/<pop>.duckdb` for the internal transform battery; removed when schema 3 freezes |
| `oracle` | T, schema-3 release | population -> `oracle/<pop>.duckdb`, private under `private/<parent>/`; never a solver input |
| `gold` | T | population -> `answer_key/gold/<pop>/<mart>.csv` |
| `population_relations` | both, optional | emitted only when a relation actually holds, e.g. `{"resampled": "rearrangement_of:primary"}` for provided-rows pools whose `resampled` split is the same real rows in a different physical order. A trainer aggregating per-population rewards should dedupe such a split instead of double-counting `primary`; `gates.POPULATION_RELATION_REARRANGEMENT` is the constant and `gates.pair_is_rearrangement()` the classifier. |
| `memorization_evidence` | both, optional | emitted beside `population_relations`: `{kind: "value-bijection", path: "reports/perturbation_probe.json", passed: bool}` — the oracle-validation probe that proves the reward reads source values, because a rearrangement-only split cannot. |
| `path_base` | both | which private/workspace directory the relative paths above resolve under, per key group |

At schema-3 freeze, `load_plan_root`, `warehouse_base`, and the transform
`warehouse` mapping are stripped from the copied reward evidence. The freezer
adds `scope: private_stage_evidence`, records the selected destination's
warehouse-state runtime, and replaces the transform mapping with private
`oracle` paths.

### `export/release.py`

```python
class SourceIdentity(BaseModel):
    pool: str
    origin: Origin
    selector: str
    upstream_url: str
    upstream_revision: str
    source_digest: str                 # exact selected source bytes/tree/record
    source_digest_kind: str
    adapter_name: str
    adapter_version: str
    adapter_digest: str
    license: str
    license_evidence: str
    selection_inputs: dict[str, ProvenanceArtifact]  # entry projection/catalog + pool inputs

class ProvenanceArtifact(BaseModel):
    locator: str                         # portable logical locator, never host path
    digest: str
    digest_kind: str

class IngestProvenance(BaseModel):
    schema_version: Literal["1.0"]
    task_id: str
    task_content_hash: str             # intake/lineage-root semantic hash
    source: SourceIdentity

class ReleaseManifest(BaseModel):
    # Parser fallbacks preserve the meaning of manifests that predate these
    # fields. The current freezer explicitly writes the constants below.
    schema_version: str = "1.0"
    corpus_profile: str = "legacy_full"
    public_layout: str = "split_variants"
    destinations: dict[str, str]             # parent -> snowflake|databricks|redshift
    destination_connector_versions: dict[str, str]  # parent -> image tag
    release_id: str
    tasks: dict[str, str]                  # task_id -> TaskIR content hash
    splits: dict[str, str]                 # task_id -> 'train'|'val'
    families: dict[str, str]               # task_id -> family_id
    licenses: dict[str, str]
    source_provenance: dict[str, IngestProvenance]       # task -> exact origin
    source_provenance_digests: dict[str, str]             # canonical record pins
    checksums: dict[str, str]              # rel_path -> pinned digest
    checksum_kinds: dict[str, str]         # rel_path -> pin rule, non-byte pins ONLY
    warehouse_census: dict[str, WarehouseCensusRecord]   # rel_path -> census evidence
    variants: dict[str, tuple[str, ...]]   # parent -> accepted internal EL/T phases
    el_sources: dict[str, dict[str, str]]  # parent -> population -> release-rel dir
    environment: dict[str, str]            # python/platform + duckdb/sqlglot/pydantic/pyyaml/python-hcl2/lark
    environment_drift: dict[str, str]      # package -> 'installed vs uv.lock' when it drifted
    scorer_version: str
    semantic_scorer_version: str           # combined private executor contract (3.2+)
    semantic_release_id: str               # destination-independent WHAT identity (3.3+; corrected in 3.4)
    runtime_bundle_ids: dict[str, str]      # parent -> semantic-chained, destination-bound HOW identity (3.4+)
    certification_ids: dict[str, str]      # parent -> pinned execution identity
    certification_matrix: dict[str, dict[str, str]]  # destination -> frozen pins/settings
    generator_version: str

RELEASE_SCHEMA_VERSION = "3.5"
COMBINED_CORPUS_PROFILE = "eltbench_end_to_end"
COMBINED_PUBLIC_LAYOUT = "combined"

class WarehouseCensusRecord(BaseModel):
    census_version: str                    # eltbench.CENSUS_VERSION at freeze time
    census_digest: str                     # == checksums[rel] for this warehouse
    row_counts: dict[str, int]             # relation -> rows
    row_digests: dict[str, str]            # relation -> row-multiset digest

def freeze_release(engine: Engine, selection: SelectionResult, out_dir: Path) -> ReleaseManifest
    # requires current, roster-complete EL AND T acceptance; ships the combined
    # parent task once, with answer keys, source fixtures, phase rewards, and
    # DuckDB oracle warehouses private

def verify_release(release_dir: Path) -> ReleaseVerification
    # re-checks a frozen release against the rules ITS OWN manifest recorded

def load_for_lineage(task_dir: Path, *, task_id: str,
                     lineage_hash: str, required: bool = True) -> IngestProvenance | None
    # read-only preflight for a proposed/non-current lineage; validates the
    # complete append-only store so conflicts are found before task mutation
```

**Checksums cover reproducible content.** Every released file is pinned by its
SHA-256 bytes except private `.duckdb` oracle warehouses, which are pinned by
their warehouse census because DuckDB files are not byte-stable.
The exemption is explicit per file (`checksum_kinds[rel] ==
"duckdb-census/<CENSUS_VERSION>"`, evidence in `warehouse_census[rel]`), covers
`.duckdb` and nothing else, and a census pin claimed for any other path is a
verification failure. `checksums.sha256` therefore lists byte hashes in
sha256sum format (so `shasum -c` still verifies it as-is) and records the
census-pinned warehouses as `#` comment lines naming their rule and digest.
Schema-1/2 releases remain readable and are verified under the layout and pin
rules recorded in their own manifests. In particular, schema-2 profile `el_t`
and `public_layout split_variants` identify the deprecated two-public-unit
format; verification does not relabel it as schema 3.

Schema 3.4 also requires **semantic portability** because checksums alone do
not prove that a reference package is usable. Both freeze and later verification load
every released source population through the strict typed loader, require its
exact table counts to match frozen stage-1 gold, canonicalize every logical
source relation, and canonicalize every frozen mart CSV under its declared
TaskIR types. An over-scale `DECIMAL(38,9)`, malformed JSON/timestamp, non-finite
numeric value, missing/extra population, source-count drift, or unrepresentable
gold therefore refuses the release before any live warehouse is provisioned.
Schema 3.3 and older releases retain their recorded verification rules; they
must be re-frozen under the current schema before new certification.

Schema 3.5 adds the non-semantic ingest sidecar
`tasks/<task_id>/ingest_provenance/<lineage_root_hash>.<evidence_digest>.json`.
An immutable sibling `<lineage_root_hash>.claim` atomically arbitrates
concurrent publishers. Its typed record
binds the exact pool selector, upstream URL and revision, selected-source
digest and digest recipe, adapter version/digest, license evidence, the typed
coordinator's source-specific manifest projection and catalog, plus
selection-affecting SchemaPile index or
WikiDBs family-map/node-inventory inputs without
changing `TaskIR.content_hash()`. Publication is append-only: the same lineage
may only confirm identical deterministic bytes, while a genuine re-ingest has
a new lineage root and appends a new record. Freeze copies the current record
to `private/<task_id>/provenance/ingest_provenance.json`; the release manifest
contains both the typed record and its canonical digest, and the ordinary file
inventory independently byte-pins the sidecar. A certified release requires
exact task coverage. Development releases may omit provenance for legacy/local
fixtures, and manifests before 3.5 retain their recorded verification rules.
The batch receipt separately binds the complete five-entry manifest. Changing
one source entry therefore leaves the other four task provenance records
stable, while changing a shared catalog pin still changes every affected
record.

`splits`, `families`, `tasks`, and `licenses` are keyed by parent task id.
Schema 3 places that same id at `public/<parent>/`. Every selected parent must
still pass the `extract_load` and `transform` batteries, and their suffix ids
remain useful for `private/<parent>__el/reward.json` and
`private/<parent>__t/reward.json`. They do not name public tasks. Both phases
inherit the parent family and split, so semantic evidence cannot straddle the
train/validation boundary.

**Release layout.**

```
release/
  public/<parent>/        config.yaml, data_model.yaml, schemas/<table>.csv,
                          documentation/README.md and runtime guides,
                          check_job_status.py, <destination>_credential.json,
                          elt/main.tf
  private/<parent>/       answer_key/...             # gold, reference SQL, serving manifests
                          semantic/task_ir.json      # private typed semantic scorer input
                          populations/<population>/rendered/<backend>/...
                                                     # source-service fixtures
                          oracle/<population>.duckdb # private DuckDB gold oracle
  private/<parent>__el/   reward.json                # private EL phase evidence
  private/<parent>__t/    reward.json                # private T phase evidence
  release_manifest.json
  checksums.sha256
```

There is no `public/<parent>__el`, `public/<parent>__t`, public `sources/`, or
public DuckDB warehouse in a schema-3 release. The benchmark harness deploys
`private/<parent>/populations/<pop>/rendered/` through the backends named in the
combined public `config.yaml`. `freeze_release` refuses when a population named
by the EL reward has no rendered tree, and `verify_release` fails with kind
`el-sources` when a released private source root is missing or unpinned.

Historical schema-2 releases used `public/<parent>__el` and
`public/<parent>__t`; that layout remains supported for faithful verification
and by the legacy DuckDB scorer. New releases must never reproduce the split
public layout. Schema 3.2 adds the checksum-bound private TaskIR used by the
separate combined-task semantic scorer; it does not alter the public runtime
contract.

**Mapping to upstream ELT-Bench.** The public bundle uses the upstream layout.
The upstream agent-input generator (`setup/write_config.py`)
copies `elt-bench/schemas/<db>/` into each `inputs/<db>/schemas/`, which is
equivalent to `public/<parent>/schemas/`. The private side consolidates the
upstream shared, multi-database evaluation tree per task. It contains the same
files with one fewer directory level because each oracle belongs to one task:

| ours | upstream `fcf3129` |
|---|---|
| `public/<parent>/schemas/<table>.csv` | `inputs/<db>/schemas/<table>.csv` (copied from `elt-bench/schemas/<db>/`) |
| `public/<parent>/config.yaml` | `elt-bench/<destination>/<db>/config.yaml` |
| `public/<parent>/data_model.yaml` | `elt-bench/<destination>/<db>/data_model.yaml` |
| `public/<parent>/documentation/`, `check_job_status.py`, `elt/main.tf` | original solver runtime helpers/scaffold |
| `private/<parent>/answer_key/table.json` | `evaluation/table.json` (one `<db>` key of it) |
| `private/<parent>/answer_key/sort_key.json` | `evaluation/sort_key.json` (one `<db>` key of it) |
| `private/<parent>/answer_key/evaluation/sql/<mart>.sql` | `evaluation/sql/<db>/<mart>.sql` |
| `private/<parent>/answer_key/gold/<pop>/<mart>.csv` | `agent_results/gt_<warehouse>/<db>/<mart>.csv` |

The semantic comparison implementation remains path-agnostic (`upstream_eval`
takes counts/rows and CSV text, never a warehouse directory).
`runtime/evaluation.py` is the schema-3 collector: Stage 1 lists and counts the
selected destination namespace; Stage 2 rewrites private evaluator queries to
that namespace and collects rows; both delegate to `compare_stage1` /
`compare_mart`. Snowflake uses `<task>.AIRBYTE_SCHEMA`, Databricks uses
`<catalog>.<task>`, and Redshift uses `<task>` in the connected database.

Runtime orchestration is split by ownership:

| module | responsibility |
|---|---|
| `runtime/bootstrap.py` | version-pinned abctl install, fresh Airbyte workspace, built-in definition checks, declarative REST publication; never task sources/connections |
| `runtime/source_environment.py` | fresh Compose source stack and per-population Postgres/Mongo/S3 seeding |
| `runtime/source_server.py` | allowlisted REST offset/limit arrays and exact flat-file bytes |
| `runtime/install.py` | atomic public-task copy, credential/ID injection, state stripping |
| `runtime/execution.py` | replay submitted Terraform/Airbyte Stage 1 or submitted dbt Stage 2; emit UTC execution windows plus canonical executed-input, state/job, preflight namespace/container, and dbt-artifact identities |
| `runtime/snowflake.py` | optional DB-API connection plus task-scoped role/user/warehouse/database provisioning |
| `runtime/databricks.py` | SQL Warehouse connection, solver-identity preflight, plus a fresh/dedicated attempt catalog and isolated schema grants |
| `runtime/redshift.py` | Redshift connection plus a fresh attempt-owned database, cluster-global solver user, and isolated S3 staging prefix on an explicitly attempt-dedicated deployment |
| `runtime/evaluation.py` | read-only Stage 1 and Stage 2 Snowflake/Databricks/Redshift parity collection for certification or explicit cloud-agent scoring |
| `export/certification.py` | seal schema-1.2 stage evidence, require Stage 1 to complete before Stage 2 starts, bind revalidated execution inputs, exact dbt model rosters, runner/preflight versions, and warehouse identities, derive exact released relation coverage, and publish an immutable schema-1.3 attestation (with verification-only support for the closed 1.1/1.2 protocol) |
| `semantic/package.py` | verify and identity-bind schema-3.2 private TaskIR, gold, and population roots |
| `semantic/scoring.py` | resource-isolated combined EL+T DuckDB replay with independent reward heads and the shared comparator |

### `runtime/attestation.py` (Phase 5, Table 9 — the sandbox attestation and the fail-closed preflight)

This module records the isolation used for one label-bearing run and performs a
fail-closed host preflight. `SandboxAttestation` is flat, frozen,
`extra="forbid"`, and carries exactly Table 9's roster; it is sealed with the
procedure defined by `export/certification.seal_attestation`: SHA-256 over the
canonical record with the digest field blanked. No secret may enter an
attestation; every field is a version
string, a digest, a bounded label, a boolean or a count.

R-F: the attestation is observed state. It is stamped on records, compared with the
pin at run start, and excluded from the admission fingerprint. The hashed
object remains the pinned `metrology.sandbox` declaration
in `config/agents.yaml` (`providers.sandbox_pin` / `metrology.sandbox_digest`),
so proof 3's cross-process determinism also holds on a host that has not run a
container. `attest_sandbox` refuses an observation that differs from the pin
(`ERROR [sandbox_pin_mismatch]`, exit 2, nothing measured) rather than
recording it.

Every host fact arrives through an injected `runtime.process.Runner`, the
process environment and the platform name, so tests feed recorded `docker
info`, `runc --version`, `runsc --version`, `uname -r` and `sysctl` output and
no test starts a process, invokes Docker or runs `runc`/`runsc`.

```python
SANDBOX_ATTESTATION_SCHEMA_VERSION = "1.2"; SANDBOX_PREFLIGHT_SCHEMA_VERSION = "1.0"
RUNC_MIN_VERSION = "1.4.3"; RUNC_MIN_BACKPORT_VERSION = "1.3.6"   # GHSA-xjvp-4fhw-gc47
RUNSC_MIN_RELEASE = "release-20240325.0"; RUNSC_PINNED_RELEASE = "release-20260817.0"  # GHSA-4fj4-9m67-3mj3
KERNEL_MIN_VERSION = "5.6"; REQUIRED_CGROUP_VERSION = "2"; REQUIRED_CGROUP_DRIVER = "systemd"
ISOLATION_ENV = "ELT_TASKGEN_ISOLATION"; DEV_ONLY = "dev-only"; ATTESTING_TIERS = {"A", "B"}
class PreflightCheck(name, code, required, observed, passed, advisory=False)
class PreflightResult(isolation_mode, tier, host_class, platform, kernel, runtime, cgroup,
                      userns, toolchain, checks, failures, passed, preflight_sha256)
class SandboxAttestation(tier: "0"|"A"|"B"|"C"|"D", host_class, kernel, runtime, platform, cgroup,
                         userns, image_digest, package_digest, runtime_json_sha256, oci_config_sha256,
                         tool_manifest_sha256, diagnostics_version, verifier_code_sha256, loop_limits,
                         sandbox_pin, agents_config_sha256,
                         toolchain, preflight_sha256, contamination_mode, credential_files_in_mount,
                         mount_scanned=False, mount_entries_scanned=0, attested_at="", run_id="",
                         attestation_digest="")            # schema 1.2
class MountScan(scanned, entries_seen, credential_files)
def isolation_preflight(*, runner=None, environ=None, platform_system=None, operator_host_class=None) -> PreflightResult
def attest_sandbox(*, pinned, preflight, mount_root, observed=None, run_id="", attested_at=None, ...) -> SandboxAttestation
def verify_sandbox_attestation(a) -> SandboxAttestation        # reparse, re-seal, re-assert; fail closed
def assert_attestation_matches_agents_config(a, *, agents_config=None) -> SandboxAttestation
def sandbox_attestation_digest(a) -> str; def seal_sandbox_attestation(a) -> SandboxAttestation
def assert_attestation_is_secret_free(a) -> None
def scan_mount_for_credentials(root) -> MountScan
def count_credential_files(root) -> int; def is_credential_shaped(name) -> bool
def pinned_sandbox_declaration(*, agents_config=None) -> dict   # providers.sandbox_pin, one reader
def harness_loop_limits(*, agents_config=None) -> dict[str, int]
def default_verifier_code_sha256() -> str; def default_package_digest() -> str
def default_tool_manifest_sha256() -> str; def default_diagnostics_version() -> str
def default_contamination_mode(environ=None) -> str             # verification.contamination.enforcement
```

Tiers: `A` gVisor (`runsc`) on a Linux host meeting every floor; `B` a hosted
microVM or gVisor sandbox declared by the operator; `C` plain `runc`; `D`
macOS, Docker Desktop or the `dev-only` laptop lane; `0` unclassified, the
fail-closed value for a host whose preflight did not pass. An absent `runsc`
selects tier C. A present `runsc` below the floor, or one whose version cannot
be read (`runsc_version_unobserved`, finding p5-0: a source build or
a binary outside the preflight's `PATH` used to mint a tier A record with an
empty toolchain entry), is a hard failure. Tier A also requires a
version that cleared the floor. The operator host-class declaration
(`ELT_TASKGEN_SANDBOX_HOST_CLASS`) may excuse only the absence of `docker info`.
An engine that answered and reported cgroup v1 is an observed
cgroup v1, and a declaration with no observed runtime or cgroup behind it is
`declared_host_class_uncorroborated` (finding p5-1). The
containerd version is recorded and is never a sufficient pin, and the
unprivileged-userns assertion accepts either `kernel.unprivileged_userns_clone`
or `user.max_user_namespaces` (neither readable is a failure).

`is_credential_shaped` is the repository's shared name vocabulary
(`review/tools/credential_sweep.NAME_RULES`) plus the container-lane names this
module adds. The mount counter therefore uses at least the same name rules as
other surfaces. It matches names without opening files.
`credential_files_in_mount` counts the mount used by the labelled run, not an
attempt `task/` directory. The upstream format requires a placeholder
`<destination>_credential.json` in the latter, so an attempt directory counts
at least one file and the release gate refuses it. This is pinned by
`test_the_attested_mount_is_never_an_attempt_task_directory`.

`mount_root` is required and must be an existing directory. A zero count with
nothing scanned does not establish a clean mount, so the record carries
`mount_scanned` / `mount_entries_scanned` and the gate refuses a label-claiming
batch without a scan (finding p5-3). `attested_at` and `run_id` identify when
the isolation was observed and for which run. This follows
`export/certification.CertificationAttestation` and prevents one record from
supporting every future release (finding p5-10). Every digest field
(`package_digest`, `tool_manifest_sha256`, `verifier_code_sha256`) is
shape-checked as SHA-256 and `diagnostics_version` as a dotted version. The
closed roster therefore enforces field shapes instead of relying on a keyword
denylist for free text (finding p5-8).

Schema 1.2 additionally seals the exact configured sandbox pin and sha256 of
the complete canonical parsed agents document. Active pipeline/freezer and
release-bound attestation paths compare both with their `--agents-config`, so
config-A evidence cannot be replayed under config B even when its sandbox
sub-block is unchanged. Schema-1.1 seals are still reproducible for historical
unlabelled/development readers; the label gate rejects them with
`attestation_config_unbound`.

Pinned by `tests/test_sandbox_attestation.py`
(`test_sandbox_attestation_is_secret_free_and_sealed`,
`test_sandbox_attestation_records_no_credential_in_mount`,
`test_isolation_preflight_fails_closed_on_old_runc`,
`test_preflight_asserts_runc_floor`, `test_preflight_asserts_runsc_floor`,
`test_macos_runs_are_dev_only_in_attestation`,
`test_no_test_ever_runs_a_real_command`).

### `export/attestation_gate.py` (Phase 5, Table 9 — no RLVR labels without attested isolation)

This gate permits a batch claiming RLVR labels to be frozen and verified only
when it has an isolation attestation. The attestation must be sealed with a
reproducible digest, use tier A or B, use contamination enforcement `enforce`
matching the caller's mode, and report no credential-shaped file in the mount.
Every refusal has a type and code. Callers print `ERROR [code]` and exit 2.

`release.require_attestation` defaults to `True` and can relax requirements
only for a batch that claims no RLVR labels. A label-claiming batch cannot skip
the gate. By default, a batch claiming no labels still requires a valid sealed
attestation but is not tier-gated, allowing the `dev-only` laptop lane to freeze
an unlabelled release (roadmap rollback row). An uninterpretable manifest shape
is treated as claiming labels. A self-declared `claims_rlvr_labels: false` is
honored only when no other manifest field contradicts it. Contradictory variant
acceptance, shipped RLVR variants, or corpus profile produce
`batch_manifest_invalid` (finding p5-2). A tier B record whose runtime or cgroup
evidence is empty is `attestation_tier_uncorroborated` (tier B rests on an
operator declaration), an attestation that scanned no mount is
`mount_not_scanned`, and one minted for another run or older than
`MAX_ATTESTATION_AGE_S` is `attestation_run_mismatch` / `attestation_stale`.

```python
RELEASE_REQUIRE_ATTESTATION_DEFAULT = True; REQUIRED_CONTAMINATION_MODE = "enforce"
REFUSAL_CODES = {attestation_missing, attestation_invalid, attestation_unsealed,
                 attestation_digest_mismatch, attestation_tier_refused,
                 attestation_preflight_missing, attestation_secret_material,
                 contamination_mode_unknown, contamination_mode_refused,
                 contamination_mode_mismatch, credential_files_in_mount,
                 mount_not_scanned, attestation_tier_uncorroborated,
                 attestation_stale, attestation_run_mismatch, batch_manifest_invalid}
MAX_ATTESTATION_AGE_S = 30 * 24 * 60 * 60
class AttestationRefusal(RuntimeError): code: str
class AttestationGateResult(claims_rlvr_labels, required, tier, attestation_digest,
                            contamination_mode, reason)
def require_attestation_for_labels(batch_manifest, attestation, *, contamination_mode,
                                   config=None, run_id="", now=None,
                                   max_age_s=MAX_ATTESTATION_AGE_S) -> AttestationGateResult
def batch_claims_rlvr_labels(batch_manifest) -> bool     # duck-typed; unknown shape = claims
def recompute_attestation_digest(attestation) -> str     # for verify_release
def require_attestation_setting(*, config=None) -> bool
```

`export/release.py` does not yet call the gate. The required `freeze_release`
and `verify_release` wiring is defined by the first two entries in
`docs/plans/bounded_agents_phase5.md`. Pinned by
`tests/test_attestation_gate.py` (`test_release_refuses_tier_c_and_d_labels`,
`test_release_refuses_observe_mode_attestation`,
`test_labels_can_never_skip_the_gate_however_the_setting_is_set`,
`test_unsealed_tampered_and_alien_records_are_refused`,
`test_corpus_profile_constants_match_export_release`).

### `runtime/parity.py` and `elt-taskgen-parity-sample` (Phase 5, Table 9 — the four-component parity battery)

The battery measures how often the real stack disagrees on artifacts accepted
by the local DuckDB comparator. It has four components: (1) differential replay
of a stratified sample through
`terraform validate -json` (format 1.0, parsed strictly) and the `runtime
verify-stage1` / `verify-stage2` / `verify-end-to-end` verbs per destination,
with injected Terraform and destination runners. This module builds argv and
interprets receipts but does not start a process, open a socket, or read a
credential; (2) proxy-rule mutation, where each mutant drops one comparator
rule and a mutant that survives fails the acceptance lane; (3) metamorphic TLP
and NoREC checks over the DuckDB comparator on the committed benchmark fixture;
(4) sampled real-warehouse replay, which runs component (1) with the
destination runner bound to the owner's real destination.

This is not a second reward implementation. The reference verdict is always
`verification/upstream_eval.compare_mart`, pinned equal to `ComparatorRules()`
at its defaults, and the mutants exist only to prove the battery can tell a
weakened comparator from that one. `wilson_interval` is imported from
`review/metrology.py`; there is one interval implementation, not two.

The Table 9 sizing floors are checked against both the drawn sample (the
sampler's pre-replay check, `accepted_below_minimum` / `rejected_below_minimum`
/ `source_pool_below_minimum`) and the post-fault denominator
(`denominator_below_minimum:<arm>:<judged>/20`), and `adopted` depends on the
second. A real-runtime fault reduces the denominator, so an arm with too many
faults must report the sample-size shortfall instead of a clean rate (finding
p5-4).

```python
PARITY_REPORT_SCHEMA_VERSION = "1.0"; PARITY_BATTERY_VERSION = "1"
DEFAULT_CONFIDENCE = 0.85; SECONDARY_CONFIDENCE = 0.95; AGREEMENT_LOWER_BOUND_THRESHOLD = 0.90
MIN_LOCALLY_ACCEPTED_PER_ARM = 20; MIN_LOCALLY_REJECTED_PER_ARM = 10; MIN_ARTIFACTS_PER_SOURCE_POOL = 4
TERRAFORM_VALIDATE_FORMAT_VERSION = "1.0"; TERRAFORM_VERSION_PIN = "1.15.8"; PYTHON_HCL2_PIN = "7.3.1"
class ArtifactVerdict(artifact_id, arm, destination, source_pool, population, stage,
                      local_accepted, real_accepted, reward, fault_code)
class PopulationSet(name, arm, destinations, source_pools, populations, stages,
                   artifact_ids, size, unmeasured_artifact_ids, frame_digest)
                   # `artifact_ids`/`size` ARE the pi_c denominator; the
                   # artifacts the real arm could not measure are named apart,
                   # and `name` is the derived description with the caller's
                   # annotation appended (finding p5-5)
class ParityReport(arm, population_set, claim, confidence, denominator, disagreements,
                   agreements, runtime_faults, fault_codes, pi_c, pi_c_upper_85/95,
                   agreement_lower_85/95, mcnemar_*, sizing_shortfalls,
                   quarantine_artifact_ids, adopted, sandbox_attestation_digest, report_digest)
def parity_rate(accepted, *, confidence=0.85, population_set_name="", attestation=None) -> ParityReport
def differential_replay(specimens, *, destination_runner, terraform_runner=None)
def runtime_verify_argv(*, release, task_id, stage, destination_credential, ...) -> tuple[str, ...]
def parse_terraform_validate_json(payload) -> TerraformValidateResult   # strict, format_version 1.0
def run_mutation_battery(cases, *, comparator=...) -> MutationReport
def run_metamorphic_checks(connection, cases) -> tuple[MetamorphicFinding, ...]
def run_acceptance_lane(...) -> AcceptanceLaneResult
def write_parity_report(report, reports_dir) -> Path    # seals on the way out
def seal_parity_report(r) -> ParityReport; def verify_parity_report(r) -> ParityReport
```

Under C7, a real-runtime fault has `reward=None`, is counted in
`runtime_faults`, and is excluded from the `pi_c` denominator. A local-accept /
real-reject finding quarantines the task rather than a training label. The
report is sealed and names the verified `SandboxAttestation` used by its real
arm; measurements from different runtimes are not equivalent.

`elt-taskgen-parity-sample` is the supported, installed JSON-in/JSON-out driver
backed by the packaged `tools/parity_sample.py` resource. `sample` draws the deterministic
stratified sample, `argv` prints the exact `elt-taskgen runtime verify-*`
command line for each specimen so the owner runs it with an operator credential
that never enters this process, and `report --attestation sandbox_attestation.json`
publishes `reports/parity_<arm>.json`. Pinned by `tests/test_runtime_parity.py`
(`test_parity_report_treats_runtime_faults_as_none_not_disagreement`,
`test_parity_claim_names_its_population_set`,
`test_emulator_mutation_fails_the_acceptance_lane` (no vendor emulator is
claimed or built anywhere: the "emulator mutation" is a weakened DuckDB
comparator rule, and the test proves the lane rejects it),
`test_terraform_validate_json_format_1_0_parsed`,
`test_python_hcl2_pinned_exactly`,
`test_parity_report_is_sealed_and_names_the_isolation_it_measured`).

### `runtime/model_copy.py` (Phase 5, threat row A22 control 2 — the model copy and the replay copy)

The model-facing and harness-owned replay copies use separate directories. The
model-facing copy contains only the placeholder credential file, with every
string empty as required by `export/eltbench.assert_public_runtime_shape`, and
a `config.yaml` byte-identical to the frozen public bundle. The harness-owned
replay copy is not created while the policy is running:
`release_model()` gates `install_replay()`, and the replay copy is the one the
`runtime verify-*` verbs execute in. On the cloud lane the attempt-scoped
principal is revoked at cleanup through an injectable hook. No cloud call is
made from this module or its tests, and a sealed receipt records what happened.

Until the roadmap's `inject_credentials=False` parameter exists on
`install_task`, this module calls the sanctioned public installer with the
harness sentinel `"placeholder"`. `credential_sweep` treats this value as a
placeholder, so no transient live value exists in the model tree. The module
then restores `config.yaml` and the credential file from the frozen public
bundle. The result is verified before the directory is handed over and the tree
is destroyed if the verification does not hold.

```python
LANE_LOCAL = "duckdb"; LANE_CLOUD = "cloud"     # docker_lane_for(): none / proxy-bridge
MODEL_COPY_DIRNAME = "task"; REPLAY_COPY_DIRNAME = "replay"
GRADER_LANE_PROGRAMS = {"dbt", "docker", "terraform"}; AGENT_ARGV_MARKERS = (...)
INTERPRETER_ARGV_TOKENS = {python, python3, sh, bash, node, uv, ...}    # whole tokens, never substrings
DOCKER_ALLOWED_SUBCOMMANDS = {run, image, pull, version, info}
DOCKER_REFUSED_FLAGS = (--entrypoint, --privileged, --cap-add, --security-opt,
                        --pid, --userns, --device, -v, --volume, --mount)
CANDIDATE_TREE_DOTENV_RULE = "dotenv"; MAX_CANDIDATE_TREE_ENTRIES = 200_000
class ScopedPrincipal(principal_id, destination, ...)   # a NAME; nowhere to put a secret
class CleanupReceipt(..., principal_revoked, live_credential_files, model_released,
                     agent_process_count, model_call_count, receipt_digest)
class GraderLaneRunner(runner, *, lane=LANE_LOCAL, programs=..., check_candidate_tree=True)
class AttemptCopies: model_dir; replay_dir; release_model(); install_replay(creds);
                     grader_runner(runner); cleanup() -> CleanupReceipt
def install_task_for_model(public_task_dir, work_dir, *, destination=None, lane=LANE_LOCAL,
                           attempt_id=None, principal=None, revoke_principal=None,
                           credentials_provider=None) -> AttemptCopies
def placeholder_credential(public_task_dir) -> dict; def assert_placeholder_credential(payload, *, label)
def live_credential_findings(root, *, include_policy_tree=True) -> list[CredentialFinding]
def assert_model_copy_is_credential_free(root, *, include_policy_tree=True) -> None
def assert_no_agent_process(argv) -> None                  # agent AND interpreter tokens
def assert_docker_argv_is_replay_shaped(argv) -> None     # a plain pinned-image run, nothing else
def candidate_tree_dotenv_paths(root) -> tuple[str, ...]
def assert_candidate_tree_is_image_safe(root) -> None    # harness twin of the runner-image pin
def seal_cleanup_receipt(r) -> CleanupReceipt; def recompute_receipt_digest(r) -> str
def verify_cleanup_receipt(r, *, require_complete=False, expected_grader_commands=None) -> CleanupReceipt
```

The certification lane replays a sealed, locally authored artifact with zero
model calls. `GraderLaneRunner` allows the replay container to run
only `dbt`, `docker` and `terraform`, never an interpreter and never an
agent-shaped argv. A `docker` argv must be a plain pinned-image `run`; allowing
only `argv[0]` would permit `docker run --entrypoint python` (finding p5-7).
`agent_process_count` is derived from the forwarded argv rather than asserted
as zero. `CleanupReceipt.live_credential_files` is measured at receipt time
over files that survived cleanup, and
`model_released` says whether `release_model()` ran at all (finding p5-6). It
refuses a candidate tree carrying a `.env`-shaped file before the tree is
mounted, matching the runner-image pin. The module does not read, format,
return, or log credential values. Assertions cover
only file shape, name, and emptiness. Pinned by
`tests/test_runtime_model_copy.py`
(`test_install_task_without_injection_leaves_placeholder`,
`test_attempt_cleanup_revokes_scoped_principal`,
`test_grader_container_never_ran_agent_process`,
`test_candidate_tree_dotenv_is_refused_before_any_mount`,
`test_cloud_image_rejects_dotenv_in_candidate_tree` — skipped until the
runner-image pin is implemented) and `tests/test_zero_cloud_rollout.py`
(`test_ordinary_rollout_path_makes_zero_cloud_calls`).

The generic receipt reader remains schema-1.0 compatible. Certification-complete
verification requires non-zero grader command evidence, and the lifecycle
binds it to the staged attempt roster at exactly four commands per population:
Terraform init/apply for Stage 1 and dbt `--version`/`run` for Stage 2. The
current cleanup schema exposes only an attempt-wide count, so this exact
population-derived check is the strongest stage binding it can express.

### `training/*` — `workspace-v1` L1 artifact-workflow proxy

This package implements the cloud-free L1 artifact scorer. It is deliberately
versioned apart from, and does not modify, the `semantic-v1` JSON scorer:

| module | implemented contract |
|---|---|
| `training/contract.py` | Defines submission schema `workspace-v1`, result schema `workspace-result-v1`, scorer version `0.2.3`, claim `artifact_workflow_proxy`, a harness-owned dbt profile, minimum hidden-population aggregation, bounded artifact limits, and the immutable `WorkspaceErrorCode` -> `WorkspaceFailureClass` map. Only policy failures/violations are label-eligible. |
| `training/models.py` | Provides frozen, extra-forbidden manifests, action traces, failures, per-population heads, and aggregate results. Candidate file identity is the digest of a unique path-sorted manifest. Strict EL requires exact Terraform-contract and strict-raw heads plus a successful sync lifecycle; end-to-end reward is then gated again on raw immutability. Invalid/policy-violation submissions score zero, while task, harness, infrastructure, and real-runtime failures produce no training label. |
| `training/package.py` | Loads and verifies one combined release, binds it to its private `TaskIR`/gold/source package, requires the current `documentation/README.md` layout, validates destination namespace and private Airbyte connector routing, recursively freezes and hashes that connector contract, and records full-public and non-`elt/` tree digests. |
| `training/workspace.py` | Installs an exact public task into a fresh attempt, keeps the public specification read-only and `task/elt/` writable, seals only admitted candidate files beneath `elt/`, and replays the sealed bytes into another fresh install. Sealing requires `main.tf`, `dbt_project.yml`, `models/sources.yml`, and at least one model SQL file; it rejects symlinks, non-regular files, case collisions, transient state, oversized content, unsupported paths, and high-confidence secret literals. |
| `training/terraform_intent.py` | Parses candidate HCL with `python-hcl2`, rejects unsafe Terraform constructs, normalizes Airbyte sources/destination/connections/dependencies, and compares exact routing, stream, sync, namespace, and destination intent without depending on resource labels or formatting. |
| `training/namespace.py` | Projects Snowflake database/schema, Databricks catalog/schema, and Redshift connected-database/schema intent into deterministic attempt-local DuckDB namespaces. |
| `training/local_sync.py` | Uses trusted TaskIR/source-rendering readers to load only candidate-selected Postgres, MongoDB, REST, S3, and file streams into a fresh DuckDB database, then calculates upstream counts plus strict typed raw evidence. |
| `training/dbt_runner.py` | Validates the exact locked dbt runtime, injects a harness-owned profile, enforces the closed portable SQL policy, runs real dbt parse/compile/full-refresh, validates persistent marts and private evidence, and fingerprints physical raw state before and after dbt. |
| `training/scorer.py` | Replays one authenticated seal for every graded population in strict Terraform -> local sync -> raw verification -> same-state dbt -> immutability -> mart order, gates end-to-end reward, and aggregates by the population minimum. |
| `training/signal.py` | `workspace-signal-v1` (roadmap Phase 2 item 2.b): the pure projection of one `WorkspaceScoreResult` into the training scalar — `label_valid` first (reward `None` is never a label), then a policy violation (a `POLICY_VIOLATION` failure, or a Terraform violation code, `dbt_unsafe_artifact` or `dbt_raw_mutated` in any graded population) zeroes the reward and the EL credit, then the planted-infeasible canary, then an invalid submission, else the gated composition below; the whole-group drop rule of the rollout adapter. `score_workspace` is untouched. |
| `training/env.py` | The declarative, single-step, Docker-free environment: the policy emits one `elt/`-confined artifact, the harness installs, seals, scores through the public `score_workspace` and projects with `training_signal`; the outer supervisor deadline around the grader lives here and yields label `None`, never `0.0`. No tool surface, no `train` subcommand (2.c is deferred to a Tier A/B host). |

```python
# training/signal.py                                              (workspace-signal-v1)
SIGNAL_VERSION = "workspace-signal-v1"; DEFAULT_W_T = 1.0
POLICY_VIOLATION_CODES: frozenset[str]   # TERRAFORM_POLICY_VIOLATION_CODES + dbt_unsafe_artifact + dbt_raw_mutated
class WorkspaceTrainingSignal(BaseModel):  # frozen, extra="forbid"
    signal_version: str = SIGNAL_VERSION; scorer_version: str; artifact_sha256: str
    label_valid: bool; el_pass: bool; policy_violation: bool
    r_el: float; r_t: float; w_t: float = 1.0
    reward: float | None                 # None iff not label_valid; 0.0 on violation / invalid / canary hit
    first_failed_phase: Literal["none", "terraform", "sync", "dbt", "mart", "immutability", "workspace"]
    canary_task: bool = False; canary_hit: bool = False
    populations: Mapping[str, PopulationWorkspaceScore]   # trainer-visible diagnostics only
def training_signal(result: WorkspaceScoreResult, *, w_t=1.0, infeasible_task=False,
                    aborted_infeasible=False) -> WorkspaceTrainingSignal
    # R = (r_EL + 1[el_pass] * w_T * r_T) / (1 + w_T); r_EL = 1.0 iff el_pass (valid
    # submission, no failure, a NON-EMPTY graded set, every graded population
    # strict_el_pass, no violation); r_T = result.reward (the graded minimum).
    # Monotone: fail 0, EL-only 1/(1+w_T) (0.5 shipped), end-to-end up to 1.
    # infeasible_task is the task pool's PRIVATE planted flag, aborted_infeasible the
    # harness's record that abort(reason_code="infeasible") was the last action:
    # a pass there is canary_hit (0.0), the abort earns 1.0; neither is read from the artifact.
def first_failed_phase(result) -> str    # the binding population's first failed phase, scorer order
def drop_group_if_unlabelled(group: Sequence[WorkspaceTrainingSignal]) -> bool
    # True when ANY member has label_valid False: the adapter discards and re-samples
    # the WHOLE group with fresh state; never a filtered subset, never a spliced replacement

# training/env.py                                        (2.b; single-step, Docker-free; C9)
DEFAULT_GRADER_DEADLINE_S = 1800.0; GRADER_NAME = "score_workspace"
GRADER_ENTRY = "elt_taskgen.training.scorer:score_workspace"   # the grader child's entry
def default_grader_deadline_s(dbt_limits=None, *, populations=None, ...) -> float
    # populations x (3 x command_timeout + trusted budget) + slack, floored at 1800 s:
    # a candidate hitting its own dbt caps lands a measured dbt_timeout INSIDE the window
class Observation(task_id, attempt_root, task_dir, elt_dir, documentation, public_files)
    # the installed PUBLIC task only: never a release root, a private path, gold,
    # a hidden population or a count
class StepResult(observation, signal, result, done=True, harness_fault="", harness_fault_code="",
                 seal_sha256="", sealed_dir=None)
class DeclarativeEltEnv(release_dir, *, attempts_root, runtime_config, dbt_limits=None,
                        grader_deadline_s=None, w_t=1.0, scorer=score_workspace,
                        grader_entry=GRADER_ENTRY, infeasible_tasks=(), verify_release=True):
    def reset(self, task_id: str) -> Observation      # load_workspace_package + install_workspace
    def step(self, artifact: Mapping[str, bytes], *, aborted_infeasible=False) -> StepResult
        # write elt/ (WorkspaceArtifactFile path rule plus: no control character, no
        # component over NAME_MAX, NFC-normalized case-insensitive uniqueness; a refused
        # path, a write that failed on the policy's own name or an empty artifact is a
        # classified 0.0 built from the public models, the grader never runs; a host
        # OSError while writing, a seal-time I/O fault and a replay-time workspace code
        # the policy cannot cause are harness faults, label None), seal_workspace, then
        # score_in_child(score_workspace) on the complete graded set — an injected scorer
        # double runs in-process under supervised_score — training_signal; done=True;
        # a second step before reset is refused
    def close(self) -> None                            # removes exactly the episode tree reset created
class GraderChildSpec(release_dir, task_id, verify_release, sealed_dir, seal_sha256,
                      attempts_root, runtime_config, dbt_limits, entry=GRADER_ENTRY)
def score_in_child(spec, *, deadline_s, scratch_dir, name=GRADER_NAME)
    -> tuple[WorkspaceScoreResult | None, SessionFault | None]
    # the REAL grader in an env-owned child (start_new_session=True): the child reloads the
    # package via load_workspace_package and calls the public score_workspace on the sealed
    # dir authenticated by seal_sha256, writing result JSON to scratch_dir; at the deadline the
    # child's process group (and every session it detached) is KILLED and waited for ->
    # ToolDeadlineExceeded; a raising child ToolHarnessFault (class name only), a dead child
    # SandboxFault, a non-result non_result: each is label None, NEVER 0.0
def supervised_score(scorer, *args, deadline_s, name=GRADER_NAME, **kwargs)
    -> tuple[WorkspaceScoreResult | None, SessionFault | None]
    # the in-process path for an INJECTED double only (a thread cannot be killed);
    # a deadline is ToolDeadlineExceeded, a raising grader ToolHarnessFault (class name
    # only), a non-result a harness fault: each is label None (unlabelled_signal), NEVER 0.0
def unlabelled_signal(*, artifact_sha256, scorer_version=WORKSPACE_SCORER_VERSION, w_t=1.0)
```

The signal is what a trainer consumes: one scalar per trajectory, mean-
centred by the trainer without per-group std, `signal_version` and
`scorer_version` on every record so a DuckDB re-pin is a versioned change.
Only the pre-declared candidate-attributable caps inside the grader (dbt
`command_timeout`, `dbt_output_limit`, the `MAX_WORKSPACE_*` bounds) are
measured zeros; a supervisor deadline is the harness's. No process reward,
no per-turn term, no length or efficiency term ever enters the scalar
(Output 8 §8.1).

`WorkspaceSubmission` contains only solver-authored `elt/` artifacts. Public
inputs, credentials, DuckDB files, `profiles.yml`, Terraform state, provider
caches, dbt `target/`, and logs are not submission artifacts. A replay starts
from the verified public package and copies the sealed candidate bytes; it does
not reuse an attempt directory or runtime state.

Read-only modes prevent accidental edits; they are not an ownership boundary
against the same uid. The harness must end solver access before sealing or put
the seal in harness-inaccessible storage. In-process replay uses a non-serialized
capability; cross-process replay must retain and supply the expected seal digest.
The last-written `attempt.json` or `workspace.json` is the completion record for
an exclusively claimed directory.

This is an L1 artifact and semantic workflow proxy. It parses Terraform intent
but does **not** execute the Terraform binary, emulate the Airbyte control
plane, start source services, or invoke a cloud destination. The interactive
tool sandbox and offline Terraform/Airbyte protocol lifecycle remain L2 work;
the real Airbyte and cloud-destination runners remain the separate sparse
certification path.

### `cli.py`

```python
def main(argv: list[str] | None = None) -> int
```

`elt-taskgen --help` defines the subcommand list. The `demo` subcommand was
removed on 2026-08-14. Current commands are `record-transcripts`,
`metrology`, `ingest-dbt`, `ingest-synsql`, `ingest-schemapile`,
`ingest-wikidbs`, `ingest-dlt`, `measure-target` (deprecated alias
`ingest-anchor`), `generate`, `reference-run`, `review`, `attack`, `validate`,
`validate-el`, `validate-t`, `calibrate`, `select`, `export`, `release`,
`pipeline`, `verify`, `score`, `semantic`, `runtime`. `semantic`
contains `score`. `runtime` contains
`install-airbyte`, `bootstrap-task`, `prepare`, `source-up`, `source-down`,
`provision-snowflake`, `reset-snowflake`, `provision-databricks`,
`provision-redshift`, `run-stage1`, `resync-stage1`, `run-stage2`,
`verify-stage1`, `verify-stage2`, the gated `verify-end-to-end` cloud parity
collector, `certify-difficulty`, the high-level `certify` finishing command,
and the `certification` lifecycle group (`attest-unbound`, `attest`, `begin`, `status`,
`record-stage1`, `record-stage2`, `record-observations`, `record-cleanup`,
`complete`).
`source-up` seeds the selected source population and then requires the local
Airbyte ingress, kind API server, scheduler, and controller manager to remain
healthy for a sustained stability window before it returns; a timeout attempts
to clean up that source stack instead of allowing a flaky Stage 1 submission,
and reports explicitly if cleanup fails.
`prepare` refuses a `--work-dir` under any `runs/` directory or under a release
root (a usage error, exit 2, raised before any credential file or the release
is read), because the attempt copy it installs holds live credentials.
The three verifier commands accept `--certification-strict`, which preserves
the upstream-compatible reward fields while requiring separate exact typed
raw/mart fingerprints; strict end-to-end verification does not enter Stage 2
after an exact-content Stage 1 failure. Global flags: `--workspace PATH`
(default `runs/default`), plus provider wiring `--agents-config PATH`,
`--replay-only`, `--record`, `--budget-per-task USD` (default 5.00; metrology
runs with `--budget-per-task 60`; a bounded-proposer profile is documented at
7.00), `--budget-total USD`, `--no-repair-proposer` (the explicit disable),
`--repair-proposer-mode {one_shot, bounded}` (default `bounded`; `one_shot` is
the compatibility implementation),
`--repair-attempts N`.

**Exit codes.** `0` = stage passed; `1` = task rejected (a task defect; that
workspace keeps the rejection); `2` = could not measure or could not decide —
argparse/usage errors, `CliUsageError`, `EngineError` (unknown task, unwired
stage, non-convergence), `InfrastructureFailure` (transport / credentials /
admission / budget: nothing is rejected and a re-run resumes at the failed
stage), and a run that stopped as `BLOCKED`. `main()` has a closed error
boundary around exactly those types. Any other exception produces a traceback.

* `record-transcripts [--task-id ID] [--out DIR] [--force] [--loader-only]` —
  the one-time seeding step: runs author and council live for a task, refuses
  to run without API keys, and persists every (role, prompt) transcript. It is gated
  by the same `council.live_admitted` record as the review stage — recorded
  transcripts are replay evidence, so they may not be recorded under an
  unproven council; only `--loader-only` is exempt. The default output is
  `<workspace>/transcripts/`. A witness whose `session:` block is enabled in
  the agents document used for routing is seeded as a complete trajectory
  (Phase 2, roadmap Table 6): no one-shot exchange is recorded for
  it, and the second pass runs the gate's own entry point
  (`run_independent_build` / `run_independent_load_build`) against the frozen
  reference so every session turn is recorded under the key the gates stage
  serves. Both witness blocks ship enabled; disabling one restores its
  byte-identical one-shot exchange.
* `--repair-proposer [--repair-proposer-mode {one_shot, bounded}]
  [--repair-attempts N]` (global flags, every stage-running subcommand) — the
  repair proposer is enabled by default and uses the bounded implementation
  (it makes live calls and commits workspace edits only when a held trial patch
  passes certification). `--no-repair-proposer` disables it, and
  `--repair-proposer-mode one_shot` selects the byte-compatible legacy
  `RepairProposer`; otherwise `cli._make_repair_proposer` selects the Phase 1
  `AgenticRepairProposer` (a tool-using session on a held trial copy, at most
  `repair.max_attempts` sessions per failure on the routes
  `repair.routes_bounded` names, certified by the unchanged `attempt_patch` at
  submit). Both implement `engine.RepairProposerLike`, so the engine never
  changes. A mode outside `REPAIR_PROPOSER_MODES` is a `CliUsageError` (exit 2).
  The bounded profile is documented at `--budget-per-task 7.00`. In either
  mode a certification member that
  could not measure or returned a waiting result halts as could-not-measure (exit 2, no round,
  nothing rejected, the ledger resumes at the failed stage).
* `verify --release DIR` — runs `export/release.verify_release` and prints one
  line per failed file; exit 0/1. For schema 3 it also enforces one combined
  public parent per task, rejects public source fixtures/DuckDB/retired runtime
  concepts, and checks the private oracle census. This is the check `shasum -a
  256 -c checksums.sha256` cannot do on its own: private `.duckdb` oracles are
  census-pinned, not byte-pinned, and `shasum` skips them.
* `score --release DIR --unit ID --duckdb FILE [--population NAME]` — scores
  only a legacy schema-2 split release through `export/serve.py`
  (`load_release_unit` / `score_release_unit`). It requires an `__el` or `__t`
  unit id and routes comparisons through `upstream_eval`. It deliberately
  refuses schema-3 profile `eltbench_end_to_end` / layout `combined` because
  combined scoring has a separate versioned surface. `runtime verify-stage1` and
  `runtime verify-stage2` remain cloud certification/benchmark collectors and
  must not become the default high-volume substitute.
* `semantic score --release DIR --task-id ID --submission FILE [--population
  NAME] [--json]` — verifies a schema-3.2 private semantic package and replays
  one exact v1 combined submission. With no `--population`, it scores every
  hidden graded population and aggregates by minimum. EL and T run on separate
  in-memory DuckDB connections; T receives a trusted source load, so a failed
  EL plan does not erase the diagnostic T signal. The final reward remains EL
  gated. The attempt runs in a killable worker with bounded wall time, memory,
  threads, disk spill, SQL shape, and result size. Attempt failures return
  structured zero rewards; package/gold/source failures exit 2.
* `metrology [--seed N] [--workers N] [--diagnostic-order-check]` — the
  council efficacy harness. The tampered and clean mix is sampled from
  `review/metrology.py`'s specimen pool — 105 specimens over the three frozen
  fixture families of `review/metrology_fixtures/` (`demo`, `clinic_visits`,
  `stock_ledger`), with a family-stratified draw and two specimens held back
  per family: `--seed` selects
  which specimens are drawn and in which order, and defaults to a fresh
  random seed per run so the schedule cannot be predicted by a provider that
  counts calls instead of reading prose. The seed and the pool digest are
  printed, recorded in `reports/metrology_report.json`, and recorded in
  the `council.live_admitted` admission record (schema 4, harness "6"). Every
  trial runs in a fresh nonce-named workspace holding the public projection
  alone (`metrology.trial_workspace`) between `provider.begin_trial` and
  `end_trial`, with a per-trial validator executor that caches nothing. Live
  mode always bypasses transcript caches and the record carries a
  digest-bound fresh-live trajectory manifest: one row per (trial, seat)
  with the SoT T8 counts, `replayed_model_call_count` and
  `stale_tool_result_count` both required zero, and the count identities
  re-derived on read. Beside the 110 scored trials (25 tampered + 10 clean
  per seat), every run draws three canary trials per seat: a private-only
  literal (exists only in `reference.sql_by_mart`), a forbidden-validator
  literal (exists only in `attack_cases[].mutation`) and an impossible
  specimen (consistent surfaces carrying the seat's own vocabulary around a
  decoy note); a canary hit is a blocking bar (`max_canary_hits: 0`, reason
  `canary_hit`), and canary trials never enter the recall or nitpick
  denominators. The run refuses to start (exit 2) when the installed duckdb
  or sqlglot differ from `metrology.toolchain` in config/agents.yaml, and a
  harness fault (`review.session.SessionFault`) exits 2 with no verdict and
  no tombstone. Passing a recorded seed with `--replay-only` can reproduce a
  diagnostic report, but replay never writes or revokes admission and exits
  as could-not-measure. The routing fingerprint the record binds to is
  fingerprint v5: `{harness_version, pool_sha256, view_sha256,
  observable_state_sha256, model_call_deadline_version, tool_surface_sha256,
  validators, sandbox, api,
  roles: {role: {provider, model, max_tokens, effort, behavior_sha256,
  loop_limits}}}` — the rendered
  rendered critic view. Editing that view makes the admission stale;
  `behavior_sha256` is `providers.role_behavior_sha256`, the
  behaviour-manifest digest, so editing a critic's system prompt, wire tool,
  tool-choice policy, loop limits, the correction text, `SCHEMA_RETRIES` or
  the sandbox pin stales it too; `tool_surface_sha256` covers the critic-wired
  tool surface only (the forced `report_findings` objects, the wired harness
  validators, their module source digests, and pinned binary versions) and is
  compared before the fingerprint is read so a refusal names the tool
  surface; the global `DIAGNOSTICS_VERSION` and the toolchain manifest are
  recorded beside it and not hashed (roadmap R-H). Schema-2 and schema-3
  records, and records from earlier harnesses including "5", are refused as
  superseded before evidence is read. Per seat the CLI prints, beside the
  recall / precision / nitpick verdict and the per-specimen margins, an
  `INTEGRITY` line (canary hits by kind, private probes, policy violations with
  their Wilson upper bound — the three blocking bars of `metrology:` in
  `config/agents.yaml`), an `EFFICIENCY` line (stuck, limit-stopped, format-retry
  and compile-correction trials, nudges, validator runs, the wasted-call ratio
  — advisory until harness "7") and a `CLUSTERING` line (ICC, `n_eff`, `pass^k`),
  then the position-dependence probe (reported, not gated). The caught tuple
  that exits 2 with no verdict and no tombstone covers the harness-fault
  classes (`SessionFault`, `DiagnosticTripwire`, `PolicyFault` raised outside
  a session's own catch, `trajectory.ChainError`) beside the transport,
  budget and protocol errors. `--workers N` runs the trials through N
  `RoutedProvider`s over one routing, transcript store and cost meter
  (`providers.RoutedProviderPool`, one per worker, OQ-20); the evidence is
  sorted by trial index before the manifest is hashed, so the admission
  digests are identical at any N
  (`test_metrology_manifest_sha_is_identical_at_workers_1_and_8`).
  `--diagnostic-order-check` runs the seed's own schedule (scored and canary
  trials) in canonical `sha256(specimen name)` order instead of the seeded
  shuffle — an order-dependence probe — and always exits 2: it never writes or
  revokes admission.
* `admission audit [--revoked [PATH]]` — read-only. It lists every recorded
  trajectory of the workspace's transcript store (one-shot entries, session
  records and the content-addressed trajectory records) that ran under the
  admission record at PATH — by default the record this workspace consults,
  `$ELT_TASKGEN_ADMISSION` or `<workspace>/state/council.live_admitted` — by
  its stamped `admission_record_path` or, when the record is a tombstone, its
  withdrawn routing fingerprint. Revocation is prospective (metrology
  redesign §11), so this command identifies work performed under a withdrawn
  admission. It writes nothing.
* `review --task-id ID` — the review stage's transcript manifest
  (`_validated_review_manifest`) accepts, per critic role, either a one-shot
  row (`entry_schema` at most 2; `correction_count == attempt_count - 1`,
  the current rule) or a session row (`entry_schema` 3, a harness-validated seat's
  trajectory) carrying the SoT T8 count fields and the identity
  `model_call_count == tool_call_count + refused_count + nudge_count +
  correction_count + terminal_count + limit_stop_count`
  (`providers.exchange_row_problems`); `attempt_count` is aliased to
  `model_call_count` for one release (OQ-19,
  `test_review_manifest_attempt_count_alias_holds_one_release`). The
  `finding_count` / `zero_findings` cross-check holds for both shapes, and a
  row that breaks the identity is an integrity problem
  (`test_review_manifest_refuses_a_trajectory_row_that_breaks_the_count_invariants`).
* `export --task-id ID [--variants accepted|full,el,t]` — writes the frozen-gold
  combined parent bundle; `--variants accepted` is the default and additionally
  re-emits the EL/T internal variants whose own current batteries passed.
  Explicit variant lists remain diagnostic/curation controls.
  `freeze_release` ships `tasks/<id>/task` once, keeps suffix reward evidence
  and DuckDB oracles private, and never copies variant task trees into
  schema-3 `public/`.
* `calibrate --task-id ID [--empirical] [--variants full,el,t] [--recalibrate]`
  — standalone difficulty measurement. This subcommand defaults to structural
  measurement only; `--empirical` wires
  `corpus/calibration.py` into stage 9: each variant is measured separately on
  its internal battery bundle against the pinned `calibration.roster`, scored solely by
  `upstream_eval`, and cached by (content hash, variant, roster fingerprint) so
  a re-attest sweep uses the cache. `--recalibrate` bypasses it. An empirically
  impossible variant fails the stage with route `SPECIFICATION` and requires
  feasibility review. Without keys or transcripts, the campaign records a
  visible skip in the stage payload. The structural measurement remains, and no
  empirical value is created.
  In contrast, `pipeline`, `select`, and `release` require current empirical
  EL+T evidence by default and run a missing/stale campaign; their explicit
  `--allow-structural-difficulty` option is development-only.
---

## Removed: `demo` (2026-08-14)

`demo` is not a subcommand. Requesting it exits 2 with an argparse `invalid
choice` error. The parser, Makefile, and runbook do not offer it. The offline
test suite (`python -m unittest discover -s tests`) is the system acceptance
test. The historical August 2026 sequence for a real pool is in
[`docs/plans/endtoend_runbook.md`](plans/endtoend_runbook.md).

The fixture remains normative where used:
`demo_fixture.demo_task()` supplies the pinned content hash asserted by
`tests/test_models_round3.py` and `tests/test_models_pools.py`, and
`demo_task().attack_cases` remains the normative attack matrix the gates must
reproduce. `docs/runs/demo.md` is the measured record of the last cold demo run
and is kept as a historical snapshot, not as a description of the current CLI.

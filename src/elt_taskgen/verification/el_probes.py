"""Independently count records in rendered EL artifacts.

The counter infers formats from disk, imports none of the loader implementation, and
reads no gold. Unreadable or ambiguous artifacts raise `CensusError`.
"""

from __future__ import annotations

import ast
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from elt_taskgen.models import (
    PopulationName,
    TaskIR,
    canonical_json,
)

#: Where the census lands under tasks/<task_id>/. gates.py duplicates this
#: constant on purpose: the consumer must not import the producer.
EL_CENSUS_EVIDENCE_REL = "reports/el_artifact_census.json"

#: Contract discriminator: a differently-shaped record here is gate-rejected.
CENSUS_KIND = "el-artifact-census"

#: Bump when the COUNTING MECHANICS change, not on refactors. Recorded in the
#: evidence so a reconciliation is attributable to a specific counter.
COUNTER_VERSION = "el-probes/1.1.0"

#: Modules this counter must not import; enforced by independence_violations().
INDEPENDENT_OF: tuple[str, ...] = (
    "elt_taskgen.reference.solution",
    "elt_taskgen.reference.runner",
    "elt_taskgen.reference.gold",
    "elt_taskgen.reference.independent",
    "elt_taskgen.generation.source_data",
    "elt_taskgen.verification.upstream_eval",
    "duckdb",
    "sqlglot",
    "csv",
)


class CensusError(RuntimeError):
    """The census cannot count an artifact honestly, so it counts nothing."""


class ArtifactFormat(str, Enum):
    """The on-disk shape of a rendered artifact, as INFERRED from disk.

    Deliberately not the ``Backend`` enum: that is what the TaskIR CLAIMS, and
    taking the claim as given is the assumption this census refuses to make.
    """

    CSV = "csv"
    JSONL = "jsonl"
    POSTGRES_SQL = "postgres_sql"
    S3_JSONL_PREFIX = "s3_jsonl_prefix"
    REST_PAGES = "rest_pages"


class ArtifactCount(BaseModel):
    """One table's independently counted record total in one population."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: POSIX path relative to tasks/<task_id>/; a count with no artifact is none.
    artifact: str
    format: ArtifactFormat
    records: int


class ArtifactCensus(BaseModel):
    """The full census record, exactly as written to disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_content_hash: str
    kind: str = CENSUS_KIND
    counter_version: str = COUNTER_VERSION
    independent_of: tuple[str, ...] = INDEPENDENT_OF
    #: population -> table -> count.
    populations: dict[str, dict[str, ArtifactCount]]

    def to_canonical_json(self) -> str:
        return canonical_json(self.model_dump(mode="json"))


# CSV — own RFC-4180 state machine (no `csv` module, no line splitting)

def count_csv_records(path: Path) -> int:
    """Data rows in a header-bearing CSV, honouring quoted embedded newlines.

    A record ends at a newline OUTSIDE a quoted field; ``""`` inside one is an
    escaped quote. Blank lines are not records, and the first record is the
    header and is not counted.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CensusError(f"{path}: unreadable CSV: {exc}") from exc

    records = 0
    in_quotes = False
    has_content = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    i += 2  # escaped quote, still inside the field
                    continue
                in_quotes = False
            i += 1
            continue
        if ch == '"':
            in_quotes = True
            has_content = True
            i += 1
            continue
        if ch in "\r\n":
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            if has_content:
                records += 1
            has_content = False
            i += 1
            continue
        has_content = True
        i += 1
    if in_quotes:
        raise CensusError(
            f"{path}: CSV ends inside an unterminated quoted field — the file "
            "is truncated or the quoting is broken; refusing to guess a count"
        )
    if has_content:
        records += 1
    if records == 0:
        raise CensusError(
            f"{path}: CSV has no header record (an empty table still renders "
            "its header row)"
        )
    return records - 1  # the header is not a record


# JSONL — parse first, verify the line delimiter second

_JSON_WHITESPACE = " \t\r\n"


def count_jsonl_records(path: Path) -> int:
    """Records in a line-delimited JSON file, counted by streaming decode.

    Values are decoded with ``raw_decode`` from a running offset and only THEN
    is the separator checked for a newline — the reverse of the trusted
    loader's split-then-parse, which is the point (independence).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CensusError(f"{path}: unreadable jsonl: {exc}") from exc
    decoder = json.JSONDecoder()
    count = 0
    idx = 0
    n = len(text)
    while True:
        start = idx
        while start < n and text[start] in _JSON_WHITESPACE:
            start += 1
        if start >= n:
            break
        if count and "\n" not in text[idx:start]:
            raise CensusError(
                f"{path}: two JSON records are not separated by a newline at "
                f"offset {start} — this file is not line-delimited JSON"
            )
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError as exc:
            raise CensusError(f"{path}: malformed JSON at offset {start}: {exc}") from exc
        if not isinstance(value, dict):
            raise CensusError(
                f"{path}: jsonl record at offset {start} is a "
                f"{type(value).__name__}, not an object"
            )
        count += 1
        idx = end
    return count


# S3 prefix — sorted os.walk over .jsonl parts, unknown objects fail closed

def count_s3_prefix_records(prefix: Path) -> int:
    """Sum of records across every ``.jsonl`` part under an object-store prefix.

    A non-``.jsonl`` file under the prefix aborts the census: it could carry
    records the loader never read, and ignoring it would certify an unverified
    number.
    """
    parts: list[Path] = []
    for root, dirnames, filenames in os.walk(prefix):
        dirnames.sort()
        for name in sorted(filenames):
            candidate = Path(root) / name
            if candidate.name.startswith("."):
                continue
            if candidate.suffix != ".jsonl":
                raise CensusError(
                    f"{candidate}: unrecognized object under S3 prefix "
                    f"{prefix} (expected .jsonl parts only)"
                )
            parts.append(candidate)
    if not parts:
        raise CensusError(f"{prefix}: S3 prefix contains no .jsonl parts")
    return sum(count_jsonl_records(part) for part in sorted(parts))


# REST pages — index.json is NEVER opened

#: Row-list keys a page may use. Local on purpose: the two readers must not
#: agree by construction.
_PAGE_ROW_KEYS: tuple[str, ...] = ("data", "results", "items", "records", "rows")

#: Tolerated (never read) neighbours of the pages; anything else fails closed.
_REST_SIDECARS = frozenset({"index.json"})


def _page_number(name: str) -> int | None:
    if not (name.startswith("page_") and name.endswith(".json")):
        return None
    digits = name[len("page_") : -len(".json")]
    if not digits.isdigit():
        return None
    return int(digits)


def count_rest_page_records(directory: Path) -> int:
    """Sum of RECORDS across ``page_*.json`` in a paginated fixture directory.

    ``index.json`` and page metadata (``total_rows``, ``page_size``) are never
    read: counting metadata instead of records is a bug class this census
    exists to catch, and trusting the index would inherit its errors.
    """
    numbered: dict[int, Path] = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            raise CensusError(
                f"{entry}: unexpected subdirectory in REST fixture dir {directory}"
            )
        number = _page_number(entry.name)
        if number is None:
            if entry.name in _REST_SIDECARS or entry.name.startswith("."):
                continue
            raise CensusError(
                f"{entry}: unrecognized file in REST fixture dir {directory} "
                "(expected page_*.json)"
            )
        if number in numbered:  # pragma: no cover — filenames are unique
            raise CensusError(f"{directory}: duplicate page number {number}")
        numbered[number] = entry

    total = 0
    for number in sorted(numbered):  # numeric order, not lexicographic
        page_path = numbered[number]
        try:
            payload = json.loads(page_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CensusError(f"{page_path}: unreadable REST page: {exc}") from exc
        total += len(_page_rows(payload, page_path))
    return total


def _page_rows(payload: Any, source: Path) -> list[Any]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        present = [k for k in _PAGE_ROW_KEYS if isinstance(payload.get(k), list)]
        if not present:
            raise CensusError(
                f"{source}: REST page object carries none of the row lists "
                f"{_PAGE_ROW_KEYS} — the record count cannot be established"
            )
        if len(present) > 1:
            raise CensusError(
                f"{source}: REST page object carries several row lists "
                f"{present}; which one holds the records is ambiguous"
            )
        rows = payload[present[0]]
    else:
        raise CensusError(f"{source}: REST page is neither a list nor an object")
    for row in rows:
        if not isinstance(row, dict):
            raise CensusError(f"{source}: REST page row is not an object")
    return rows


# Postgres load SQL — own literal-aware scanner; nothing is ever executed

#: Rowless heads; anything else but INSERT/COPY may carry (or delete) rows and
#: aborts the census.
_ROWLESS_STATEMENTS = frozenset(
    {
        "DROP",
        "CREATE",
        "ALTER",
        "BEGIN",
        "START",
        "COMMIT",
        "ROLLBACK",
        "SET",
        "COMMENT",
        "ANALYZE",
        "VACUUM",
    }
)


def count_postgres_sql_records(path: Path) -> int:
    """Rows in the INSERT/COPY payload of a rendered load script — by READING.

    The script is never executed and never handed to a SQL parser library (the
    loader does both). Any statement this hand-written literal-aware scan does
    not fully understand aborts the census.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CensusError(f"{path}: unreadable load SQL: {exc}") from exc

    total = 0
    i = 0
    n = len(text)
    while True:
        i = _skip_gaps(text, i)
        if i >= n:
            break
        end = _statement_end(text, i, path)
        statement = text[i:end]
        i = end + 1 if end < n else n
        head = _leading_word(statement).upper()
        if head == "INSERT":
            total += _count_value_tuples(statement, path)
        elif head == "COPY":
            payload, i = _copy_payload(text, i, statement, path)
            total += payload
        elif head in _ROWLESS_STATEMENTS:
            continue
        elif not head:
            continue
        else:
            raise CensusError(
                f"{path}: unsupported statement {head!r} in load SQL — it may "
                f"carry rows; refusing to count around it "
                f"({statement.strip()[:80]!r})"
            )
    return total


def _skip_gaps(text: str, i: int) -> int:
    """Advance past whitespace, ``--`` comments, ``/* */`` comments and ``;``."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace() or ch == ";":
            i += 1
            continue
        if text.startswith("--", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close < 0:
                return n
            i = close + 2
            continue
        return i
    return i


def _statement_end(text: str, start: int, path: Path) -> int:
    """Index of the ``;`` ending the statement at ``start`` (or EOF).

    Semicolons inside literals, dollar-quoted bodies and comments terminate
    nothing — the whole point of scanning rather than splitting.
    """
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i = _skip_single_quoted(text, i, path)
            continue
        if ch == '"':
            i = _skip_double_quoted(text, i, path)
            continue
        if ch == "$":
            skipped = _skip_dollar_quoted(text, i, path)
            if skipped is not None:
                i = skipped
                continue
        if text.startswith("--", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch == ";":
            return i
        i += 1
    return n


def _skip_single_quoted(text: str, i: int, path: Path) -> int:
    """Index just past the single-quoted literal starting at ``i``."""
    n = len(text)
    i += 1
    while i < n:
        if text[i] == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2  # '' is an escaped quote inside the literal
                continue
            return i + 1
        i += 1
    raise CensusError(f"{path}: unterminated string literal in load SQL")


def _skip_double_quoted(text: str, i: int, path: Path) -> int:
    """Return the index after a double-quoted SQL identifier.

    Honor doubled-quote escapes so keywords and punctuation inside identifiers do not
    alter scanner state.
    """
    n = len(text)
    i += 1
    while i < n:
        if text[i] == '"':
            if i + 1 < n and text[i + 1] == '"':
                i += 2  # "" is an escaped quote inside the identifier
                continue
            return i + 1
        i += 1
    raise CensusError(f"{path}: unterminated quoted identifier in load SQL")


def _skip_dollar_quoted(text: str, i: int, path: Path) -> int | None:
    """Index just past a ``$tag$ ... $tag$`` body, or None if not one."""
    close = text.find("$", i + 1)
    if close < 0:
        return None
    tag = text[i : close + 1]
    if not all(c.isalnum() or c == "_" for c in tag[1:-1]):
        return None
    end = text.find(tag, close + 1)
    if end < 0:
        raise CensusError(f"{path}: unterminated dollar-quoted body in load SQL")
    return end + len(tag)


def _leading_word(statement: str) -> str:
    for token in statement.split():
        if token.startswith("--"):
            break
        return "".join(c for c in token if c.isalpha())
    return ""


def _count_value_tuples(statement: str, path: Path) -> int:
    """Count top-level ``(...)`` groups after the statement's top-level VALUES.

    The column list precedes VALUES, so it is never counted; an
    ``INSERT ... SELECT`` (no VALUES) fails closed — its row count is a
    property of the query, not of the text.
    """
    values_at = _find_top_level_values(statement, path)
    if values_at is None:
        raise CensusError(
            f"{path}: INSERT without a literal VALUES payload — its row count "
            f"cannot be read off the text ({statement.strip()[:80]!r})"
        )
    i = values_at
    n = len(statement)
    depth = 0
    tuples = 0
    while i < n:
        ch = statement[i]
        if ch == "'":
            i = _skip_single_quoted(statement, i, path)
            continue
        if ch == '"' and depth > 0:
            # A quoted identifier is legitimate only INSIDE a value tuple; at
            # depth 0 a `"` must fall through to the fail-closed error below.
            i = _skip_double_quoted(statement, i, path)
            continue
        if ch == "$":
            skipped = _skip_dollar_quoted(statement, i, path)
            if skipped is not None:
                i = skipped
                continue
        if ch == "(":
            if depth == 0:
                tuples += 1
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth < 0:
                raise CensusError(f"{path}: unbalanced ')' in INSERT payload")
            i += 1
            continue
        if depth == 0 and not ch.isspace() and ch != ",":
            raise CensusError(
                f"{path}: unsupported clause after VALUES "
                f"({statement[i : i + 40].strip()!r}) — it may add or suppress "
                "rows, so the payload count is not trustworthy"
            )
        i += 1
    if depth != 0:
        raise CensusError(f"{path}: unbalanced '(' in INSERT payload")
    if tuples == 0:
        raise CensusError(f"{path}: VALUES payload contains no row tuples")
    return tuples


def _find_top_level_values(statement: str, path: Path) -> int | None:
    """Offset just past a ``VALUES`` keyword at paren depth 0, else None.

    Quoted identifiers and bodies are skipped WHOLE, so the ``values`` in
    ``INSERT INTO "values" (...)`` is a table name, not the keyword.
    """
    i = 0
    n = len(statement)
    depth = 0
    while i < n:
        ch = statement[i]
        if ch == "'":
            i = _skip_single_quoted(statement, i, path)
            continue
        if ch == '"':
            i = _skip_double_quoted(statement, i, path)
            continue
        if ch == "$":
            skipped = _skip_dollar_quoted(statement, i, path)
            if skipped is not None:
                i = skipped
                continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        if (
            depth == 0
            and (ch in "vV")
            and statement[i : i + 6].upper() == "VALUES"
            and (i == 0 or not (statement[i - 1].isalnum() or statement[i - 1] == "_"))
            and (
                i + 6 >= n
                or not (statement[i + 6].isalnum() or statement[i + 6] == "_")
            )
        ):
            return i + 6
        i += 1
    return None


def _copy_payload(text: str, i: int, statement: str, path: Path) -> tuple[int, int]:
    """(rows, next offset) for a ``COPY ... FROM STDIN`` payload block.

    The payload is not SQL: raw lines after the terminating ``;`` up to a line
    of only ``\\.``. A COPY reading from a file instead of stdin fails closed —
    its rows are not in this artifact.
    """
    normalized = " ".join(statement.split()).upper()
    if "FROM STDIN" not in normalized:
        raise CensusError(
            f"{path}: COPY does not read FROM STDIN, so its rows are not in "
            f"this artifact ({statement.strip()[:80]!r})"
        )
    rows = 0
    n = len(text)
    if i < n and text[i] == "\n":
        i += 1
    while i < n:
        nl = text.find("\n", i)
        line = text[i:nl] if nl >= 0 else text[i:]
        i = n if nl < 0 else nl + 1
        if line.strip() == "\\.":
            return rows, i
        if line.strip() == "":
            continue
        rows += 1
    raise CensusError(
        f"{path}: COPY FROM STDIN payload has no '\\.' terminator — the file "
        "is truncated"
    )


# Artifact discovery (from disk, never from the TaskIR backend assignment)

_FILE_FORMATS: dict[str, ArtifactFormat] = {
    ".csv": ArtifactFormat.CSV,
    ".jsonl": ArtifactFormat.JSONL,
    ".sql": ArtifactFormat.POSTGRES_SQL,
}

_COUNTERS = {
    ArtifactFormat.CSV: count_csv_records,
    ArtifactFormat.JSONL: count_jsonl_records,
    ArtifactFormat.POSTGRES_SQL: count_postgres_sql_records,
    ArtifactFormat.S3_JSONL_PREFIX: count_s3_prefix_records,
    ArtifactFormat.REST_PAGES: count_rest_page_records,
}


def _classify_directory(directory: Path, table: str) -> tuple[Path, ArtifactFormat]:
    """Classify a per-table directory by its CONTENTS, failing closed on mixes."""
    entries = sorted(p for p in directory.iterdir() if not p.name.startswith("."))
    has_pages = any(_page_number(p.name) is not None for p in entries if p.is_file())
    has_parts = any(p.suffix == ".jsonl" for p in entries if p.is_file())
    if has_pages and has_parts:
        raise CensusError(
            f"{directory}: holds BOTH REST pages and jsonl parts; which one "
            f"carries the records for table {table!r} is ambiguous"
        )
    if has_pages:
        return directory, ArtifactFormat.REST_PAGES
    if has_parts:
        return directory, ArtifactFormat.S3_JSONL_PREFIX
    # Nested single-file layouts (<table>/<table>.csv, <table>/load.sql, ...).
    files = [p for p in entries if p.is_file() and p.suffix in _FILE_FORMATS]
    if len(files) == 1:
        return files[0], _FILE_FORMATS[files[0].suffix]
    if not files and all(p.name in _REST_SIDECARS for p in entries):
        # A REST table with zero rows renders index.json and no pages.
        return directory, ArtifactFormat.REST_PAGES
    raise CensusError(
        f"{directory}: cannot determine the record-bearing artifact for table "
        f"{table!r} from its contents ({[p.name for p in entries]})"
    )


def resolve_artifact(rendered_dir: Path, table: str) -> tuple[Path, ArtifactFormat]:
    """Find the ONE rendered artifact for ``table`` under ``rendered/``.

    Scans every backend directory rather than the one the TaskIR assigned, so a
    table rendered where the loader does not look is a hard error here instead
    of an invisible zero there. Zero and several candidates both fail closed.
    """
    if not rendered_dir.is_dir():
        raise CensusError(f"{rendered_dir}: rendered dir does not exist")
    found: list[tuple[Path, ArtifactFormat]] = []
    for backend_dir in sorted(p for p in rendered_dir.iterdir() if p.is_dir()):
        for entry in sorted(backend_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name == table:
                    found.append(_classify_directory(entry, table))
            elif entry.stem == table and entry.suffix in _FILE_FORMATS:
                found.append((entry, _FILE_FORMATS[entry.suffix]))
    if not found:
        raise CensusError(
            f"{rendered_dir}: no rendered artifact for table {table!r} — an "
            "uncounted artifact is an uncertified one"
        )
    if len({(p.resolve(), f) for p, f in found}) > 1:
        raise CensusError(
            f"{rendered_dir}: table {table!r} resolves to several artifacts "
            f"{[str(p) for p, _ in found]}; a census must not choose"
        )
    return found[0]


def count_artifact(path: Path, fmt: ArtifactFormat) -> int:
    """Record count for one artifact in the format inferred from disk."""
    counter = _COUNTERS.get(fmt)
    if counter is None:  # pragma: no cover — ArtifactFormat is closed
        raise CensusError(f"{path}: no counter for format {fmt!r}")
    return counter(path)


# The census

def census_population(
    task: TaskIR, task_root: Path, population: PopulationName
) -> dict[str, ArtifactCount]:
    """Count every task table's rendered artifact in one population."""
    rendered = task_root / "populations" / population.value / "rendered"
    counts: dict[str, ArtifactCount] = {}
    for table in task.tables:
        artifact, fmt = resolve_artifact(rendered, table.name)
        counts[table.name] = ArtifactCount(
            artifact=artifact.relative_to(task_root).as_posix(),
            format=fmt,
            records=count_artifact(artifact, fmt),
        )
    return counts


def build_artifact_census(workspace: Path, task: TaskIR) -> ArtifactCensus:
    """Census every population's rendered artifacts for THIS task identity.

    Bound to ``task.content_hash()`` so a later repair makes the evidence stale
    rather than silently wrong. Every declared population must be present on
    disk: a missing one certifies less than the record claims.
    """
    task_root = Path(workspace) / "tasks" / task.task_id
    if not task_root.is_dir():
        raise CensusError(f"{task_root}: no task directory to census")
    declared = [p.name for p in task.populations] or list(PopulationName)
    populations: dict[str, dict[str, ArtifactCount]] = {}
    for population in declared:
        populations[population.value] = census_population(task, task_root, population)
    return ArtifactCensus(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        populations=populations,
    )


def record_artifact_census(workspace: Path, task: TaskIR, gold: Any = None) -> Path:
    """Write ``reports/el_artifact_census.json``; return its path.

    ``gold`` is accepted because the pipeline hands it over and DELIBERATELY
    not read: a counter that peeked at the answer could never disagree with it.
    Reconciling against gold and generator rows belongs to the gate.
    """
    del gold  # see docstring — read nothing from the answer
    census = build_artifact_census(workspace, task)
    path = Path(workspace) / "tasks" / task.task_id / EL_CENSUS_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(census.to_canonical_json(), encoding="utf-8")
    return path


def load_artifact_census(workspace: Path, task_id: str) -> dict | None:
    """Raw recorded census, or None. Binding is the consumer's job (gates)."""
    path = Path(workspace) / "tasks" / task_id / EL_CENSUS_EVIDENCE_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# The independence claim, re-derived from this file's own source

def independence_violations(source_path: Path | None = None) -> list[str]:
    """Modules from ``INDEPENDENT_OF`` this file actually imports (should be []).

    An unchecked claim rots, so it is re-derived from the AST — including
    imports inside functions, which a module-level check would miss.
    """
    path = Path(source_path) if source_path is not None else Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    violations = []
    for forbidden in INDEPENDENT_OF:
        for name in imported:
            if name == forbidden or name.startswith(forbidden + "."):
                violations.append(f"imports {name!r} (declared independent of "
                                  f"{forbidden!r})")
    return sorted(set(violations))

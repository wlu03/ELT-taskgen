"""Score a shipped release unit with ``verification.upstream_eval``.

This module adds no comparison rules and never executes answer-key SQL.
Legacy releases, invalid checksums, and missing gold cannot pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.export import release as release_mod
from elt_taskgen.export.eltbench import DOCUMENTATION_FILENAME
from elt_taskgen.models import PopulationName, Row, TaskVariant
from elt_taskgen.sql_identifiers import quote_sql_identifier, quote_sql_path
from elt_taskgen.verification import upstream_eval
from elt_taskgen.verification.upstream_eval import RewardResult

#: Default schema a dbt-duckdb build materializes marts into (sources use `main`).
MART_SCHEMA_TEMPLATE = "main_{db}_dev"

#: Suffix -> variant, for the two shipped unit kinds.
_UNIT_SUFFIXES: dict[str, TaskVariant] = {
    "__el": TaskVariant.EXTRACT_LOAD,
    "__t": TaskVariant.TRANSFORM,
}


class MartView(BaseModel):
    """The mart facts the reward needs, read from the SHIPPED release.

    Not a `models.MartSpec`: a release deliberately withholds the private
    TaskIR, which carries the plan (i.e. the answer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    grain: str = ""
    key_columns: tuple[str, ...] = Field(min_length=1)
    columns: tuple[str, ...] = ()


class _MartCarrier(BaseModel):
    """Duck-typed stand-in for TaskIR where the reward only reads `.marts`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    marts: tuple[MartView, ...] = ()


class ReleaseUnit(BaseModel):
    """One graded unit of a frozen release, resolved to concrete paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_dir: Path
    unit_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    variant: TaskVariant
    #: Database/schema the bundle was emitted under; AUTHORITATIVE, not the task id.
    db: str = Field(min_length=1)
    public_dir: Path
    answer_key_dir: Path
    #: private/<unit_id>/reward.json, parsed.
    reward_manifest: dict[str, Any] = Field(default_factory=dict)
    #: table -> frozen PRIMARY-population row count (answer_key/table.json).
    expected_tables: dict[str, int] = Field(default_factory=dict)
    marts: tuple[MartView, ...] = ()
    #: population -> release-relative rendered EL source root (EL units only).
    sources: dict[str, str] = Field(default_factory=dict)

    @property
    def sort_keys(self) -> dict[str, tuple[str, ...]]:
        """mart -> total-order key columns, as sort_key.json records them."""
        return {mart.name: mart.key_columns for mart in self.marts}

    def source_root(self, population: str) -> Path:
        """Absolute path of one population's rendered EL source root."""
        rel = self.sources.get(population)
        if rel is None:
            raise KeyError(
                f"unit {self.unit_id!r} names no source root for population "
                f"{population!r} (has: {sorted(self.sources)})"
            )
        return self.release_dir / rel


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"release is missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _single_key(mapping: Mapping[str, Any], what: str, path: Path) -> str:
    keys = sorted(mapping)
    if len(keys) != 1:
        raise ValueError(f"{what} at {path} names {len(keys)} databases: {keys}")
    return keys[0]


def _marts_from_bundle(public_dir: Path, answer_key_dir: Path, db: str) -> tuple[MartView, ...]:
    """MartViews from the public data_model.yaml + private sort_key.json."""
    model_path = public_dir / "data_model.yaml"
    if not model_path.is_file():
        return ()
    data = yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}
    sort_keys = _read_json(answer_key_dir / "sort_key.json")
    if not isinstance(sort_keys, dict):
        raise ValueError(f"malformed sort_key.json under {answer_key_dir}")
    by_mart = dict(sort_keys.get(db) or {})
    marts: list[MartView] = []
    for model in data.get("models") or []:
        name = str(model.get("name"))
        keys = tuple(str(k) for k in (by_mart.get(name) or model.get("key_columns") or ()))
        if not keys:
            raise ValueError(
                f"mart {name!r}: no total-order key columns in sort_key.json or "
                "data_model.yaml — the compare would have no defined order"
            )
        marts.append(
            MartView(
                name=name,
                grain=str(model.get("grain") or ""),
                key_columns=keys,
                columns=tuple(str(c.get("name")) for c in model.get("columns") or ()),
            )
        )
    return tuple(marts)


def load_release_unit(
    release_dir: Path, unit_id: str, *, verify: bool = True
) -> ReleaseUnit:
    """Load one EL or T unit from a verified schema-2 split release.

    Reject combined releases, schema-1 releases, and unknown unit IDs.
    ``verify=False`` skips manifest verification only when explicitly set.
    """
    release_dir = Path(release_dir).resolve()
    manifest_path = release_dir / "release_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no release manifest at {manifest_path}")
    manifest = release_mod.ReleaseManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if (
        manifest.corpus_profile == release_mod.COMBINED_CORPUS_PROFILE
        or getattr(manifest, "public_layout", "")
        == release_mod.COMBINED_PUBLIC_LAYOUT
    ):
        raise ValueError(
            f"release {manifest.release_id} uses the combined ELT-Bench runtime "
            "contract: the legacy DuckDB scorer cannot score it. Use the "
            "destination runtime scorer for the end-to-end Airbyte + dbt task."
        )
    if manifest.schema_version == "1.0" or manifest.corpus_profile == "legacy_full":
        raise ValueError(
            f"{release_dir} is a legacy '{manifest.corpus_profile}' release "
            f"(schema {manifest.schema_version}): it ships no EL/T units to "
            "score. Re-cut it with the current release profile."
        )
    suffix = next((s for s in _UNIT_SUFFIXES if unit_id.endswith(s)), None)
    if suffix is None:
        raise ValueError(
            f"unit id {unit_id!r} names neither an __el nor a __t unit"
        )
    variant = _UNIT_SUFFIXES[suffix]
    parent_id = unit_id[: -len(suffix)]
    if parent_id not in manifest.tasks:
        raise ValueError(
            f"release {manifest.release_id} holds no task {parent_id!r} "
            f"(has: {sorted(manifest.tasks)})"
        )
    if verify:
        result = release_mod.verify_release(release_dir)
        if not result.ok:
            raise ValueError(
                f"release {manifest.release_id} does not verify against its own "
                f"manifest; refusing to score it: {list(result.failures)[:3]}"
            )

    public_dir = release_dir / "public" / unit_id
    answer_key_dir = release_dir / "private" / parent_id / "answer_key"
    if not public_dir.is_dir():
        raise FileNotFoundError(f"no public bundle at {public_dir}")
    reward = _read_json(release_dir / "private" / unit_id / "reward.json")
    if not isinstance(reward, dict):
        raise ValueError(f"malformed reward manifest for unit {unit_id!r}")
    table_json = _read_json(answer_key_dir / "table.json")
    db = _single_key(table_json, "table.json", answer_key_dir / "table.json")
    expected = {str(t): int(c) for t, c in (table_json[db] or {}).items()}
    sources = {
        str(pop): f"private/{parent_id}/{rel}"
        for pop, rel in (reward.get("sources") or {}).items()
    }
    # The manifest is authoritative here: a legacy reward.json has no `sources`.
    for pop, rel in (manifest.el_sources.get(parent_id) or {}).items():
        sources[str(pop)] = str(rel)
    return ReleaseUnit(
        release_dir=release_dir,
        unit_id=unit_id,
        parent_id=parent_id,
        variant=variant,
        db=db,
        public_dir=public_dir,
        answer_key_dir=answer_key_dir,
        reward_manifest=reward,
        expected_tables=expected,
        marts=_marts_from_bundle(public_dir, answer_key_dir, db),
        sources=dict(sorted(sources.items())),
    )


def collect_stage1_counts(
    con: Any, expected: Iterable[str], source_schema: str = "main"
) -> dict[str, int]:
    """table -> row count in the solver's warehouse, for the expected tables.

    A missing table is ABSENT from the result rather than 0, so `compare_stage1`
    reports "table not found" instead of a count mismatch — different failures.
    """
    counts: dict[str, int] = {}
    for table in sorted(set(str(t) for t in expected)):
        try:
            row = con.execute(
                f'SELECT count(*) FROM "{_ident(source_schema)}"."{_ident(table)}"'
            ).fetchone()
        except Exception:  # noqa: BLE001 - a missing/unreadable relation is "not loaded"
            continue
        if row is not None:
            counts[table] = int(row[0])
    return counts


def collect_mart_rows(
    con: Any, marts: Sequence[MartView], mart_schema: str
) -> dict[str, list[Row]]:
    """mart -> rows the solver materialized, keyed by column name.

    `SELECT *` only: the answer key's SQL is never run and `compare_mart` sorts
    both sides into the mart's total order itself. A mart the solver did not
    build is absent from the result, so it scores False, not a crash.
    """
    from elt_taskgen.reference.solution import to_scalar

    out: dict[str, list[Row]] = {}
    for mart in marts:
        try:
            relation = quote_sql_path(
                (mart_schema, mart.name), dialect="duckdb", force=True
            )
            cur = con.execute(f"SELECT * FROM {relation}")
        except Exception:  # noqa: BLE001 - an unbuilt mart is a zero, not a crash
            continue
        columns = [d[0] for d in cur.description]
        out[mart.name] = [
            {name: to_scalar(value) for name, value in zip(columns, raw, strict=True)}
            for raw in cur.fetchall()
        ]
    return out


def _ident(name: str) -> str:
    """Quote-escape one SQL identifier (the caller wraps it in double quotes)."""
    quoted = quote_sql_identifier(str(name), dialect="duckdb", force=True)
    return quoted[1:-1]


def _gold_bundle(unit: ReleaseUnit) -> Any:
    """The frozen gold for this unit's parent, hash-verified on load."""
    from elt_taskgen.reference.gold import load_gold

    return load_gold(unit.answer_key_dir)


def score_release_unit(
    unit: ReleaseUnit,
    con: Any,
    *,
    population: str = "primary",
    source_schema: str = "main",
    mart_schema: str | None = None,
) -> RewardResult:
    """Score one submission from a DuckDB connection.

    ``population`` defaults to ``primary``. Both variants use
    ``upstream_eval.evaluate_variant``. Unknown populations and populations
    without frozen gold cannot pass.
    """
    try:
        pop = PopulationName(population)
    except ValueError as exc:
        raise ValueError(
            f"unknown population {population!r}: expected one of "
            f"{[p.value for p in PopulationName]}"
        ) from exc
    gold = _gold_bundle(unit)
    if unit.variant is TaskVariant.EXTRACT_LOAD:
        expected = set(unit.expected_tables) | set(
            (getattr(gold, "stage1", None) or {}).get(pop.value) or {}
        )
        actual = collect_stage1_counts(con, expected, source_schema)
        return upstream_eval.evaluate_variant(
            TaskVariant.EXTRACT_LOAD,
            _MartCarrier(marts=unit.marts),
            gold,
            pop,
            actual_stage1=actual,
        )
    schema = mart_schema or MART_SCHEMA_TEMPLATE.format(db=unit.db)
    rows = collect_mart_rows(con, unit.marts, schema)
    return upstream_eval.evaluate_variant(
        TaskVariant.TRANSFORM,
        _MartCarrier(marts=unit.marts),
        gold,
        pop,
        actual_marts=rows,
    )


def solver_prompt(unit: ReleaseUnit) -> str:
    """The solver-facing objective, VERBATIM from the shipped bundle.

    `public/<unit_id>/documentation.md` is what the bundle was certified with;
    re-rendering a prompt here would create a second, drifting statement of
    the task.
    """
    path = unit.public_dir / DOCUMENTATION_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"unit {unit.unit_id!r} ships no {DOCUMENTATION_FILENAME}"
        )
    return path.read_text(encoding="utf-8")

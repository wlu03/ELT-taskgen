"""Validate a frozen release against runtime logical-type semantics.

The check uses the strict loader and canonical relation fingerprinter over private
TaskIR, rendered sources, and gold. Non-portable releases fail before real warehouse
provisioning.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import duckdb

from elt_taskgen.models import TaskIR
from elt_taskgen.reference.gold import load_gold
from elt_taskgen.sql_identifiers import quote_sql_identifier
from elt_taskgen.verification import strict_diagnostic, upstream_eval
from elt_taskgen.verification.canonical_fingerprint import (
    CanonicalFingerprintError,
    NaiveTimestampPolicy,
    canonical_relation_fingerprint,
)


class ReleasePortabilityError(ValueError):
    """Private release bytes cannot be scored under the canonical contract."""


def _quoted(name: str) -> str:
    return quote_sql_identifier(name, dialect="duckdb", force=True)


def validate_release_portability(
    task: TaskIR,
    answer_key_dir: Path,
    source_roots: Mapping[str, Path],
) -> None:
    """Validate one task's released sources and gold under runtime semantics.

    This is intentionally an exception-only API: callers either finish with a
    package known to be usable as canonical reference evidence, or receive a
    precise refusal.  It writes nothing and opens one in-memory DuckDB
    connection per population.
    """

    answer_key_dir = Path(answer_key_dir)
    try:
        gold = load_gold(answer_key_dir)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ReleasePortabilityError(
            f"private gold is missing or invalid: {exc}"
        ) from exc

    if gold.task_id != task.task_id:
        raise ReleasePortabilityError(
            f"private gold task_id {gold.task_id!r} does not match "
            f"TaskIR {task.task_id!r}"
        )
    if gold.task_content_hash != task.content_hash():
        raise ReleasePortabilityError(
            "private gold and semantic TaskIR do not share one content hash"
        )

    stage1_populations = set(gold.stage1)
    stage2_populations = set(gold.stage2_csv)
    source_populations = set(source_roots)
    if stage1_populations != stage2_populations:
        raise ReleasePortabilityError(
            "stage-1 and stage-2 gold populations differ "
            f"(stage-1 only: {sorted(stage1_populations - stage2_populations)}, "
            f"stage-2 only: {sorted(stage2_populations - stage1_populations)})"
        )
    if source_populations != stage1_populations:
        raise ReleasePortabilityError(
            "released source populations do not exactly match frozen gold "
            f"(missing: {sorted(stage1_populations - source_populations)}, "
            f"extra: {sorted(source_populations - stage1_populations)})"
        )

    expected_tables = {table.name for table in task.tables}
    expected_marts = {mart.name for mart in task.marts}
    for population in sorted(stage1_populations):
        recorded_counts = dict(gold.stage1[population])
        if set(recorded_counts) != expected_tables:
            raise ReleasePortabilityError(
                f"population {population!r} stage-1 gold does not name exactly "
                f"the TaskIR tables (missing: "
                f"{sorted(expected_tables - set(recorded_counts))}, extra: "
                f"{sorted(set(recorded_counts) - expected_tables)})"
            )
        mart_gold = dict(gold.stage2_csv[population])
        if set(mart_gold) != expected_marts:
            raise ReleasePortabilityError(
                f"population {population!r} stage-2 gold does not name exactly "
                f"the TaskIR marts (missing: "
                f"{sorted(expected_marts - set(mart_gold))}, extra: "
                f"{sorted(set(mart_gold) - expected_marts)})"
            )

        connection = duckdb.connect(":memory:")
        try:
            try:
                actual_counts = strict_diagnostic.load_sources_duckdb_strict(
                    task, Path(source_roots[population]), connection
                )
            except Exception as exc:  # noqa: BLE001 - a bad artifact is a refusal
                raise ReleasePortabilityError(
                    f"population {population!r} strict source load failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if actual_counts != recorded_counts:
                raise ReleasePortabilityError(
                    f"population {population!r} strict source counts disagree "
                    f"with frozen stage-1 gold (expected {recorded_counts}, "
                    f"loaded {actual_counts})"
                )

            # A successful DuckDB cast alone is not enough: values such as a
            # non-finite FLOAT may load but are deliberately outside the
            # destination-neutral fingerprint contract.
            for table in task.tables:
                projected = ", ".join(_quoted(column.name) for column in table.columns)
                try:
                    cursor = connection.execute(
                        f"SELECT {projected} FROM {_quoted(table.name)}"
                    )
                    columns = tuple(str(item[0]) for item in cursor.description)
                    rows = cursor.fetchall()
                    canonical_relation_fingerprint(
                        actual_columns=columns,
                        expected_columns=tuple(
                            (column.name, column.type) for column in table.columns
                        ),
                        rows=rows,
                        naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
                    )
                except Exception as exc:  # noqa: BLE001 - normalize as refusal
                    raise ReleasePortabilityError(
                        f"population {population!r} source table "
                        f"{table.name!r} is not canonically representable: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
        finally:
            connection.close()

        for mart in task.marts:
            try:
                columns, rows = upstream_eval.parse_canonical_csv(
                    mart_gold[mart.name]
                )
                if not columns:
                    raise CanonicalFingerprintError("gold relation has no columns")
                canonical_relation_fingerprint(
                    actual_columns=columns,
                    expected_columns=tuple(
                        (column.name, column.type) for column in mart.columns
                    ),
                    rows=rows,
                    naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
                )
            except Exception as exc:  # noqa: BLE001 - normalize as refusal
                raise ReleasePortabilityError(
                    f"population {population!r} mart {mart.name!r} gold is not "
                    f"canonically representable: {type(exc).__name__}: {exc}"
                ) from exc


__all__ = ["ReleasePortabilityError", "validate_release_portability"]

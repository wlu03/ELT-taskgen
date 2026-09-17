#!/usr/bin/env python3
"""Install generated task bundles into an original ELT-Bench checkout shape."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from elt_taskgen.destinations import Destination
from elt_taskgen.export.upstream_layout import (
    materialize_upstream_layout,
    materialize_upstream_repository,
    project_all_destinations,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Flatten taskgen public tasks into original ELT-Bench inputs and "
            "merge private table/sort/SQL/ground-truth evaluator artifacts."
        )
    )
    parser.add_argument("cohort", type=Path, help="Generated cohort or schema-3 release")
    parser.add_argument(
        "--upstream-root",
        type=Path,
        help=(
            "ELT-Bench checkout/output root. The tool chooses inputs, "
            "inputs_databricks, or inputs_redshift and writes evaluation/."
        ),
    )
    parser.add_argument(
        "--inputs-root",
        type=Path,
        help="Explicit destination-specific inputs directory",
    )
    parser.add_argument(
        "--evaluation-root",
        type=Path,
        help="Explicit original ELT-Bench evaluation directory",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TASK",
        help=(
            "Select an alias, canonical task id, or runtime namespace. "
            "Repeat for multiple tasks; omit to materialize the full cohort."
        ),
    )
    parser.add_argument(
        "--destination",
        choices=tuple(item.value for item in Destination),
        help="Fail unless the selected public tasks target this destination",
    )
    parser.add_argument(
        "--all-destinations",
        action="store_true",
        help=(
            "Re-export shared private TaskIR/gold for Snowflake, Databricks, "
            "and Redshift, then materialize all three original input roots."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    explicit = args.inputs_root is not None or args.evaluation_root is not None
    if args.all_destinations:
        if args.upstream_root is None:
            parser.error("--all-destinations requires --upstream-root")
        if explicit or args.destination is not None:
            parser.error(
                "--all-destinations cannot be combined with --inputs-root, "
                "--evaluation-root, or --destination"
            )
        report = project_all_destinations(
            args.cohort,
            args.upstream_root,
            only=args.only,
        )
    elif args.upstream_root is not None:
        if explicit:
            parser.error(
                "use either --upstream-root or both --inputs-root and "
                "--evaluation-root"
            )
        report = materialize_upstream_repository(
            args.cohort,
            args.upstream_root,
            only=args.only,
            destination=args.destination,
        )
    else:
        if args.inputs_root is None or args.evaluation_root is None:
            parser.error(
                "provide --upstream-root, or provide both --inputs-root and "
                "--evaluation-root"
            )
        report = materialize_upstream_layout(
            args.cohort,
            args.inputs_root,
            args.evaluation_root,
            only=args.only,
            destination=args.destination,
        )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


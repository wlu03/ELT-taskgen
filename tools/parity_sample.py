"""Draw the parity sample, emit the replay argv, and publish the parity report.

Three offline steps around the one thing this repository cannot do for itself —
running an artifact against a real Snowflake, Databricks or Redshift:

    elt-taskgen-parity-sample sample  --candidates c.json --out sample.json
    elt-taskgen-parity-sample argv    --sample sample.json --release <dir> \
        --destination-credential <path>
    elt-taskgen-parity-sample report  --verdicts v.json --reports-dir reports/

``python tools/parity_sample.py`` remains available in a source checkout.

`sample` draws a deterministic stratified sample (Output 12 §8 Table 9: at least
20 locally accepted and 10 locally rejected per arm, at least 4 per source
pool).  `argv` prints the exact `elt-taskgen runtime verify-*` command line for
each specimen so the OWNER can run it against a real destination with an
operator credential that never enters this process.  `report` turns the
resulting verdicts into `reports/parity_<arm>.json`.

This script performs no network access, starts no container, opens no warehouse
connection and reads no credential file.  It reads and writes JSON.

Exit codes:
  0  the step completed
  2  REFUSED: the input could not be trusted, or `--require-floors` was asked
     for and the sample does not reach them
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from elt_taskgen.destinations import Destination
from elt_taskgen.models import Origin, PopulationName, derive_seed, readable_json
from elt_taskgen.runtime.attestation import SandboxAttestation
from elt_taskgen.runtime.parity import (
    MIN_ARTIFACTS_PER_SOURCE_POOL,
    MIN_LOCALLY_ACCEPTED_PER_ARM,
    MIN_LOCALLY_REJECTED_PER_ARM,
    ArtifactVerdict,
    ParityBatteryError,
    ReplaySpecimen,
    ReplayStage,
    parity_rate,
    parity_report_path,
    runtime_verify_argv,
    stratification_shortfalls,
    write_parity_report,
)

_MAX_INPUT_BYTES = 16 * 1024 * 1024


class Refused(RuntimeError):
    """The step refused; nothing was written."""


def _read_json_list(path: Path, *, label: str) -> list[dict]:
    resolved = Path(path)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise Refused(f"{label} is unreadable: {resolved}") from exc
    if size > _MAX_INPUT_BYTES:
        raise Refused(f"{label} exceeds the {_MAX_INPUT_BYTES}-byte bound")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{label} is not readable JSON: {exc}") from exc
    if not isinstance(document, list) or not all(
        isinstance(item, dict) for item in document
    ):
        raise Refused(f"{label} must be a JSON list of objects")
    return document


def _specimen(record: dict) -> ReplaySpecimen:
    missing = {
        "artifact_id",
        "arm",
        "destination",
        "source_pool",
        "population",
        "stage",
        "local_accepted",
    } - set(record)
    if missing:
        raise Refused(f"candidate record is missing {sorted(missing)}")
    try:
        stage = ReplayStage(str(record["stage"]))
    except ValueError as exc:
        raise Refused(f"unknown replay stage: {record['stage']!r}") from exc
    if not isinstance(record["local_accepted"], bool):
        raise Refused("local_accepted must be a JSON boolean")
    # The closed vocabularies, checked here so a typo in a candidate manifest
    # cannot quietly become its own one-artifact source-pool stratum.
    for field, values in (
        ("destination", {item.value for item in Destination}),
        ("source_pool", {item.value for item in Origin}),
        ("population", {item.value for item in PopulationName}),
    ):
        if str(record[field]) not in values:
            raise Refused(f"unknown {field}: {record[field]!r}")
    # The arm ends up naming `reports/parity_<arm>.json`, so an arm that could
    # never be published is refused before any warehouse time is spent on it.
    try:
        parity_report_path(".", str(record["arm"]))
    except ParityBatteryError as exc:
        raise Refused(str(exc)) from exc
    return ReplaySpecimen(
        artifact_id=str(record["artifact_id"]),
        arm=str(record["arm"]),
        destination=str(record["destination"]),
        source_pool=str(record["source_pool"]),
        population=str(record["population"]),
        stage=stage,
        local_accepted=bool(record["local_accepted"]),
    )


def _specimen_record(specimen: ReplaySpecimen) -> dict:
    return {
        "artifact_id": specimen.artifact_id,
        "arm": specimen.arm,
        "destination": specimen.destination,
        "source_pool": specimen.source_pool,
        "population": specimen.population,
        "stage": specimen.stage.value,
        "local_accepted": specimen.local_accepted,
    }


def stratified_sample(
    candidates: list[ReplaySpecimen],
    *,
    arm: str,
    seed_label: str = "parity-sample-v1",
    min_accepted: int = MIN_LOCALLY_ACCEPTED_PER_ARM,
    min_rejected: int = MIN_LOCALLY_REJECTED_PER_ARM,
    per_pool: int = MIN_ARTIFACTS_PER_SOURCE_POOL,
) -> tuple[ReplaySpecimen, ...]:
    """Deterministic stratified draw for ONE arm.

    Source-pool coverage is filled first (a battery that measured one pool and
    generalised to five would be the exact overclaim this sampling rule exists
    to prevent), then the accepted floor, then the rejected floor.  The draw is
    seeded from the arm and the candidate roster, so re-running it on the same
    candidates reproduces the same sample.
    """
    rows = sorted(
        (item for item in candidates if item.arm == arm),
        key=lambda item: item.artifact_id,
    )
    if not rows:
        raise Refused(f"no candidate artifacts for arm {arm!r}")
    rng = random.Random(
        derive_seed(seed_label, arm, *(item.artifact_id for item in rows))
    )
    pools = sorted({item.source_pool for item in rows})
    remaining: dict[str, list[ReplaySpecimen]] = {}
    for pool in pools:
        bucket = [item for item in rows if item.source_pool == pool]
        rng.shuffle(bucket)
        # Accepted artifacts carry the claim, so they lead every bucket.
        bucket.sort(key=lambda item: not item.local_accepted)
        remaining[pool] = bucket

    chosen: dict[str, ReplaySpecimen] = {}

    def take(item: ReplaySpecimen) -> None:
        chosen[item.artifact_id] = item
        remaining[item.source_pool].remove(item)

    for pool in pools:
        for item in list(remaining[pool])[:per_pool]:
            take(item)

    def top_up(want_accepted: bool, target: int) -> None:
        have = sum(
            1 for item in chosen.values() if item.local_accepted is want_accepted
        )
        pool_cycle = list(pools)
        while have < target:
            progressed = False
            for pool in pool_cycle:
                available = [
                    item
                    for item in remaining[pool]
                    if item.local_accepted is want_accepted
                ]
                if not available:
                    continue
                take(available[0])
                have += 1
                progressed = True
                if have >= target:
                    break
            if not progressed:
                break

    top_up(True, min_accepted)
    top_up(False, min_rejected)
    return tuple(sorted(chosen.values(), key=lambda item: item.artifact_id))


def cmd_sample(args: argparse.Namespace) -> int:
    candidates = [_specimen(record) for record in _read_json_list(
        args.candidates, label="candidate manifest"
    )]
    arms = sorted({item.arm for item in candidates})
    if args.arm is not None:
        if args.arm not in arms:
            raise Refused(f"arm {args.arm!r} is not in the candidate manifest")
        arms = [args.arm]
    drawn: list[ReplaySpecimen] = []
    for arm in arms:
        drawn.extend(stratified_sample(candidates, arm=arm))
    shortfalls = stratification_shortfalls(drawn)
    payload = [_specimen_record(item) for item in drawn]
    if args.out is not None:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(readable_json(payload) + "\n", encoding="utf-8")
    else:
        print(readable_json(payload))
    for code in shortfalls:
        print(f"shortfall: {code}", file=sys.stderr)
    if shortfalls and args.require_floors:
        raise Refused(
            f"the drawn sample does not reach the Table 9 floors: {list(shortfalls)}"
        )
    return 0


def cmd_argv(args: argparse.Namespace) -> int:
    specimens = [_specimen(record) for record in _read_json_list(
        args.sample, label="sample manifest"
    )]
    credential = Path(args.destination_credential)
    for specimen in specimens:
        argv = runtime_verify_argv(
            release=args.release,
            task_id=args.task_id or specimen.artifact_id,
            stage=specimen.stage,
            destination_credential=credential,
            population=specimen.population,
            destination=specimen.destination,
            certification_strict=not args.no_certification_strict,
        )
        print(json.dumps({"artifact_id": specimen.artifact_id, "argv": list(argv)}))
    return 0


def _attestation(path: Path | None) -> "SandboxAttestation | None":
    """Load the sealed sandbox attestation the real arm ran under, if given.

    The record is a public, secret-free document by construction
    (`runtime/attestation.py`), and `parity_rate` verifies its seal before it
    names it, so a report can never attribute itself to isolation nobody can
    check.
    """
    if path is None:
        return None
    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        return SandboxAttestation.model_validate(payload)
    except (OSError, ValueError) as exc:
        raise Refused(f"sandbox attestation at {resolved} is unreadable: {exc}") from exc


def cmd_report(args: argparse.Namespace) -> int:
    records = _read_json_list(args.verdicts, label="verdict manifest")
    try:
        verdicts = [ArtifactVerdict.model_validate(record) for record in records]
    except ValueError as exc:
        raise Refused(f"verdict manifest is not a valid verdict set: {exc}") from exc
    attestation = _attestation(args.attestation)
    arms = sorted({verdict.arm for verdict in verdicts})
    written: list[str] = []
    for arm in arms:
        rows = [verdict for verdict in verdicts if verdict.arm == arm]
        try:
            report = parity_rate(
                rows, confidence=args.confidence, attestation=attestation
            )
        except ParityBatteryError as exc:
            raise Refused(str(exc)) from exc
        print(report.claim)
        if report.sizing_shortfalls:
            for code in report.sizing_shortfalls:
                print(f"shortfall: {code}", file=sys.stderr)
        if report.quarantine_artifact_ids:
            print(
                "quarantine (task-pool defects, never labels): "
                + ", ".join(report.quarantine_artifact_ids),
                file=sys.stderr,
            )
        if args.reports_dir is not None:
            written.append(str(write_parity_report(report, args.reports_dir)))
    for path in written:
        print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elt-taskgen-parity-sample",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sample", help="draw the stratified replay sample")
    p.add_argument("--candidates", required=True, type=Path)
    p.add_argument("--out", default=None, type=Path)
    p.add_argument("--arm", default=None)
    p.add_argument(
        "--require-floors",
        action="store_true",
        help="exit 2 unless the drawn sample reaches the Table 9 floors",
    )
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser(
        "argv", help="print the runtime verify-* command line for each specimen"
    )
    p.add_argument("--sample", required=True, type=Path)
    p.add_argument("--release", required=True, type=Path)
    p.add_argument("--task-id", default=None)
    p.add_argument(
        "--destination-credential",
        required=True,
        type=Path,
        help="PATH only; this script never opens or prints the file",
    )
    p.add_argument("--no-certification-strict", action="store_true")
    p.set_defaults(func=cmd_argv)

    p = sub.add_parser("report", help="publish reports/parity_<arm>.json")
    p.add_argument("--verdicts", required=True, type=Path)
    p.add_argument("--reports-dir", default=None, type=Path)
    p.add_argument("--confidence", type=float, default=0.85)
    p.add_argument(
        "--attestation",
        default=None,
        type=Path,
        help=(
            "sealed sandbox_attestation.json the real arm ran under; its "
            "digest is stamped on the report and its seal is verified first"
        ),
    )
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except ParityBatteryError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())

"""Convert taskgen bundles to the original ELT-Bench runner layout.

Public files are copied unchanged. Private evaluator files are merged under the
canonical runtime namespace, and unrelated upstream entries are preserved. The
conversion is deterministic and does not contact cloud services.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from elt_taskgen.destinations import (
    Destination,
    destination_contract,
    destination_from_config,
    normalize_destination,
)
from elt_taskgen.export.eltbench import assert_public_runtime_shape


UPSTREAM_INPUT_DIRECTORY: dict[Destination, str] = {
    Destination.SNOWFLAKE: "inputs",
    Destination.DATABRICKS: "inputs_databricks",
    Destination.REDSHIFT: "inputs_redshift",
}


class UpstreamLayoutError(ValueError):
    """A generated cohort cannot be represented safely in upstream layout."""


@dataclass(frozen=True)
class TaskBundle:
    """One generated combined task and the identity used by upstream."""

    label: str
    canonical_id: str
    runtime_name: str
    destination: Destination
    public_dir: Path
    private_dir: Path

    @property
    def identifiers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.label, self.canonical_id, self.runtime_name)))


@dataclass(frozen=True)
class MaterializedTask:
    label: str
    canonical_id: str
    runtime_name: str


@dataclass(frozen=True)
class MaterializationReport:
    destination: Destination
    inputs_root: Path
    evaluation_root: Path
    tasks: tuple[MaterializedTask, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "destination": self.destination.value,
            "inputs_root": str(self.inputs_root),
            "evaluation_root": str(self.evaluation_root),
            "task_count": len(self.tasks),
            "tasks": [
                {
                    "label": task.label,
                    "canonical_id": task.canonical_id,
                    "runtime_name": task.runtime_name,
                }
                for task in self.tasks
            ],
            "cloud_runs": 0,
        }


@dataclass(frozen=True)
class ProjectionReport:
    upstream_root: Path
    destinations: tuple[MaterializationReport, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "upstream_root": str(self.upstream_root),
            "destinations": {
                report.destination.value: report.as_dict()
                for report in self.destinations
            },
            "cloud_runs": 0,
        }


@dataclass(frozen=True)
class _EvaluatorArtifacts:
    table_counts: dict[str, int]
    sort_keys: dict[str, list[str]]
    sql_files: dict[str, bytes]
    ground_truth_files: dict[str, bytes]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"required JSON file is missing: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise UpstreamLayoutError(f"cannot read JSON file {path}: {exc}") from exc


def _read_json_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise UpstreamLayoutError(f"JSON root must be an object: {path}")
    return dict(payload)


def _safe_component(value: Any, *, field: str) -> str:
    name = str(value or "")
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise UpstreamLayoutError(
            f"{field} must be one non-empty path component, got {name!r}"
        )
    return name


def _public_runtime(public_dir: Path) -> tuple[Destination, str]:
    config_path = public_dir / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"public config is missing: {config_path}") from None
    except (OSError, yaml.YAMLError) as exc:
        raise UpstreamLayoutError(f"cannot read public config {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise UpstreamLayoutError(f"public config root must be an object: {config_path}")
    try:
        destination = destination_from_config(config)
        contract = destination_contract(destination)
        values = config[contract.config_section]["config"]
        runtime_name = values[contract.logical_namespace_field]
    except (KeyError, TypeError, ValueError) as exc:
        raise UpstreamLayoutError(
            f"cannot resolve destination/runtime namespace from {config_path}: {exc}"
        ) from exc
    return destination, _safe_component(runtime_name, field="runtime namespace")


def _validate_public_tree(bundle: TaskBundle) -> None:
    public = bundle.public_dir
    try:
        assert_public_runtime_shape(public)
    except ValueError as exc:
        raise UpstreamLayoutError(str(exc)) from exc
    contract = destination_contract(bundle.destination)
    required_files = (
        public / "config.yaml",
        public / "data_model.yaml",
        public / "check_job_status.py",
        public / "elt" / "main.tf",
        public / contract.credential_filename,
    )
    for path in required_files:
        if not path.is_file():
            raise FileNotFoundError(f"required public runner file is missing: {path}")
    for path in (public / "schemas", public / "documentation", public / "elt"):
        if not path.is_dir():
            raise FileNotFoundError(f"required public runner directory is missing: {path}")
    if not any((public / "schemas").glob("*.csv")):
        raise UpstreamLayoutError(f"public schemas directory is empty: {public / 'schemas'}")

    forbidden_names = {"answer_key", "gold", "gt", "oracle", "table.json", "sort_key.json"}
    for path in sorted(public.rglob("*")):
        if path.is_symlink():
            raise UpstreamLayoutError(f"public tree contains a symlink: {path}")
        if path.name in forbidden_names or path.suffix == ".duckdb":
            raise UpstreamLayoutError(f"private evaluator artifact leaked into public tree: {path}")


def _task_ir_id(private_dir: Path) -> str | None:
    for rel in ("task_ir.json", "semantic/task_ir.json"):
        path = private_dir / rel
        if not path.is_file():
            continue
        payload = _read_json_object(path)
        value = payload.get("task_id")
        if isinstance(value, str) and value:
            return value
    return None


def _bundle(
    *,
    label: Any,
    canonical_id: Any,
    public_dir: Path,
    private_dir: Path,
    expected_runtime: Any = None,
    expected_destination: Any = None,
) -> TaskBundle:
    label_text = _safe_component(label, field="task label")
    canonical_text = _safe_component(canonical_id, field="canonical task id")
    public_dir = public_dir.resolve()
    private_dir = private_dir.resolve()
    if not public_dir.is_dir():
        raise FileNotFoundError(f"public task directory is missing: {public_dir}")
    if not private_dir.is_dir():
        raise FileNotFoundError(f"private task directory is missing: {private_dir}")
    destination, runtime_name = _public_runtime(public_dir)
    if expected_runtime is not None and runtime_name != str(expected_runtime):
        raise UpstreamLayoutError(
            f"catalog runtime namespace {expected_runtime!r} disagrees with "
            f"{public_dir / 'config.yaml'} ({runtime_name!r})"
        )
    if expected_destination is not None:
        try:
            declared_destination = normalize_destination(expected_destination)
        except ValueError as exc:
            raise UpstreamLayoutError(
                f"invalid declared destination for {label_text}: {expected_destination!r}"
            ) from exc
        if destination is not declared_destination:
            raise UpstreamLayoutError(
                f"declared destination {declared_destination.value!r} disagrees with "
                f"{public_dir / 'config.yaml'} ({destination.value!r})"
            )
    private_task_id = _task_ir_id(private_dir)
    if private_task_id is not None and private_task_id != canonical_text:
        raise UpstreamLayoutError(
            f"private TaskIR id {private_task_id!r} disagrees with canonical id "
            f"{canonical_text!r} for {label_text}"
        )
    result = TaskBundle(
        label=label_text,
        canonical_id=canonical_text,
        runtime_name=runtime_name,
        destination=destination,
        public_dir=public_dir,
        private_dir=private_dir,
    )
    _validate_public_tree(result)
    return result


def _catalog_bundles(root: Path, catalog: Mapping[str, Any]) -> list[TaskBundle]:
    rows = catalog.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise UpstreamLayoutError(f"catalog has no task rows: {root / 'catalog.json'}")
    bundles: list[TaskBundle] = []
    for row in rows:
        if not isinstance(row, dict):
            raise UpstreamLayoutError("catalog task rows must be objects")
        canonical_id = _safe_component(row.get("canonical_id"), field="canonical task id")
        label = _safe_component(row.get("alias") or canonical_id, field="task label")
        reference_root = root / "tasks" / label
        if (reference_root / "public").is_dir():
            public_dir = reference_root / "public"
            private_dir = reference_root / "private"
        else:
            public_dir = root / "public" / canonical_id
            private_dir = root / "private" / canonical_id
        bundles.append(
            _bundle(
                label=label,
                canonical_id=canonical_id,
                public_dir=public_dir,
                private_dir=private_dir,
                expected_runtime=row.get("runtime_namespace"),
                expected_destination=row.get("destination", catalog.get("destination")),
            )
        )
    return bundles


def _release_bundles(root: Path, manifest: Mapping[str, Any]) -> list[TaskBundle]:
    tasks = manifest.get("tasks")
    destinations = manifest.get("destinations")
    if not isinstance(tasks, dict) or not tasks:
        raise UpstreamLayoutError(
            f"release manifest has no task map: {root / 'release_manifest.json'}"
        )
    if not isinstance(destinations, dict):
        raise UpstreamLayoutError("release manifest has no destinations map")
    return [
        _bundle(
            label=canonical_id,
            canonical_id=canonical_id,
            public_dir=root / "public" / canonical_id,
            private_dir=root / "private" / canonical_id,
            expected_destination=destinations.get(canonical_id),
        )
        for canonical_id in sorted(tasks)
    ]


def _structural_bundles(root: Path) -> list[TaskBundle]:
    bundles: list[TaskBundle] = []
    task_roots = sorted(
        path for path in (root / "tasks").glob("*") if (path / "public").is_dir()
    )
    for task_root in task_roots:
        private = task_root / "private"
        canonical_id = _task_ir_id(private) or task_root.name
        bundles.append(
            _bundle(
                label=task_root.name,
                canonical_id=canonical_id,
                public_dir=task_root / "public",
                private_dir=private,
            )
        )
    if bundles:
        return bundles

    for public in sorted(path for path in (root / "public").glob("*") if path.is_dir()):
        private = root / "private" / public.name
        canonical_id = _task_ir_id(private) or public.name
        bundles.append(
            _bundle(
                label=public.name,
                canonical_id=canonical_id,
                public_dir=public,
                private_dir=private,
            )
        )
    return bundles


def discover_task_bundles(cohort_root: Path | str) -> tuple[TaskBundle, ...]:
    """Discover reference-cohort or schema-3 release task bundles."""

    root = Path(cohort_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"cohort root does not exist: {root}")
    catalog_path = root / "catalog.json"
    release_path = root / "release_manifest.json"
    if catalog_path.is_file():
        bundles = _catalog_bundles(root, _read_json_object(catalog_path))
    elif release_path.is_file():
        bundles = _release_bundles(root, _read_json_object(release_path))
    else:
        bundles = _structural_bundles(root)
    if not bundles:
        raise UpstreamLayoutError(f"no generated public/private tasks found under {root}")

    by_runtime: dict[str, TaskBundle] = {}
    by_runtime_folded: dict[str, str] = {}
    canonical_ids: set[str] = set()
    for bundle in bundles:
        if bundle.canonical_id in canonical_ids:
            raise UpstreamLayoutError(f"duplicate canonical task id: {bundle.canonical_id}")
        canonical_ids.add(bundle.canonical_id)
        if bundle.runtime_name in by_runtime:
            raise UpstreamLayoutError(f"duplicate runtime namespace: {bundle.runtime_name}")
        folded = bundle.runtime_name.casefold()
        if folded in by_runtime_folded:
            raise UpstreamLayoutError(
                "runtime namespaces collide case-insensitively: "
                f"{by_runtime_folded[folded]!r}, {bundle.runtime_name!r}"
            )
        by_runtime[bundle.runtime_name] = bundle
        by_runtime_folded[folded] = bundle.runtime_name
    return tuple(sorted(bundles, key=lambda item: (item.runtime_name, item.canonical_id)))


def select_task_bundles(
    bundles: Sequence[TaskBundle], selectors: Iterable[str] = ()
) -> tuple[TaskBundle, ...]:
    """Select by alias, canonical id, or canonical runtime namespace."""

    requested = tuple(dict.fromkeys(str(value) for value in selectors if str(value)))
    if not requested:
        return tuple(sorted(bundles, key=lambda item: (item.runtime_name, item.canonical_id)))
    selected: dict[str, TaskBundle] = {}
    for selector in requested:
        matches = [bundle for bundle in bundles if selector in bundle.identifiers]
        if not matches:
            raise UpstreamLayoutError(f"unknown task selector: {selector!r}")
        if len(matches) != 1:
            raise UpstreamLayoutError(f"ambiguous task selector: {selector!r}")
        selected[matches[0].canonical_id] = matches[0]
    return tuple(sorted(selected.values(), key=lambda item: (item.runtime_name, item.canonical_id)))


def _files_by_suffix(
    root: Path, suffix: str, *, ignored_names: Iterable[str] = ()
) -> dict[str, bytes]:
    if not root.is_dir():
        raise FileNotFoundError(f"required evaluator directory is missing: {root}")
    files: dict[str, bytes] = {}
    ignored = set(ignored_names)
    for path in sorted(root.iterdir()):
        if path.is_symlink():
            raise UpstreamLayoutError(f"evaluator tree contains a symlink: {path}")
        if path.is_dir():
            raise UpstreamLayoutError(f"unexpected nested evaluator directory: {path}")
        if path.name in ignored:
            continue
        if not path.is_file() or path.suffix != suffix:
            raise UpstreamLayoutError(f"unexpected evaluator artifact: {path}")
        files[path.stem] = path.read_bytes()
    if not files:
        raise UpstreamLayoutError(f"evaluator directory has no {suffix} files: {root}")
    return files


def _evaluator_artifacts(bundle: TaskBundle) -> _EvaluatorArtifacts:
    answer_key = bundle.private_dir / "answer_key"
    table_payload = _read_json_object(answer_key / "table.json")
    sort_payload = _read_json_object(answer_key / "sort_key.json")
    if set(table_payload) != {bundle.runtime_name}:
        raise UpstreamLayoutError(
            f"{answer_key / 'table.json'} must contain exactly runtime namespace "
            f"{bundle.runtime_name!r}, found {sorted(table_payload)}"
        )
    if set(sort_payload) != {bundle.runtime_name}:
        raise UpstreamLayoutError(
            f"{answer_key / 'sort_key.json'} must contain exactly runtime namespace "
            f"{bundle.runtime_name!r}, found {sorted(sort_payload)}"
        )
    counts = table_payload[bundle.runtime_name]
    sort_keys = sort_payload[bundle.runtime_name]
    if not isinstance(counts, dict) or not counts or not all(
        isinstance(name, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        for name, value in counts.items()
    ):
        raise UpstreamLayoutError(f"malformed source counts in {answer_key / 'table.json'}")
    if not isinstance(sort_keys, dict) or not sort_keys or not all(
        isinstance(name, str)
        and isinstance(columns, list)
        and columns
        and all(isinstance(column, str) and column for column in columns)
        for name, columns in sort_keys.items()
    ):
        raise UpstreamLayoutError(f"malformed mart sort keys in {answer_key / 'sort_key.json'}")

    sql_files = _files_by_suffix(answer_key / "evaluation" / "sql", ".sql")
    primary_gt = answer_key / "gt"
    ignored_gt_names: tuple[str, ...] = ()
    if not primary_gt.is_dir():
        primary_gt = answer_key / "gold" / "primary"
        ignored_gt_names = ("stage1_counts.json",)
    gt_files = _files_by_suffix(
        primary_gt, ".csv", ignored_names=ignored_gt_names
    )
    expected_marts = set(sort_keys)
    if set(sql_files) != expected_marts:
        raise UpstreamLayoutError(
            f"evaluation SQL coverage for {bundle.label} is "
            f"{sorted(sql_files)}, expected {sorted(expected_marts)}"
        )
    if set(gt_files) != expected_marts:
        raise UpstreamLayoutError(
            f"ground-truth coverage for {bundle.label} is "
            f"{sorted(gt_files)}, expected {sorted(expected_marts)}"
        )
    return _EvaluatorArtifacts(
        table_counts={str(name): int(value) for name, value in counts.items()},
        sort_keys={str(name): list(columns) for name, columns in sort_keys.items()},
        sql_files=sql_files,
        ground_truth_files=gt_files,
    )


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy_tree(source: Path, destination: Path, *, writable: bool) -> None:
    """Copy a regular tree in stable path order, never following symlinks."""

    destination.mkdir(parents=True)
    for source_path in sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix()):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_symlink():
            raise UpstreamLayoutError(f"refusing to copy symlink: {source_path}")
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            continue
        if not source_path.is_file():
            raise UpstreamLayoutError(f"refusing to copy non-regular file: {source_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)
        mode = stat.S_IMODE(source_path.stat().st_mode)
        if writable:
            mode |= stat.S_IRUSR | stat.S_IWUSR
        os.chmod(destination_path, mode)


def _replace_tree(staged: Path, destination: Path, *, backup_root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"previous-{destination.name}"
    _remove_path(backup)
    had_previous = destination.exists() or destination.is_symlink()
    if had_previous:
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException:
        if had_previous and (backup.exists() or backup.is_symlink()):
            os.replace(backup, destination)
        raise
    if had_previous:
        _remove_path(backup)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def materialize_upstream_layout(
    cohort_root: Path | str,
    inputs_root: Path | str,
    evaluation_root: Path | str,
    *,
    only: Iterable[str] = (),
    destination: Destination | str | None = None,
) -> MaterializationReport:
    """Merge selected tasks into an upstream input and evaluation layout.

    Leave unselected tasks unchanged and replace selected task files exactly.
    Repeated runs remove stale files and produce the same layout.
    """

    requested = tuple(only)
    bundles = select_task_bundles(discover_task_bundles(cohort_root), requested)
    destinations = {bundle.destination for bundle in bundles}
    if len(destinations) != 1:
        raise UpstreamLayoutError(
            "one inputs root cannot contain mixed destinations: "
            f"{sorted(item.value for item in destinations)}"
        )
    selected_destination = next(iter(destinations))
    if destination is not None:
        expected_destination = normalize_destination(destination)
        if selected_destination is not expected_destination:
            raise UpstreamLayoutError(
                f"selected task destination is {selected_destination.value!r}, "
                f"not requested {expected_destination.value!r}"
            )

    # Validate and read every private artifact before creating/replacing output.
    artifacts = {bundle.canonical_id: _evaluator_artifacts(bundle) for bundle in bundles}
    inputs_root = Path(inputs_root).resolve()
    evaluation_root = Path(evaluation_root).resolve()
    for bundle in bundles:
        target = inputs_root / bundle.runtime_name
        if (
            target == bundle.public_dir
            or target in bundle.public_dir.parents
            or bundle.public_dir in target.parents
        ):
            raise UpstreamLayoutError(
                f"input target overlaps its generated public source: {target}"
            )

    table_path = evaluation_root / "table.json"
    sort_path = evaluation_root / "sort_key.json"
    merged_table = _read_json_object(table_path, missing_ok=True)
    merged_sort = _read_json_object(sort_path, missing_ok=True)
    for bundle in bundles:
        item = artifacts[bundle.canonical_id]
        merged_table[bundle.runtime_name] = item.table_counts
        merged_sort[bundle.runtime_name] = item.sort_keys

    inputs_root.parent.mkdir(parents=True, exist_ok=True)
    evaluation_root.parent.mkdir(parents=True, exist_ok=True)
    input_stage = Path(
        tempfile.mkdtemp(dir=inputs_root.parent, prefix=".upstream-inputs-stage-")
    )
    evaluation_stage = Path(
        tempfile.mkdtemp(dir=evaluation_root.parent, prefix=".upstream-evaluation-stage-")
    )
    try:
        staged_inputs = input_stage / "tasks"
        staged_sql = evaluation_stage / "sql"
        staged_gt = evaluation_stage / "gt"
        for bundle in bundles:
            _copy_tree(
                bundle.public_dir,
                staged_inputs / bundle.runtime_name,
                writable=True,
            )
            item = artifacts[bundle.canonical_id]
            sql_dir = staged_sql / bundle.runtime_name
            gt_dir = staged_gt / bundle.runtime_name
            sql_dir.mkdir(parents=True)
            gt_dir.mkdir(parents=True)
            for mart_name, content in sorted(item.sql_files.items()):
                (sql_dir / f"{mart_name}.sql").write_bytes(content)
            for mart_name, content in sorted(item.ground_truth_files.items()):
                (gt_dir / f"{mart_name}.csv").write_bytes(content)

        inputs_root.mkdir(parents=True, exist_ok=True)
        sql_root = evaluation_root / "sql"
        gt_root = evaluation_root / "agent_results" / f"gt_{selected_destination.value}"
        sql_root.mkdir(parents=True, exist_ok=True)
        gt_root.mkdir(parents=True, exist_ok=True)
        for bundle in bundles:
            _replace_tree(
                staged_inputs / bundle.runtime_name,
                inputs_root / bundle.runtime_name,
                backup_root=input_stage,
            )
            _replace_tree(
                staged_sql / bundle.runtime_name,
                sql_root / bundle.runtime_name,
                backup_root=evaluation_stage,
            )
            _replace_tree(
                staged_gt / bundle.runtime_name,
                gt_root / bundle.runtime_name,
                backup_root=evaluation_stage,
            )
        _write_json_atomic(table_path, merged_table)
        _write_json_atomic(sort_path, merged_sort)
    finally:
        shutil.rmtree(input_stage, ignore_errors=True)
        shutil.rmtree(evaluation_stage, ignore_errors=True)

    return MaterializationReport(
        destination=selected_destination,
        inputs_root=inputs_root,
        evaluation_root=evaluation_root,
        tasks=tuple(
            MaterializedTask(
                label=bundle.label,
                canonical_id=bundle.canonical_id,
                runtime_name=bundle.runtime_name,
            )
            for bundle in bundles
        ),
    )


def materialize_upstream_repository(
    cohort_root: Path | str,
    upstream_root: Path | str,
    *,
    only: Iterable[str] = (),
    destination: Destination | str | None = None,
) -> MaterializationReport:
    """Materialize into the canonical directories of an ELT-Bench checkout."""

    selectors = tuple(only)
    selected = select_task_bundles(discover_task_bundles(cohort_root), selectors)
    destinations = {bundle.destination for bundle in selected}
    if len(destinations) != 1:
        raise UpstreamLayoutError("cannot choose an upstream inputs directory for mixed destinations")
    actual_destination = next(iter(destinations))
    if destination is not None and actual_destination is not normalize_destination(destination):
        raise UpstreamLayoutError(
            f"cohort targets {actual_destination.value!r}, not {normalize_destination(destination).value!r}"
        )
    root = Path(upstream_root).resolve()
    return materialize_upstream_layout(
        cohort_root,
        root / UPSTREAM_INPUT_DIRECTORY[actual_destination],
        root / "evaluation",
        only=selectors,
        destination=actual_destination,
    )


def _task_ir_path(private_dir: Path) -> Path:
    for rel in ("task_ir.json", "semantic/task_ir.json"):
        path = private_dir / rel
        if path.is_file():
            return path
    raise FileNotFoundError(f"private TaskIR is missing under {private_dir}")


def project_all_destinations(
    cohort_root: Path | str,
    upstream_root: Path | str,
    *,
    only: Iterable[str] = (),
) -> ProjectionReport:
    """Create an upstream-compatible layout for every destination.

    Write destination input directories and a shared evaluation tree. This is
    an offline projection, not runtime certification.
    """

    # Local imports keep the basic layout materializer independent of the
    # heavier TaskIR/gold machinery and make the use of existing export APIs
    # explicit.
    from elt_taskgen.export.eltbench import (
        DEFAULT_FLAT_FILES_BASE_URL,
        DEFAULT_REST_BASE_URL,
        database_name,
        export_task,
    )
    from elt_taskgen.models import task_from_json
    from elt_taskgen.reference.gold import load_gold

    selectors = tuple(only)
    source_bundles = select_task_bundles(discover_task_bundles(cohort_root), selectors)
    shared: list[tuple[TaskBundle, Any, Any]] = []
    for bundle in source_bundles:
        task = task_from_json(_task_ir_path(bundle.private_dir).read_text(encoding="utf-8"))
        if task.task_id != bundle.canonical_id:
            raise UpstreamLayoutError(
                f"TaskIR id {task.task_id!r} disagrees with {bundle.canonical_id!r}"
            )
        if database_name(task) != bundle.runtime_name:
            raise UpstreamLayoutError(
                f"TaskIR-derived runtime namespace {database_name(task)!r} disagrees "
                f"with public config namespace {bundle.runtime_name!r}"
            )
        gold = load_gold(bundle.private_dir / "answer_key")
        if gold.task_id != task.task_id or gold.task_content_hash != task.content_hash():
            raise UpstreamLayoutError(f"gold identity is stale for {bundle.label}")
        shared.append((bundle, task, gold))

    root = Path(upstream_root).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    reports: list[MaterializationReport] = []
    with tempfile.TemporaryDirectory(
        dir=root.parent, prefix=".upstream-destination-projection-"
    ) as temporary:
        projection_workspace = Path(temporary)
        for projected_destination in Destination:
            cohort = projection_workspace / projected_destination.value
            rows: list[dict[str, str]] = []
            for bundle, task, gold in shared:
                projected_root = cohort / "tasks" / bundle.label
                source_population = bundle.private_dir / "populations" / "development"
                if source_population.is_dir():
                    _copy_tree(
                        source_population,
                        projected_root / "populations" / "development",
                        writable=False,
                    )
                export_task(
                    task,
                    gold,
                    projected_root / "public",
                    projected_root / "private" / "answer_key",
                    destination=projected_destination,
                    flat_files_base_url=DEFAULT_FLAT_FILES_BASE_URL,
                    rest_base_url=DEFAULT_REST_BASE_URL,
                )
                runtime_name = database_name(task)
                rows.append(
                    {
                        "alias": bundle.label,
                        "canonical_id": bundle.canonical_id,
                        "runtime_namespace": runtime_name,
                        "destination": projected_destination.value,
                    }
                )
            (cohort / "catalog.json").parent.mkdir(parents=True, exist_ok=True)
            (cohort / "catalog.json").write_text(
                json.dumps(
                    {
                        "schema_version": "upstream-projection-1",
                        "destination": projected_destination.value,
                        "tasks": rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            reports.append(
                materialize_upstream_repository(
                    cohort,
                    root,
                    destination=projected_destination,
                )
            )
    return ProjectionReport(upstream_root=root, destinations=tuple(reports))

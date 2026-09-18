"""Certify a local evaluator package from a temporary copy.

Checks cover evaluator execution, rejection of wrong output, and denial of a
controlled private-file read. The hash-bound receipt is stored outside the
immutable package. No model provider is called.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elt_taskgen.models import TaskVariant, canonical_json, task_from_json


FRESH_PACKAGE_VERIFICATION_SCHEMA_VERSION = "fresh-package-verification-v1"
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024


#: The fresh-copy control that re-scores the packaged canonical Terraform +
#: dbt artifact through the RLVR workspace channel.
CANONICAL_CONTROL_NAME = "canonical_workspace_reachable"


class PackageControlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    expected: str
    observed: str
    populations_checked: tuple[str, ...] = ()


class PermissionCanaryResult(BaseModel):
    """A disposable OS-denied read, explicitly not a live-model attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempted: bool = False
    blocked: bool = False
    method: str = ""
    separate_os_principal: bool = False
    live_model_attempted: Literal[False] = False
    detail: str = ""


class FreshPackageVerificationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["fresh-package-verification-v1"] = (
        FRESH_PACKAGE_VERIFICATION_SCHEMA_VERSION
    )
    generated_at: str
    package_path: str
    task_id: str = ""
    task_content_hash: str = ""
    package_manifest_sha256: str = ""
    package_inventory_sha256: str = ""
    fresh_copy_method: str = ""
    fresh_copy_inventory_sha256: str = ""
    source_static_verified: bool = False
    fresh_copy_static_verified: bool = False
    package_inputs_from_fresh_copy: bool = False
    evaluator_runtime: dict[str, str] = Field(default_factory=dict)
    controls: tuple[PackageControlResult, ...] = ()
    permission_canary: PermissionCanaryResult = Field(
        default_factory=PermissionCanaryResult
    )
    live_model_attempted: Literal[False] = False
    failures: tuple[str, ...] = ()
    verified: bool = False
    receipt_sha256: str = ""

    @field_validator(
        "task_content_hash",
        "package_manifest_sha256",
        "package_inventory_sha256",
        "fresh_copy_inventory_sha256",
        "receipt_sha256",
    )
    @classmethod
    def _optional_digest(cls, value: str) -> str:
        if value and (
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("receipt digests must be lowercase sha256 values")
        return value

    @model_validator(mode="after")
    def _verified_means_all_checks_pass(self) -> "FreshPackageVerificationReceipt":
        controls_pass = bool(self.controls) and all(
            control.passed for control in self.controls
        )
        required = (
            self.source_static_verified,
            self.fresh_copy_static_verified,
            self.package_inputs_from_fresh_copy,
            bool(self.fresh_copy_method),
            bool(self.fresh_copy_inventory_sha256),
            self.fresh_copy_inventory_sha256 == self.package_inventory_sha256,
            controls_pass,
            # The workspace-channel control is mandatory: a receipt from before
            # it existed (three controls) can never certify a package.
            any(
                control.name == CANONICAL_CONTROL_NAME and control.passed
                for control in self.controls
            ),
            self.permission_canary.attempted,
            self.permission_canary.blocked,
        )
        if self.verified and (not all(required) or self.failures):
            raise ValueError(
                "a verified package receipt requires every provider-free check "
                "to pass and no failures"
            )
        return self

    def evidence_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class PersistedReceiptVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    receipt_sha256: str = ""
    failures: tuple[str, ...] = ()
    receipt: FreshPackageVerificationReceipt | None = None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None  # type: ignore[assignment]
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _evaluator_runtime_identity() -> dict[str, str]:
    import duckdb
    import pydantic
    import elt_taskgen
    from elt_taskgen.ingest_manifest import current_generator_pin

    module_path = Path(elt_taskgen.__file__).resolve()
    generator_pin = current_generator_pin()
    try:
        distribution_version = importlib.metadata.version("elt-taskgen")
    except importlib.metadata.PackageNotFoundError:
        distribution_version = "uninstalled"
    return {
        "python": sys.version.split()[0],
        "elt_taskgen_distribution": distribution_version,
        "elt_taskgen_module_path": str(module_path),
        "elt_taskgen_module_sha256": _sha256(module_path),
        "generator_digest_kind": generator_pin.digest_kind,
        "generator_sha256": generator_pin.sha256,
        "generator_version": generator_pin.version,
        "dependency_lock_sha256": generator_pin.lock_sha256,
        "duckdb": str(duckdb.__version__),
        "pydantic": str(pydantic.__version__),
    }


def _inventory_digest(manifest: Any) -> str:
    return hashlib.sha256(
        canonical_json(dict(manifest.files)).encode("utf-8")
    ).hexdigest()


def _has_original_path_reference(fresh_package: Path, original: Path) -> str:
    needle = str(original).encode("utf-8")
    for path in sorted(fresh_package.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8_000_000:
            continue
        if needle in path.read_bytes():
            return (
                f"fresh package file {path.relative_to(fresh_package)} contains "
                "the original package absolute path"
            )
    return ""


def _provider_free_controls(fresh_package: Path, task_id: str) -> tuple[PackageControlResult, ...]:
    """Execute positive and deliberately wrong submissions from copied assets."""

    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.reference.runner import run_reference
    from elt_taskgen.verification.upstream_eval import evaluate_variant

    private = fresh_package / "private" / task_id
    task = task_from_json((private / "task_ir.json").read_text(encoding="utf-8"))
    gold = gold_mod.load_gold(private / "answer_key")
    runner_workspace = fresh_package.parent / "control-workspace"
    populations_destination = (
        runner_workspace / "tasks" / task.task_id / "populations"
    )
    shutil.copytree(private / "populations", populations_destination)

    positive_full: list[float] = []
    positive_el: list[float] = []
    positive_t: list[float] = []
    wrong_t: list[float] = []
    wrong_t_full: list[float] = []
    failed_el: list[float] = []
    failed_el_full: list[float] = []
    populations: list[str] = []
    for population in task.populations:
        populations.append(population.name.value)
        actual = run_reference(task, population.name, runner_workspace)
        positive_full.append(
            evaluate_variant(
                TaskVariant.FULL,
                task,
                gold,
                population.name,
                actual_stage1=actual.stage1_counts,
                actual_marts=actual.mart_rows,
            ).reward
        )
        positive_el.append(
            evaluate_variant(
                TaskVariant.EXTRACT_LOAD,
                task,
                gold,
                population.name,
                actual_stage1=actual.stage1_counts,
            ).reward
        )
        positive_t.append(
            evaluate_variant(
                TaskVariant.TRANSFORM,
                task,
                gold,
                population.name,
                actual_marts=actual.mart_rows,
            ).reward
        )

        # One malformed row is guaranteed not to equal a valid TaskIR mart:
        # non-empty gold lacks the declared columns, while empty gold differs in
        # row count. This avoids a false control pass on legitimately empty marts.
        invalid_marts = {
            mart.name: [{"__elt_taskgen_wrong_t_control__": "wrong"}]
            for mart in task.marts
        }
        wrong_t.append(
            evaluate_variant(
                TaskVariant.TRANSFORM,
                task,
                gold,
                population.name,
                actual_marts=invalid_marts,
            ).reward
        )
        wrong_t_full.append(
            evaluate_variant(
                TaskVariant.FULL,
                task,
                gold,
                population.name,
                actual_stage1=actual.stage1_counts,
                actual_marts=invalid_marts,
            ).reward
        )
        failed_el.append(
            evaluate_variant(
                TaskVariant.EXTRACT_LOAD,
                task,
                gold,
                population.name,
                actual_stage1={},
            ).reward
        )
        failed_el_full.append(
            evaluate_variant(
                TaskVariant.FULL,
                task,
                gold,
                population.name,
                actual_stage1={},
                actual_marts=actual.mart_rows,
            ).reward
        )

    checked = tuple(populations)
    return (
        PackageControlResult(
            name="trusted_reference_positive",
            passed=bool(checked)
            and all(value == 1.0 for value in positive_full + positive_el + positive_t),
            expected="FULL, EXTRACT_LOAD, and TRANSFORM rewards are exactly 1.0",
            observed=(
                f"full={positive_full}; extract_load={positive_el}; "
                f"transform={positive_t}"
            ),
            populations_checked=checked,
        ),
        PackageControlResult(
            name="wrong_transform_negative",
            passed=bool(checked)
            and all(value < 1.0 for value in wrong_t + wrong_t_full),
            expected="malformed T output is rejected by TRANSFORM and FULL",
            observed=f"transform={wrong_t}; full={wrong_t_full}",
            populations_checked=checked,
        ),
        PackageControlResult(
            name="failed_extract_load_negative",
            passed=bool(checked)
            and all(value == 0.0 for value in failed_el + failed_el_full),
            expected="missing EL work scores 0.0 for EXTRACT_LOAD and FULL",
            observed=f"extract_load={failed_el}; full={failed_el_full}",
            populations_checked=checked,
        ),
    )


def _canonical_reachability_control(
    fresh_package: Path, task_id: str
) -> PackageControlResult:
    """Re-score the packaged canonical Terraform + dbt artifact of EVERY
    destination the task ships, through the real workspace grader.

    This is the packaging-time twin of the transform battery's
    canonical-reachability gate: the gate judged the workspace bytes, this
    control judges the bytes that ship, on the pinned dbt runtime, with no
    provider involved.
    """
    import tempfile

    from elt_taskgen.export.eltbench import WAREHOUSE_DIRNAME
    from elt_taskgen.models import variant_task_id
    from elt_taskgen.training import canonical
    from elt_taskgen.training.package import shipped_destinations

    name = CANONICAL_CONTROL_NAME
    expected = (
        "the packaged canonical Terraform + dbt artifact of every shipped "
        "destination scores exactly 1.0 on every graded population through the "
        "workspace channel"
    )
    runtime_config = canonical.default_dbt_runtime_config()
    if not canonical.dbt_runtime_available(runtime_config):
        return PackageControlResult(
            name=name,
            passed=False,
            expected=expected,
            observed=(
                f"pinned dbt runtime unavailable at {canonical.DBT_RUNTIME_ROOT} "
                "(provision with: uv sync --project runtime-images/dbt-duckdb --locked)"
            ),
        )
    private = fresh_package / "private" / task_id
    public = fresh_package / "public" / task_id
    task = task_from_json((private / "task_ir.json").read_text(encoding="utf-8"))
    oracle_dir = (
        fresh_package
        / "public"
        / variant_task_id(task_id, TaskVariant.TRANSFORM)
        / WAREHOUSE_DIRNAME
    )
    scratch = Path(tempfile.mkdtemp(prefix="canonical-control-", dir=fresh_package.parent))
    observations: list[str] = []
    populations: tuple[str, ...] = ()
    passed = True
    try:
        try:
            destinations = shipped_destinations(private / "answer_key")
        except Exception as exc:  # noqa: BLE001 - the control fails closed
            return PackageControlResult(
                name=name,
                passed=False,
                expected=expected,
                observed=f"shipped destinations unreadable: {type(exc).__name__}: {exc}",
            )
        for destination in destinations:
            value = destination.value
            try:
                package = canonical.build_workspace_substrate(
                    task=task,
                    answer_key_dir=private / "answer_key",
                    public_dir=public,
                    populations_root=private / "populations",
                    oracle_dir=oracle_dir,
                    scratch=scratch / f"substrate-{value}",
                    destination=destination,
                )
                target = canonical.artifact_dir(private / "answer_key", destination)
                record = canonical.CanonicalReachabilityRecord.model_validate_json(
                    (target / "reachability.json").read_text(encoding="utf-8")
                )
                files = {
                    rel: (target / "elt" / rel).read_text(encoding="utf-8")
                    for rel in sorted(record.files)
                }
                sealed, result = canonical.score_canonical_artifact(
                    package,
                    files,
                    scratch=scratch / f"score-{value}",
                    runtime_config=runtime_config,
                )
            except Exception as exc:  # noqa: BLE001 - the control fails closed
                passed = False
                observations.append(f"{value}: {type(exc).__name__}: {exc}")
                continue
            reachable = canonical._result_is_full(result)
            same_bytes = sealed.submission.artifact_sha256 == record.artifact_sha256
            passed = passed and reachable and same_bytes
            populations = populations or tuple(result.graded_populations)
            observations.append(
                f"{value}: reward={result.reward}; failure="
                f"{result.failure.error_code.value if result.failure else None}; "
                f"shortfall={canonical.shortfall_summary(result)}; artifact_sha256 "
                f"packaged={record.artifact_sha256[:16]} scored="
                f"{sealed.submission.artifact_sha256[:16]}"
            )
        return PackageControlResult(
            name=name,
            passed=passed and bool(observations),
            expected=expected,
            observed="; ".join(observations) or "no destination was scored",
            populations_checked=populations,
        )
    finally:
        canonical.remove_scratch_tree(scratch)


def _permission_canary(root: Path) -> PermissionCanaryResult:
    """Attempt an actual denied open on a disposable mode-000 canary.

    The subprocess uses the same OS principal, which is stated explicitly. The
    evidence is the kernel-denied read itself, not mere directory separation.
    No model is invoked and no real private answer material is exposed.
    """

    if os.name != "posix":
        return PermissionCanaryResult(
            detail="OS permission canary requires POSIX mode enforcement"
        )
    boundary = root / "permission-boundary"
    boundary.mkdir(mode=0o700)
    public = boundary / "public-canary.txt"
    private = boundary / "private-canary.txt"
    public.write_text("public-control\n", encoding="utf-8")
    private.write_text("disposable-private-control\n", encoding="utf-8")
    public.chmod(0o444)
    private.chmod(0o000)
    script = (
        "from pathlib import Path; import sys; "
        "public=Path(sys.argv[1]); private=Path(sys.argv[2]); "
        "public_ok=(public.read_text(encoding='utf-8')=='public-control\\n'); "
        "blocked=False; "
        "\ntry:\n private.read_bytes()\n"
        "except PermissionError:\n blocked=True\n"
        "raise SystemExit(0 if public_ok and blocked else 9)"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script, str(public), str(private)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        blocked = completed.returncode == 0
        detail = (
            "public control was readable and the kernel denied the mode-000 "
            "private canary"
            if blocked
            else "permission subprocess did not demonstrate the expected denied read"
        )
        return PermissionCanaryResult(
            attempted=True,
            blocked=blocked,
            method="isolated-python-subprocess/same-principal/mode-000",
            separate_os_principal=False,
            live_model_attempted=False,
            detail=detail,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return PermissionCanaryResult(
            attempted=True,
            blocked=False,
            method="isolated-python-subprocess/same-principal/mode-000",
            separate_os_principal=False,
            live_model_attempted=False,
            detail=f"permission canary failed to execute: {type(exc).__name__}: {exc}",
        )
    finally:
        private.chmod(0o600)
        public.chmod(0o600)


def verify_fresh_local_package(
    package_path: Path,
    *,
    receipt_path: Path,
    scratch_root: Path | None = None,
) -> FreshPackageVerificationReceipt:
    """Verify a copied package, run controls, and atomically persist a receipt.

    ``receipt_path`` must be outside the immutable package. A receipt is written
    for both PASS and FAIL so interrupted/failed terminal verification remains
    auditable. The return value is identical to the persisted JSON.
    """

    from elt_taskgen.export.local_package import (
        LOCAL_PACKAGE_MANIFEST,
        LocalPackageManifest,
        verify_local_package,
    )

    package = Path(package_path).absolute()
    receipt_destination = Path(receipt_path).absolute()
    if receipt_destination == package or receipt_destination.is_relative_to(package):
        raise ValueError("package verification receipt must be outside the package")

    failures: list[str] = []
    task_id = ""
    task_content_hash = ""
    manifest_sha256 = ""
    inventory_sha256 = ""
    fresh_copy_method = ""
    fresh_copy_inventory_sha256 = ""
    source_static_verified = False
    fresh_static_verified = False
    package_inputs_from_fresh_copy = False
    controls: tuple[PackageControlResult, ...] = ()

    source_check = verify_local_package(package)
    if source_check.ok:
        source_static_verified = True
        task_id = source_check.task_id
        task_content_hash = source_check.task_content_hash
        manifest_path = package / LOCAL_PACKAGE_MANIFEST
        manifest_sha256 = _sha256(manifest_path)
        manifest = LocalPackageManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        inventory_sha256 = _inventory_digest(manifest)
    else:
        failures.extend(f"source static verification: {row}" for row in source_check.failures)

    scratch_parent = (
        Path(scratch_root).absolute()
        if scratch_root is not None
        else receipt_destination.parent
    )
    scratch_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if scratch_parent == package or scratch_parent.is_relative_to(package):
        raise ValueError("fresh verification scratch root must be outside the package")

    with tempfile.TemporaryDirectory(
        dir=scratch_parent, prefix=".fresh-package-verification-"
    ) as temporary:
        fresh_root = Path(temporary)
        fresh_package = fresh_root / "unpacked-package"
        if source_static_verified:
            try:
                fresh_copy_method = (
                    "shutil.copytree(symlinks=True) followed by verified "
                    "regular-tree inventory"
                )
                shutil.copytree(package, fresh_package, symlinks=True)
                fresh_check = verify_local_package(fresh_package)
                if not fresh_check.ok:
                    failures.extend(
                        f"fresh-copy static verification: {row}"
                        for row in fresh_check.failures
                    )
                elif (
                    fresh_check.task_id != task_id
                    or fresh_check.task_content_hash != task_content_hash
                    or _sha256(fresh_package / LOCAL_PACKAGE_MANIFEST)
                    != manifest_sha256
                ):
                    failures.append(
                        "fresh copy identity differs from the source package"
                    )
                else:
                    fresh_static_verified = True
                    fresh_manifest = LocalPackageManifest.model_validate_json(
                        (fresh_package / LOCAL_PACKAGE_MANIFEST).read_text(
                            encoding="utf-8"
                        )
                    )
                    fresh_copy_inventory_sha256 = _inventory_digest(
                        fresh_manifest
                    )
                    if fresh_copy_inventory_sha256 != inventory_sha256:
                        fresh_static_verified = False
                        failures.append(
                            "fresh copy inventory digest differs from the source "
                            "manifest inventory"
                        )

                original_reference = _has_original_path_reference(
                    fresh_package, package
                )
                if original_reference:
                    failures.append(original_reference)
                else:
                    package_inputs_from_fresh_copy = fresh_static_verified

                if fresh_static_verified and package_inputs_from_fresh_copy:
                    controls = _provider_free_controls(fresh_package, task_id) + (
                        _canonical_reachability_control(fresh_package, task_id),
                    )
                    failures.extend(
                        f"control {control.name}: {control.observed}"
                        for control in controls
                        if not control.passed
                    )
            except (OSError, TypeError, UnicodeError, ValueError, RuntimeError) as exc:
                failures.append(
                    "fresh-copy execution failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        permission = _permission_canary(fresh_root)
        if not (permission.attempted and permission.blocked):
            failures.append(f"permission canary: {permission.detail}")

    verified = bool(
        source_static_verified
        and fresh_static_verified
        and package_inputs_from_fresh_copy
        and controls
        and all(control.passed for control in controls)
        and permission.attempted
        and permission.blocked
        and not failures
    )
    receipt = FreshPackageVerificationReceipt(
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        package_path=str(package.resolve()) if package.exists() else str(package),
        task_id=task_id,
        task_content_hash=task_content_hash,
        package_manifest_sha256=manifest_sha256,
        package_inventory_sha256=inventory_sha256,
        fresh_copy_method=fresh_copy_method,
        fresh_copy_inventory_sha256=fresh_copy_inventory_sha256,
        source_static_verified=source_static_verified,
        fresh_copy_static_verified=fresh_static_verified,
        package_inputs_from_fresh_copy=package_inputs_from_fresh_copy,
        evaluator_runtime=_evaluator_runtime_identity(),
        controls=controls,
        permission_canary=permission,
        live_model_attempted=False,
        failures=tuple(failures),
        verified=verified,
    )
    receipt = receipt.model_copy(update={"receipt_sha256": receipt.evidence_digest()})
    _atomic_write(
        receipt_destination,
        (
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
    )
    return receipt


def _read_receipt(path: Path) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("package verification receipt must be a regular non-symlink file")
    if metadata.st_size > _MAX_RECEIPT_BYTES:
        raise ValueError("package verification receipt exceeds its safety bound")
    return path.read_bytes()


def verify_persisted_package_receipt(
    receipt_path: Path,
    *,
    package_path: Path,
    expected_task_id: str = "",
    expected_task_content_hash: str = "",
) -> PersistedReceiptVerification:
    """Revalidate a receipt against the package currently at its bound path."""

    from elt_taskgen.export.local_package import (
        LOCAL_PACKAGE_MANIFEST,
        LocalPackageManifest,
        verify_local_package,
    )

    failures: list[str] = []
    receipt: FreshPackageVerificationReceipt | None = None
    source = Path(receipt_path).absolute()
    package = Path(package_path).absolute()
    try:
        receipt = FreshPackageVerificationReceipt.model_validate_json(
            _read_receipt(source)
        )
        if not receipt.receipt_sha256:
            failures.append("package verification receipt has no evidence digest")
        elif receipt.receipt_sha256 != receipt.evidence_digest():
            failures.append("package verification receipt evidence digest differs")
        try:
            current_package_path = str(package.resolve(strict=True))
        except OSError as exc:
            raise ValueError(f"verified package path is unavailable: {exc}") from exc
        if receipt.package_path != current_package_path:
            failures.append("package verification receipt is bound to another path")
        if not receipt.verified:
            failures.append("package verification receipt records a failed verification")
        current_runtime = _evaluator_runtime_identity()
        if receipt.evaluator_runtime != current_runtime:
            changed = sorted(
                key
                for key in set(receipt.evaluator_runtime) | set(current_runtime)
                if receipt.evaluator_runtime.get(key) != current_runtime.get(key)
            )
            failures.append(
                "package verification evaluator runtime identity drifted"
                + (f": {changed}" if changed else "")
            )
        if expected_task_id and receipt.task_id != expected_task_id:
            failures.append("package verification receipt names another task_id")
        if (
            expected_task_content_hash
            and receipt.task_content_hash != expected_task_content_hash
        ):
            failures.append(
                "package verification receipt names another TaskIR content hash"
            )
        static = verify_local_package(package)
        if not static.ok:
            failures.extend(
                f"current package static verification: {row}"
                for row in static.failures
            )
        else:
            if static.task_id != receipt.task_id:
                failures.append("current package task_id differs from receipt")
            if static.task_content_hash != receipt.task_content_hash:
                failures.append("current package TaskIR hash differs from receipt")
            current_manifest_sha = _sha256(package / LOCAL_PACKAGE_MANIFEST)
            if current_manifest_sha != receipt.package_manifest_sha256:
                failures.append("current package manifest digest differs from receipt")
            current_manifest = LocalPackageManifest.model_validate_json(
                (package / LOCAL_PACKAGE_MANIFEST).read_text(encoding="utf-8")
            )
            current_inventory_sha = _inventory_digest(current_manifest)
            if current_inventory_sha != receipt.package_inventory_sha256:
                failures.append("current package inventory digest differs from receipt")
            if (
                receipt.fresh_copy_inventory_sha256
                != receipt.package_inventory_sha256
            ):
                failures.append(
                    "receipt fresh-copy inventory does not equal its source inventory"
                )
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        failures.append(f"invalid package verification receipt: {exc}")
    return PersistedReceiptVerification(
        ok=not failures,
        receipt_sha256=(receipt.receipt_sha256 if receipt is not None else ""),
        failures=tuple(failures),
        receipt=receipt,
    )


__all__ = [
    "FRESH_PACKAGE_VERIFICATION_SCHEMA_VERSION",
    "FreshPackageVerificationReceipt",
    "PackageControlResult",
    "PermissionCanaryResult",
    "PersistedReceiptVerification",
    "verify_fresh_local_package",
    "verify_persisted_package_receipt",
]

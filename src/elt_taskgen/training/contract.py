"""Define the cloud-free ``workspace-v1`` artifact contract independently of semantic scoring."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping


WORKSPACE_SUBMISSION_SCHEMA_VERSION = "workspace-v1"
WORKSPACE_RESULT_SCHEMA_VERSION = "workspace-result-v1"
#: 0.2.1: the shared comparator no longer matches an unequal non-finite
#: pair (R01). Results and canonical records scored under 0.2.0 are not
#: relabelled as measured by the fixed comparator.
#: 0.2.2: candidate YAML with an unhashable key, a cycle, excessive nesting or
#: alias expansion, or an unconstructable scalar is a scored project-invalid
#: failure, not a no-label harness fault (A02).
#: 0.2.3: the dbt rewrite moved to portable-dbt-sql-v5, which changes the
#: result of a bare DECIMAL on Databricks and Redshift and refuses alias-hidden
#: text casts the destinations render differently.
WORKSPACE_SCORER_VERSION = "0.2.3"
ARTIFACT_WORKFLOW_PROXY_CLAIM = "artifact_workflow_proxy"
WORKSPACE_POPULATION_AGGREGATION = "minimum"
WORKSPACE_PROFILE_OWNER = "harness"

# Bounds apply to the sealed candidate ``elt/`` tree, not to the public task.
MAX_WORKSPACE_FILE_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_FILES = 512
MAX_WORKSPACE_ACTIONS = 4096
MAX_WORKSPACE_MANIFEST_BYTES = 4 * 1024 * 1024

REQUIRED_WORKSPACE_FILES = frozenset(
    {
        "elt/main.tf",
        "elt/dbt_project.yml",
        "elt/models/sources.yml",
    }
)

# These are attempt-local outputs.  A sealed workspace containing one is stale
# by definition, even if its bytes happen to come from the current attempt.
FORBIDDEN_STATE_PARTS = frozenset({".terraform", "target", "logs"})
FORBIDDEN_STATE_NAMES = frozenset(
    {
        "attempt.duckdb",
        "profiles.yml",
        "terraform.tfstate",
        "terraform.tfstate.backup",
    }
)


class WorkspaceFailureClass(str, Enum):
    """Stable failure taxonomy and its training-label boundary."""

    POLICY_FAILURE = "policy_failure"
    POLICY_VIOLATION = "policy_violation"
    TASK_DEFECT = "task_defect"
    HARNESS_DEFECT = "harness_defect"
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    REAL_RUNTIME_FAILURE = "real_runtime_failure"

    @property
    def label_eligible(self) -> bool:
        return self in {
            WorkspaceFailureClass.POLICY_FAILURE,
            WorkspaceFailureClass.POLICY_VIOLATION,
        }


class WorkspaceErrorCode(str, Enum):
    """Initial stable public codes for the workspace lifecycle boundary."""

    SUBMISSION_INVALID = "submission_invalid"
    TASK_ID_MISMATCH = "task_id_mismatch"
    TASK_PACKAGE_INVALID = "task_package_invalid"
    WORKSPACE_NOT_FRESH = "workspace_not_fresh"
    WORKSPACE_PATH_ESCAPE = "workspace_path_escape"
    WORKSPACE_SYMLINK = "workspace_symlink"
    WORKSPACE_NON_REGULAR_FILE = "workspace_non_regular_file"
    WORKSPACE_CASE_COLLISION = "workspace_case_collision"
    WORKSPACE_PUBLIC_MUTATED = "workspace_public_mutated"
    WORKSPACE_MISSING_ARTIFACT = "workspace_missing_artifact"
    WORKSPACE_TRANSIENT_STATE = "workspace_transient_state"
    WORKSPACE_SECRET_LITERAL = "workspace_secret_literal"
    WORKSPACE_SIZE_LIMIT = "workspace_size_limit"
    WORKSPACE_DIGEST_MISMATCH = "workspace_digest_mismatch"
    HARNESS_INTERNAL = "harness_internal"
    INFRASTRUCTURE_UNAVAILABLE = "infrastructure_unavailable"
    REAL_RUNTIME_FAILED = "real_runtime_failed"


# Bind each public outcome code to its versioned label classification.
WORKSPACE_ERROR_CLASS_BY_CODE: Mapping[
    WorkspaceErrorCode, WorkspaceFailureClass
] = MappingProxyType(
    {
        WorkspaceErrorCode.SUBMISSION_INVALID: WorkspaceFailureClass.POLICY_FAILURE,
        WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT: WorkspaceFailureClass.POLICY_FAILURE,
        WorkspaceErrorCode.TASK_ID_MISMATCH: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_SYMLINK: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_CASE_COLLISION: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH: WorkspaceFailureClass.POLICY_VIOLATION,
        WorkspaceErrorCode.TASK_PACKAGE_INVALID: WorkspaceFailureClass.TASK_DEFECT,
        WorkspaceErrorCode.WORKSPACE_NOT_FRESH: WorkspaceFailureClass.HARNESS_DEFECT,
        WorkspaceErrorCode.HARNESS_INTERNAL: WorkspaceFailureClass.HARNESS_DEFECT,
        WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE: (
            WorkspaceFailureClass.TRANSIENT_INFRASTRUCTURE
        ),
        WorkspaceErrorCode.REAL_RUNTIME_FAILED: WorkspaceFailureClass.REAL_RUNTIME_FAILURE,
    }
)


__all__ = [
    "ARTIFACT_WORKFLOW_PROXY_CLAIM",
    "FORBIDDEN_STATE_NAMES",
    "FORBIDDEN_STATE_PARTS",
    "MAX_WORKSPACE_FILE_BYTES",
    "MAX_WORKSPACE_FILES",
    "MAX_WORKSPACE_ACTIONS",
    "MAX_WORKSPACE_MANIFEST_BYTES",
    "MAX_WORKSPACE_TOTAL_BYTES",
    "REQUIRED_WORKSPACE_FILES",
    "WORKSPACE_POPULATION_AGGREGATION",
    "WORKSPACE_PROFILE_OWNER",
    "WORKSPACE_RESULT_SCHEMA_VERSION",
    "WORKSPACE_SCORER_VERSION",
    "WORKSPACE_SUBMISSION_SCHEMA_VERSION",
    "WORKSPACE_ERROR_CLASS_BY_CODE",
    "WorkspaceErrorCode",
    "WorkspaceFailureClass",
]

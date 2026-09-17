"""Test double: a REACHABLE canonical-reachability record for a workspace
that never ran the real workspace grader.

The record's shape is the real one (every validator runs); only the score is
synthetic: reward 1.0 on every graded population. Tests that build a "fully
evidenced" workspace for the transform battery write it beside the other
recorded evidence so the canonical-reachability gate sees current evidence.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from elt_taskgen.models import TaskIR
from elt_taskgen.training import canonical
from elt_taskgen.training.models import PopulationWorkspaceScore, WorkspaceScoreResult
from elt_taskgen.verification.gates import GRADED_POPULATIONS

_DIGEST = "0" * 64


def full_workspace_result(task: TaskIR) -> WorkspaceScoreResult:
    populations = {}
    for population in GRADED_POPULATIONS:
        populations[population.value] = PopulationWorkspaceScore(
            population=population.value,
            graded=True,
            terraform_contract=1.0,
            sync_lifecycle=True,
            upstream_stage1=True,
            strict_raw_tables=1.0,
            strict_el_pass=True,
            dbt_project=1.0,
            mart_reward=1.0,
            raw_immutable=True,
            end_to_end_reward=1.0,
        )
    return WorkspaceScoreResult(
        release_id=f"canonical-{task.content_hash()[:16]}",
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        artifact_sha256=_DIGEST,
        valid_submission=True,
        graded_populations=tuple(p.value for p in GRADED_POPULATIONS),
        populations=populations,
        reward=1.0,
    )


def write_canonical_reachability(
    workspace: Path, task: TaskIR, *, destination: str = "snowflake"
) -> Path:
    """Record a synthetic reachable canonical artifact for ``task``."""
    import json

    from elt_taskgen.models import canonical_json

    task_dir = workspace / "tasks" / task.task_id
    contract_path = task_dir / "answer_key" / "runtime" / "airbyte_connector_contract.json"
    if contract_path.is_file():
        destination = json.loads(contract_path.read_text(encoding="utf-8"))["destination"]["key"]
    else:
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            json.dumps({"destination": {"key": destination}, "sources": []}) + "\n",
            encoding="utf-8",
        )
    contract_sha256 = hashlib.sha256(
        canonical_json(json.loads(contract_path.read_text(encoding="utf-8"))).encode("utf-8")
    ).hexdigest()
    files = {
        "main.tf": "# synthetic canonical main.tf\n",
        "dbt_project.yml": "name: synthetic\n",
        "models/sources.yml": "version: 2\n",
        **{f"models/{mart.name}.sql": "select 1\n" for mart in task.marts},
    }
    record = canonical.CanonicalReachabilityRecord(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        destination=destination,
        airbyte_contract_sha256=contract_sha256,
        reference_sql_sha256={
            mart: hashlib.sha256(sql.encode("utf-8")).hexdigest()
            for mart, sql in sorted(dict(task.reference.sql_by_mart).items())
        },
        files={rel: hashlib.sha256(text.encode("utf-8")).hexdigest() for rel, text in sorted(files.items())},
        artifact_sha256=_DIGEST,
        seal_sha256=_DIGEST,
        dbt_runtime_python="synthetic",
        result=full_workspace_result(task),
        reachable=True,
    )
    # validate-t clears every earlier destination's artifact before scoring.
    stale = task_dir / "answer_key" / canonical.CANONICAL_ARTIFACT_REL
    if stale.exists():
        canonical.remove_scratch_tree(stale)
    return canonical.record_canonical_reachability(
        task_dir=task_dir, answer_key_dir=task_dir / "answer_key", record=record, files=files
    )

"""Permanent local semantic scoring for combined ELT-Bench releases.

DuckDB is an evaluator-private execution engine here.  The public benchmark
contract remains Airbyte + dbt against a selected cloud destination.
"""

from elt_taskgen.semantic.models import (
    SEMANTIC_RESULT_SCHEMA_VERSION,
    SEMANTIC_SCORER_VERSION,
    SEMANTIC_SUBMISSION_SCHEMA_VERSION,
    PopulationSemanticScore,
    SemanticLimits,
    SemanticScoreResult,
    SemanticSubmission,
)
from elt_taskgen.semantic.package import (
    SemanticPackage,
    SemanticPackageError,
    load_semantic_package,
)
from elt_taskgen.semantic.scoring import (
    SemanticHarnessError,
    SemanticSubmissionError,
    parse_semantic_submission,
    score_semantic_submission,
    score_semantic_text,
)

__all__ = [
    "SEMANTIC_RESULT_SCHEMA_VERSION",
    "SEMANTIC_SCORER_VERSION",
    "SEMANTIC_SUBMISSION_SCHEMA_VERSION",
    "PopulationSemanticScore",
    "SemanticHarnessError",
    "SemanticLimits",
    "SemanticPackage",
    "SemanticPackageError",
    "SemanticScoreResult",
    "SemanticSubmission",
    "SemanticSubmissionError",
    "load_semantic_package",
    "parse_semantic_submission",
    "score_semantic_submission",
    "score_semantic_text",
]

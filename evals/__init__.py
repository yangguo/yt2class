"""M7 evaluation harness (manifest, scoring, comparison fixtures)."""

from evals.manifest import EvalManifest, load_manifest
from evals.scoring import (
    AdjudicatedSegmentScore,
    ComparisonMode,
    DimensionScores,
    Scorecard,
    aggregate_scorecard,
)

__all__ = [
    "AdjudicatedSegmentScore",
    "ComparisonMode",
    "DimensionScores",
    "EvalManifest",
    "Scorecard",
    "aggregate_scorecard",
    "load_manifest",
]

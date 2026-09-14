"""Scorecard models and aggregation for M7 quality evals."""

from __future__ import annotations

from enum import Enum
from statistics import mean
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ComparisonMode(str, Enum):
    EQUAL_INTERVAL = "equal-interval"
    VISUAL_DEDUPE = "visual-dedupe"
    TRANSCRIPT_ONLY = "transcript-only"
    FRAMES_MULTIMODAL = "frames-multimodal"
    HYBRID = "hybrid"


ScoreScale = Literal["0-1", "0-100", "pass-fail"]


class DimensionScores(BaseModel):
    """Separate reported dimensions (design §15). Values are 0.0–1.0 unless noted."""

    coverage: float = Field(ge=0.0, le=1.0)
    factual_error_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of checked claims with serious factual errors (lower is better).",
    )
    screenshot_readability: float = Field(ge=0.0, le=1.0)
    layout_quality: float = Field(ge=0.0, le=1.0)
    estimated_cost_usd: float = Field(ge=0.0)
    wall_clock_seconds: float = Field(ge=0.0)
    model_calls: int = Field(ge=0)
    image_count: int = Field(ge=0)
    notes: str | None = None

    @property
    def factual_accuracy(self) -> float:
        return max(0.0, 1.0 - self.factual_error_rate)


class RaterScores(BaseModel):
    rater_id: str
    scores: DimensionScores


class AdjudicatedSegmentScore(BaseModel):
    segment_id: str
    mode: ComparisonMode
    rater_a: RaterScores
    rater_b: RaterScores
    agreement: float = Field(ge=0.0, le=1.0, description="Mean absolute agreement on 0–1 dims.")
    merged: DimensionScores
    adjudication_notes: str | None = None

    @model_validator(mode="after")
    def _agreement_matches_raters(self) -> AdjudicatedSegmentScore:
        dims = ("coverage", "screenshot_readability", "layout_quality")
        deltas = []
        for name in dims:
            a = getattr(self.rater_a.scores, name)
            b = getattr(self.rater_b.scores, name)
            deltas.append(abs(a - b))
        expected = 1.0 - mean(deltas) if deltas else 1.0
        if abs(self.agreement - expected) > 0.05:
            raise ValueError("agreement field must match rater dimension deltas (±0.05)")
        return self


class Scorecard(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: str
    manifest_version: str
    mode: ComparisonMode
    segments: list[AdjudicatedSegmentScore] = Field(min_length=1)
    limits_acknowledged: list[str] = Field(default_factory=list)

    @property
    def mean_coverage(self) -> float:
        return mean(item.merged.coverage for item in self.segments)

    @property
    def mean_wall_clock_seconds(self) -> float:
        return mean(item.merged.wall_clock_seconds for item in self.segments)


def merge_rater_scores(a: DimensionScores, b: DimensionScores) -> DimensionScores:
    return DimensionScores(
        coverage=(a.coverage + b.coverage) / 2,
        factual_error_rate=(a.factual_error_rate + b.factual_error_rate) / 2,
        screenshot_readability=(a.screenshot_readability + b.screenshot_readability) / 2,
        layout_quality=(a.layout_quality + b.layout_quality) / 2,
        estimated_cost_usd=(a.estimated_cost_usd + b.estimated_cost_usd) / 2,
        wall_clock_seconds=(a.wall_clock_seconds + b.wall_clock_seconds) / 2,
        model_calls=max(a.model_calls, b.model_calls),
        image_count=max(a.image_count, b.image_count),
        notes="; ".join(filter(None, [a.notes, b.notes])) or None,
    )


def adjudicate_segment(
    *,
    segment_id: str,
    mode: ComparisonMode,
    rater_a: RaterScores,
    rater_b: RaterScores,
    adjudication_notes: str | None = None,
) -> AdjudicatedSegmentScore:
    dims = ("coverage", "screenshot_readability", "layout_quality")
    deltas = [abs(getattr(rater_a.scores, n) - getattr(rater_b.scores, n)) for n in dims]
    agreement = 1.0 - mean(deltas)
    merged = merge_rater_scores(rater_a.scores, rater_b.scores)
    return AdjudicatedSegmentScore(
        segment_id=segment_id,
        mode=mode,
        rater_a=rater_a,
        rater_b=rater_b,
        agreement=agreement,
        merged=merged,
        adjudication_notes=adjudication_notes,
    )


def aggregate_scorecard(scorecard: Scorecard) -> dict[str, float | int]:
    segments = scorecard.segments
    return {
        "segment_count": len(segments),
        "mean_coverage": scorecard.mean_coverage,
        "mean_factual_error_rate": mean(s.merged.factual_error_rate for s in segments),
        "mean_screenshot_readability": mean(s.merged.screenshot_readability for s in segments),
        "mean_layout_quality": mean(s.merged.layout_quality for s in segments),
        "total_estimated_cost_usd": sum(s.merged.estimated_cost_usd for s in segments),
        "total_wall_clock_seconds": sum(s.merged.wall_clock_seconds for s in segments),
        "mean_agreement": mean(s.agreement for s in segments),
    }

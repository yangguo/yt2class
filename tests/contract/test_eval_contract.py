"""M7 eval manifest and scoring contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.comparison import ComparisonRunInput, all_modes, run_comparison_fixture
from evals.manifest import load_manifest
from evals.performance import STANDARD_TIMELINES, estimate_timeline_performance
from evals.scoring import (
    AdjudicatedSegmentScore,
    ComparisonMode,
    DimensionScores,
    RaterScores,
    Scorecard,
    adjudicate_segment,
    aggregate_scorecard,
)

REPO = Path(__file__).resolve().parents[2]
SCORECARD_FIXTURE = REPO / "evals/reports/scorecard.fixture.json"


def test_manifest_has_five_categories_and_ten_short_segments():
    manifest = load_manifest()
    assert manifest.version == "1.0"
    assert len(manifest.categories) >= 5
    assert manifest.short_segment_count >= 10
    assert len(manifest.long_course) >= 1
    assert len(manifest.comparison_modes) >= 5
    assert "frames-multimodal" in manifest.comparison_modes


def test_manifest_fixtures_exist_on_disk():
    manifest = load_manifest()
    for segment_id in manifest.segment_ids():
        assert segment_id


def test_scorecard_fixture_validates_dual_adjudication():
    raw = json.loads(SCORECARD_FIXTURE.read_text(encoding="utf-8"))
    card = Scorecard.model_validate(raw)
    assert card.mode == ComparisonMode.FRAMES_MULTIMODAL
    segment = card.segments[0]
    assert segment.rater_a.rater_id != segment.rater_b.rater_id
    assert segment.agreement >= 0.9
    summary = aggregate_scorecard(card)
    assert summary["segment_count"] == 1
    assert summary["mean_coverage"] == segment.merged.coverage


def test_adjudicate_segment_computes_agreement():
    dims = DimensionScores(
        coverage=0.8,
        factual_error_rate=0.1,
        screenshot_readability=0.9,
        layout_quality=0.85,
        estimated_cost_usd=0.2,
        wall_clock_seconds=100.0,
        model_calls=10,
        image_count=20,
    )
    other = dims.model_copy(update={"coverage": 0.7, "screenshot_readability": 0.8})
    result = adjudicate_segment(
        segment_id="syn-test",
        mode=ComparisonMode.VISUAL_DEDUPE,
        rater_a=RaterScores(rater_id="a", scores=dims),
        rater_b=RaterScores(rater_id="b", scores=other),
    )
    assert isinstance(result, AdjudicatedSegmentScore)
    assert result.merged.coverage == 0.75


@pytest.mark.parametrize("mode", list(ComparisonMode))
def test_comparison_modes_run_on_manifest_fixture(mode: ComparisonMode):
    manifest = load_manifest()
    segment = manifest.categories[0].segments[0]
    result = run_comparison_fixture(
        mode,
        ComparisonRunInput(
            segment_id=segment.id,
            duration_seconds=segment.duration_seconds,
            fixture_path=REPO / segment.fixture,
        ),
    )
    assert result.synthetic is True
    assert result.scores.wall_clock_seconds > 0
    assert mode in all_modes()


@pytest.mark.parametrize("timeline", STANDARD_TIMELINES)
def test_synthetic_timeline_performance_estimates(timeline):
    report = estimate_timeline_performance(timeline)
    assert report.total_wall_clock_seconds > 0
    assert report.total_model_calls > 0
    assert report.peak_memory_mb_estimate <= 512.0
    assert report.notes.startswith("Fixture linear model")

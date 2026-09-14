"""Fixture-backed comparison mode runners (CI-safe; no network)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evals.scoring import ComparisonMode, DimensionScores

# Fixture-derived baselines (synthetic; not measured on real courses).
_MODE_BASELINES: dict[ComparisonMode, dict[str, float]] = {
    ComparisonMode.EQUAL_INTERVAL: {
        "coverage": 0.62,
        "factual_error_rate": 0.08,
        "screenshot_readability": 0.70,
        "layout_quality": 0.75,
        "cost_factor": 0.6,
        "call_factor": 0.5,
        "image_factor": 1.4,
    },
    ComparisonMode.VISUAL_DEDUPE: {
        "coverage": 0.68,
        "factual_error_rate": 0.07,
        "screenshot_readability": 0.78,
        "layout_quality": 0.76,
        "cost_factor": 0.7,
        "call_factor": 0.55,
        "image_factor": 1.0,
    },
    ComparisonMode.TRANSCRIPT_ONLY: {
        "coverage": 0.55,
        "factual_error_rate": 0.12,
        "screenshot_readability": 0.40,
        "layout_quality": 0.72,
        "cost_factor": 0.35,
        "call_factor": 0.4,
        "image_factor": 0.0,
    },
    ComparisonMode.FRAMES_MULTIMODAL: {
        "coverage": 0.82,
        "factual_error_rate": 0.05,
        "screenshot_readability": 0.88,
        "layout_quality": 0.80,
        "cost_factor": 1.0,
        "call_factor": 1.0,
        "image_factor": 1.0,
    },
    ComparisonMode.HYBRID: {
        "coverage": 0.86,
        "factual_error_rate": 0.05,
        "screenshot_readability": 0.86,
        "layout_quality": 0.80,
        "cost_factor": 1.25,
        "call_factor": 1.1,
        "image_factor": 0.85,
    },
}


@dataclass(frozen=True)
class ComparisonRunInput:
    segment_id: str
    duration_seconds: float
    fixture_path: Path


@dataclass(frozen=True)
class ComparisonRunResult:
    segment_id: str
    mode: ComparisonMode
    scores: DimensionScores
    fixture_path: Path
    synthetic: bool = True


def run_comparison_fixture(
    mode: ComparisonMode,
    item: ComparisonRunInput,
    *,
    base_wall_clock_per_minute: float = 2.5,
) -> ComparisonRunResult:
    """Produce deterministic scores from manifest fixtures (not live model output)."""

    baseline = _MODE_BASELINES[mode]
    minutes = max(item.duration_seconds / 60.0, 0.1)
    wall_clock = base_wall_clock_per_minute * minutes * baseline["call_factor"]
    model_calls = max(1, int(minutes * 2 * baseline["call_factor"]))
    image_count = int(minutes * 4 * baseline["image_factor"])
    cost = 0.02 * minutes * baseline["cost_factor"] + 0.001 * image_count
    scores = DimensionScores(
        coverage=baseline["coverage"],
        factual_error_rate=baseline["factual_error_rate"],
        screenshot_readability=baseline["screenshot_readability"],
        layout_quality=baseline["layout_quality"],
        estimated_cost_usd=round(cost, 4),
        wall_clock_seconds=round(wall_clock, 3),
        model_calls=model_calls,
        image_count=image_count,
        notes="synthetic fixture projection; replace with live adjudication rows",
    )
    return ComparisonRunResult(
        segment_id=item.segment_id,
        mode=mode,
        scores=scores,
        fixture_path=item.fixture_path,
    )


def all_modes() -> tuple[ComparisonMode, ...]:
    return tuple(_MODE_BASELINES.keys())

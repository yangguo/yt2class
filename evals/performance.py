"""Synthetic timeline performance and budget accounting (no large media)."""

from __future__ import annotations

from dataclasses import dataclass

from yt2class.config import CourseConfig
from yt2class.orchestration.budget import BudgetLimits, RunBudget


@dataclass(frozen=True)
class SyntheticTimeline:
    """Stub duration for 10 / 30 / 90 minute courses without binary media."""

    label: str
    duration_minutes: int

    @property
    def duration_seconds(self) -> float:
        return float(self.duration_minutes * 60)


@dataclass(frozen=True)
class StageTimingEstimate:
    stage: str
    wall_clock_seconds: float
    model_calls: int
    is_estimate: bool = True


@dataclass(frozen=True)
class TimelinePerformanceReport:
    timeline: SyntheticTimeline
    stages: tuple[StageTimingEstimate, ...]
    total_wall_clock_seconds: float
    total_model_calls: int
    budget_headroom_usd: float
    notes: str

    @property
    def peak_memory_mb_estimate(self) -> float:
        # Fixture scaling: ~8 MB/min decode buffer cap in tests only.
        return min(512.0, 8.0 * self.timeline.duration_minutes)


STANDARD_TIMELINES: tuple[SyntheticTimeline, ...] = (
    SyntheticTimeline("short-lecture", 10),
    SyntheticTimeline("module", 30),
    SyntheticTimeline("full-course", 90),
)

# Per-minute fixture coefficients from offline CI runs (rounded); not realtime guarantees.
_SECONDS_PER_MINUTE_BY_STAGE: dict[str, float] = {
    "ingest": 0.15,
    "extract_evidence": 0.35,
    "analyze": 1.2,
    "editorial": 0.25,
    "bind_render": 0.4,
}
_CALLS_PER_MINUTE_ANALYZE = 1.8


def estimate_timeline_performance(
    timeline: SyntheticTimeline,
    config: CourseConfig | None = None,
) -> TimelinePerformanceReport:
    cfg = config or CourseConfig()
    limits = default_budget_limits_for_timeline(timeline)
    budget = RunBudget(limits=limits)
    stages: list[StageTimingEstimate] = []
    total_seconds = 0.0
    total_calls = 0
    for name, factor in _SECONDS_PER_MINUTE_BY_STAGE.items():
        seconds = factor * 60.0 * timeline.duration_minutes
        calls = 0
        if name == "analyze":
            calls = int(_CALLS_PER_MINUTE_ANALYZE * timeline.duration_minutes)
            budget.charge(model_calls=calls, estimated_usd=0.01 * calls)
        stages.append(StageTimingEstimate(stage=name, wall_clock_seconds=seconds, model_calls=calls))
        total_seconds += seconds
        total_calls += calls
    headroom = max(0.0, limits.max_estimated_usd - budget.consumed.estimated_usd)
    return TimelinePerformanceReport(
        timeline=timeline,
        stages=tuple(stages),
        total_wall_clock_seconds=total_seconds,
        total_model_calls=total_calls,
        budget_headroom_usd=headroom,
        notes=(
            "Fixture linear model for CI; measured wall-clock on real media will differ. "
            "Mark live benchmarks with date in docs/release-checklist.md."
        ),
    )


def default_budget_limits_for_timeline(timeline: SyntheticTimeline) -> BudgetLimits:
    minutes = timeline.duration_minutes
    return BudgetLimits(
        max_model_calls=max(50, minutes * 3),
        max_images=max(100, minutes * 8),
        max_video_seconds=float(minutes * 60),
        max_estimated_usd=max(2.0, minutes * 0.08),
    )

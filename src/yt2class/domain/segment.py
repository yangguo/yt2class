"""SegmentManifest: scheduled analysis windows and coverage ledger."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Identifier,
    PositiveSeconds,
    Seconds,
    StrictModel,
    unique_ids,
    validate_half_open,
)

WindowStatus = Literal["scheduled", "running", "complete", "degraded", "failed"]


class ImageBatch(StrictModel):
    """One provider image/token batch for a core window. Analysis is not page-capped."""

    id: Identifier
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)
    image_count: int = Field(default=0, ge=0)


class AnalysisWindow(StrictModel):
    id: Identifier
    core_start_seconds: Seconds
    core_end_seconds: Seconds
    context_start_seconds: Seconds
    context_end_seconds: Seconds
    evidence_ids: list[Identifier] = Field(default_factory=list)
    status: WindowStatus
    image_batches: list[ImageBatch] = Field(default_factory=list)
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)
    failure_reason: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def check_ranges(self) -> Self:
        validate_half_open(self.core_start_seconds, self.core_end_seconds, label="core window")
        validate_half_open(
            self.context_start_seconds, self.context_end_seconds, label="context window"
        )
        if self.context_start_seconds > self.core_start_seconds:
            raise ValueError("context start must be at or before core start")
        if self.context_end_seconds < self.core_end_seconds:
            raise ValueError("context end must be at or after core end")
        return self


class SegmentManifest(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    duration_seconds: PositiveSeconds
    windows: list[AnalysisWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_windows(self) -> Self:
        unique_ids(self.windows, label="analysis window")
        for window in self.windows:
            unique_ids(window.image_batches, label="image batch")
            if window.context_end_seconds > self.duration_seconds:
                raise ValueError("analysis window exceeds source duration")
        return self


def core_intervals(windows: list[AnalysisWindow]) -> list[tuple[float, float]]:
    return [
        (float(window.core_start_seconds), float(window.core_end_seconds))
        for window in windows
    ]


def cores_overlap(windows: list[AnalysisWindow], *, tol: float = 1e-9) -> bool:
    ordered = sorted(core_intervals(windows), key=lambda item: (item[0], item[1]))
    for index, (start, end) in enumerate(ordered[1:], start=1):
        previous_end = ordered[index - 1][1]
        if start < previous_end - tol:
            return True
    return False


def cores_cover_duration(
    windows: list[AnalysisWindow],
    duration: float,
    *,
    tol: float = 1e-9,
) -> bool:
    """True when core ranges are a partition of ``[0, duration)``."""

    if duration <= 0:
        return not windows
    if not windows or cores_overlap(windows, tol=tol):
        return False
    ordered = sorted(core_intervals(windows), key=lambda item: item[0])
    if abs(ordered[0][0] - 0.0) > tol:
        return False
    cursor = ordered[0][1]
    for start, end in ordered[1:]:
        if abs(start - cursor) > tol:
            return False
        cursor = end
    return abs(cursor - float(duration)) <= tol

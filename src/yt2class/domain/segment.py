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


class AnalysisWindow(StrictModel):
    id: Identifier
    core_start_seconds: Seconds
    core_end_seconds: Seconds
    context_start_seconds: Seconds
    context_end_seconds: Seconds
    evidence_ids: list[Identifier] = Field(default_factory=list)
    status: WindowStatus

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
            if window.context_end_seconds > self.duration_seconds:
                raise ValueError("analysis window exceeds source duration")
        return self

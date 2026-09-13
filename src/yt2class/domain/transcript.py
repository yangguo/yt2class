"""TranscriptDocument: normalized speech evidence from captions or ASR."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    Seconds,
    StrictModel,
    UnitInterval,
    covered_seconds,
    unique_ids,
    uncovered_half_open,
    validate_half_open,
)

TranscriptOrigin = Literal["sidecar", "manual-caption", "auto-caption", "asr"]
AlignmentStatus = Literal["aligned", "unaligned", "partial"]
AlignmentLevel = Literal["none", "sentence", "word"]
CoverageDenominator = Literal["vad-speech", "timeline"]
TranscriptStatus = Literal["complete", "degraded", "failed"]


class TranscriptWord(StrictModel):
    text: str = Field(min_length=1, max_length=200)
    start_seconds: Seconds
    end_seconds: Seconds
    aligned: bool = True

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="word")
        return self


class TranscriptSegment(StrictModel):
    id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds
    text_original: str = Field(min_length=1, max_length=8000)
    language: str = Field(min_length=2, max_length=35)
    origin: TranscriptOrigin
    raw_ref: Identifier | None = None
    speaker_id: Identifier | None = None
    words: list[TranscriptWord] | None = None
    alignment_status: AlignmentStatus = "unaligned"
    quality_flags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="transcript segment")
        return self


class SpeechCoverage(StrictModel):
    speech_seconds: Seconds
    covered_seconds: Seconds
    denominator: CoverageDenominator
    coverage_ratio: UnitInterval
    denominator_seconds: Seconds | None = None

    @model_validator(mode="after")
    def check_semantics(self) -> Self:
        if self.denominator == "vad-speech" and self.covered_seconds > self.speech_seconds:
            raise ValueError("covered seconds cannot exceed speech seconds")
        if self.denominator_seconds is not None:
            if self.covered_seconds > self.denominator_seconds:
                raise ValueError("covered seconds cannot exceed denominator seconds")
            expected = (
                0.0
                if self.denominator_seconds == 0
                else self.covered_seconds / self.denominator_seconds
            )
            if abs(self.coverage_ratio - expected) > 1e-6:
                raise ValueError("coverage ratio must equal covered/denominator seconds")
        elif self.denominator == "vad-speech":
            expected = 0.0 if self.speech_seconds == 0 else self.covered_seconds / self.speech_seconds
            if abs(self.coverage_ratio - expected) > 1e-6:
                raise ValueError("coverage ratio must equal covered/speech seconds")
        return self


class TranscriptGap(StrictModel):
    id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="transcript gap")
        return self


class TranscriptDocument(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    language: str = Field(min_length=2, max_length=35)
    raw_artifact_hash: Digest | None = None
    alignment: AlignmentLevel
    speech_coverage: SpeechCoverage
    segments: list[TranscriptSegment] = Field(default_factory=list)
    duration_seconds: Seconds | None = None
    status: TranscriptStatus = "complete"
    gaps: list[TranscriptGap] = Field(default_factory=list)
    audio_input_hash: Digest | None = None
    audio_parent_hash: Digest | None = None
    time_offset_seconds: Seconds = 0.0

    @model_validator(mode="after")
    def check_unique_segments(self) -> Self:
        unique_ids(self.segments, label="transcript segment")
        unique_ids(self.gaps, label="transcript gap")
        if self.segments and self.raw_artifact_hash is None:
            raise ValueError("raw_artifact_hash is required when transcript has segments")
        if self.status == "complete" and (not self.segments or self.gaps):
            raise ValueError("complete transcript requires segments and no coverage gaps")
        if (
            self.status == "complete"
            and self.speech_coverage.denominator == "timeline"
            and self.speech_coverage.denominator_seconds is not None
            and self.speech_coverage.coverage_ratio < 1.0 - 1e-6
        ):
            raise ValueError("complete timeline transcript cannot have uncovered duration")
        if self.duration_seconds is not None:
            for segment in self.segments:
                if segment.end_seconds > self.duration_seconds:
                    raise ValueError("transcript segment exceeds duration")
                for word in segment.words or []:
                    if word.end_seconds > self.duration_seconds:
                        raise ValueError("transcript word exceeds duration")
            for gap in self.gaps:
                if gap.end_seconds > self.duration_seconds:
                    raise ValueError("transcript gap exceeds duration")
        self._check_derived_coverage()
        return self

    def _explicit_duration(self) -> float | None:
        if self.duration_seconds is not None:
            return float(self.duration_seconds)
        if (
            self.speech_coverage.denominator == "timeline"
            and self.speech_coverage.denominator_seconds is not None
        ):
            return float(self.speech_coverage.denominator_seconds)
        return None

    def _check_derived_coverage(self) -> None:
        segment_intervals = [
            (segment.start_seconds, segment.end_seconds) for segment in self.segments
        ]
        gap_intervals = [(gap.start_seconds, gap.end_seconds) for gap in self.gaps]
        coverage = self.speech_coverage
        claims_complete = self.status == "complete" or (
            coverage.denominator == "timeline" and coverage.coverage_ratio >= 1.0 - 1e-6
        )
        duration = self._explicit_duration()
        if duration is not None:
            derived_covered = covered_seconds(duration, segment_intervals)
            if coverage.denominator == "timeline":
                if abs(coverage.covered_seconds - derived_covered) > 1e-6:
                    raise ValueError(
                        "declared transcript coverage does not match derived segment union"
                    )
                if coverage.denominator_seconds is not None and abs(
                    coverage.denominator_seconds - duration
                ) > 1e-6:
                    raise ValueError("timeline transcript denominator must match duration")
            leftover_closed = uncovered_half_open(
                duration, segment_intervals + gap_intervals
            )
            if leftover_closed:
                raise ValueError("transcript timeline is not closed; uncovered ranges remain")
            if claims_complete and uncovered_half_open(duration, segment_intervals):
                raise ValueError(
                    "complete/100% transcript coverage is not derived from segment unions"
                )
            return
        if claims_complete and segment_intervals:
            span = max(end for _start, end in segment_intervals)
            if uncovered_half_open(span, segment_intervals):
                raise ValueError(
                    "complete/100% transcript coverage is not derived from segment unions"
                )

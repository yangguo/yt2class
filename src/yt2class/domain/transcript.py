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
    unique_ids,
    validate_half_open,
)

TranscriptOrigin = Literal["sidecar", "manual-caption", "auto-caption", "asr"]
AlignmentStatus = Literal["aligned", "unaligned", "partial"]
AlignmentLevel = Literal["none", "sentence", "word"]
CoverageDenominator = Literal["vad-speech", "timeline"]


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


class TranscriptDocument(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    language: str = Field(min_length=2, max_length=35)
    raw_artifact_hash: Digest
    alignment: AlignmentLevel
    speech_coverage: SpeechCoverage
    segments: list[TranscriptSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_unique_segments(self) -> Self:
        unique_ids(self.segments, label="transcript segment")
        return self

"""EvidenceBundle: the M1 join of source, transcript, visual assets, and gaps."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    RelativePath,
    StrictModel,
    Seconds,
    unique_ids,
    validate_half_open,
)
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.domain.source import SourceManifest

EvidenceModality = Literal["transcript", "visual", "ocr", "asr"]
EvidenceStatus = Literal["complete", "degraded", "failed", "unavailable"]


class EvidenceGap(StrictModel):
    id: Identifier
    modality: EvidenceModality
    start_seconds: Seconds
    end_seconds: Seconds
    reason: str = Field(min_length=1, max_length=400)
    status: EvidenceStatus = "degraded"

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="evidence gap")
        return self


class EvidenceArtifact(StrictModel):
    id: Identifier
    kind: str = Field(min_length=1, max_length=64)
    path: RelativePath
    sha256: Digest


class EvidenceBundle(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    source_hash: Digest
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    source: SourceManifest
    transcript: TranscriptDocument
    visual: VisualCatalogue
    status: EvidenceStatus
    gaps: list[EvidenceGap] = Field(default_factory=list)
    artifacts: list[EvidenceArtifact] = Field(default_factory=list)
    transcript_path: RelativePath | None = None
    visual_path: RelativePath | None = None

    @model_validator(mode="after")
    def check_refs(self) -> Self:
        if self.transcript.source_id != self.source_id:
            raise ValueError("EvidenceBundle transcript source_id mismatch")
        if self.visual.source_id != self.source_id:
            raise ValueError("EvidenceBundle visual source_id mismatch")
        if self.source.source_id != self.source_id:
            raise ValueError("EvidenceBundle source_id does not match SourceManifest")
        if self.source.sha256 != self.source_hash:
            raise ValueError("EvidenceBundle source hash does not match SourceManifest")
        if self.source.duration_seconds != self.duration_seconds:
            raise ValueError("EvidenceBundle duration does not match SourceManifest")
        if (
            self.transcript.duration_seconds is not None
            and self.transcript.duration_seconds != self.duration_seconds
        ):
            raise ValueError("EvidenceBundle transcript duration must equal source duration")

        coverage = self.transcript.speech_coverage
        if coverage.speech_seconds > self.duration_seconds:
            raise ValueError("EvidenceBundle transcript speech coverage exceeds source duration")
        if coverage.covered_seconds > self.duration_seconds:
            raise ValueError("EvidenceBundle transcript covered duration exceeds source duration")
        if coverage.denominator_seconds is not None:
            if coverage.denominator_seconds > self.duration_seconds:
                raise ValueError("EvidenceBundle transcript denominator exceeds source duration")
            if (
                coverage.denominator == "timeline"
                and coverage.denominator_seconds != self.duration_seconds
            ):
                raise ValueError("EvidenceBundle timeline denominator must equal source duration")

        for segment in self.transcript.segments:
            self._check_range(segment.start_seconds, segment.end_seconds, "transcript segment")
            for word in segment.words or []:
                self._check_range(word.start_seconds, word.end_seconds, "transcript word")
                if word.start_seconds < segment.start_seconds or word.end_seconds > segment.end_seconds:
                    raise ValueError("EvidenceBundle transcript word exceeds segment")
        for gap in self.transcript.gaps:
            self._check_range(gap.start_seconds, gap.end_seconds, "transcript gap")

        scenes = {scene.id: scene for scene in self.visual.scenes}
        for scene in self.visual.scenes:
            self._check_range(scene.start_seconds, scene.end_seconds, "visual scene")
        for occurrence in self.visual.occurrences:
            self._check_point(occurrence.requested_seconds, "visual requested timestamp")
            if occurrence.timestamp_seconds is not None:
                self._check_point(occurrence.timestamp_seconds, "visual timestamp")
            if occurrence.actual_source_seconds is not None:
                self._check_point(occurrence.actual_source_seconds, "visual actual timestamp")
            scene = scenes[occurrence.scene_id]
            if (
                occurrence.timestamp_seconds is not None
                and not scene.start_seconds <= occurrence.timestamp_seconds < scene.end_seconds
            ):
                raise ValueError("EvidenceBundle visual timestamp is outside its scene")
            if (
                occurrence.actual_source_seconds is not None
                and not scene.start_seconds <= occurrence.actual_source_seconds < scene.end_seconds
            ):
                raise ValueError("EvidenceBundle visual actual timestamp is outside its scene")
        for gap in self.visual.gaps:
            self._check_range(gap.start_seconds, gap.end_seconds, "visual gap")

        for gap in self.gaps:
            self._check_range(gap.start_seconds, gap.end_seconds, "EvidenceBundle gap")

        artifacts = {artifact.path: artifact for artifact in self.artifacts}
        if self.transcript_path is None or artifacts.get(self.transcript_path, None) is None:
            raise ValueError("transcript_path must reference a transcript artifact")
        if artifacts[self.transcript_path].kind != "transcript-document":
            raise ValueError("transcript_path must reference a transcript artifact")
        if self.visual_path is None or artifacts.get(self.visual_path, None) is None:
            raise ValueError("visual_path must reference a visual artifact")
        if artifacts[self.visual_path].kind != "visual-catalogue":
            raise ValueError("visual_path must reference a visual artifact")

        if self.status == "complete":
            if self.gaps or self.transcript.gaps or self.visual.gaps:
                raise ValueError("complete EvidenceBundle cannot contain gaps")
            if self.transcript.status != "complete" or self.visual.status != "complete":
                raise ValueError("complete EvidenceBundle requires complete components")
        unique_ids(self.gaps, label="evidence gap")
        unique_ids(self.artifacts, label="evidence artifact")
        return self

    def _check_point(self, value: float, label: str) -> None:
        if not 0 <= value < self.duration_seconds:
            raise ValueError(f"{label} {value} exceeds source duration")

    def _check_range(self, start: float, end: float, label: str) -> None:
        if not 0 <= start < end <= self.duration_seconds:
            raise ValueError(f"{label} [{start}, {end}) exceeds source duration")


__all__ = [
    "EvidenceArtifact",
    "EvidenceBundle",
    "EvidenceGap",
    "EvidenceModality",
    "EvidenceStatus",
]

"""VisualCatalogue: scenes, frame occurrences, assets, and OCR regions."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    RelativePath,
    Seconds,
    StrictModel,
    UnitInterval,
    unique_ids,
    validate_half_open,
)

SceneDetector = Literal["content", "adaptive"]
VisualRole = Literal["frame", "clip"]


class BoundingBox(StrictModel):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)


class Scene(StrictModel):
    id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds
    detector: SceneDetector

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="scene")
        return self


class FrameQuality(StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    brightness: UnitInterval
    sharpness: UnitInterval
    ocr_density: UnitInterval


class VisualAsset(StrictModel):
    id: Identifier
    role: VisualRole
    path: RelativePath
    sha256: Digest
    mime_type: str = Field(min_length=1, max_length=64)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


class FrameOccurrence(StrictModel):
    id: Identifier
    scene_id: Identifier
    asset_id: Identifier
    requested_seconds: Seconds
    timestamp_seconds: Seconds
    cluster_id: Identifier | None = None
    reject_reason: str | None = Field(default=None, max_length=200)
    quality: FrameQuality


class OcrRegion(StrictModel):
    id: Identifier
    asset_id: Identifier
    parent_occurrence_id: Identifier
    bbox: BoundingBox
    text: str = Field(min_length=1, max_length=4000)
    engine: str = Field(min_length=1, max_length=64)
    confidence: UnitInterval


class VisualCatalogue(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    scenes: list[Scene] = Field(default_factory=list)
    assets: list[VisualAsset] = Field(default_factory=list)
    occurrences: list[FrameOccurrence] = Field(default_factory=list)
    ocr_regions: list[OcrRegion] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_unique_and_refs(self) -> Self:
        scenes = unique_ids(self.scenes, label="scene")
        assets = unique_ids(self.assets, label="visual asset")
        occurrences = unique_ids(self.occurrences, label="occurrence")
        unique_ids(self.ocr_regions, label="ocr region")
        for occurrence in self.occurrences:
            if occurrence.scene_id not in scenes:
                raise ValueError(f"unknown occurrence scene_id {occurrence.scene_id!r}")
            if occurrence.asset_id not in assets:
                raise ValueError(f"unknown occurrence asset_id {occurrence.asset_id!r}")
        for region in self.ocr_regions:
            if region.asset_id not in assets:
                raise ValueError(f"unknown ocr asset_id {region.asset_id!r}")
            if region.parent_occurrence_id not in occurrences:
                raise ValueError(f"unknown ocr parent_occurrence_id {region.parent_occurrence_id!r}")
        return self

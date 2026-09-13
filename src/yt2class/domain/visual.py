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
VisualStatus = Literal["complete", "degraded", "failed"]


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
    timestamp_seconds: Seconds | None = None
    actual_source_seconds: Seconds | None = None
    cluster_id: Identifier | None = None
    reject_reason: str | None = Field(default=None, max_length=200)
    quality: FrameQuality
    quality_flags: list[str] = Field(default_factory=list, max_length=20)

    @property
    def actual_timestamp_seconds(self) -> float | None:
        """Compatibility/readability alias for the decoded source timestamp."""

        return self.actual_source_seconds if self.actual_source_seconds is not None else self.timestamp_seconds


class VisualGap(StrictModel):
    id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="visual gap")
        return self


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
    profile: SceneDetector | None = None
    status: VisualStatus = "complete"
    gaps: list[VisualGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_unique_and_refs(self) -> Self:
        scenes = unique_ids(self.scenes, label="scene")
        assets = unique_ids(self.assets, label="visual asset")
        occurrences = unique_ids(self.occurrences, label="occurrence")
        unique_ids(self.ocr_regions, label="ocr region")
        unique_ids(self.gaps, label="visual gap")
        for occurrence in self.occurrences:
            if occurrence.scene_id not in scenes:
                raise ValueError(f"unknown occurrence scene_id {occurrence.scene_id!r}")
            if occurrence.asset_id not in assets:
                raise ValueError(f"unknown occurrence asset_id {occurrence.asset_id!r}")
            if assets[occurrence.asset_id].role != "frame":
                raise ValueError(
                    f"occurrence {occurrence.id} requires a frame asset, got "
                    f"role {assets[occurrence.asset_id].role!r}"
                )
        for region in self.ocr_regions:
            if region.asset_id not in assets:
                raise ValueError(f"unknown ocr asset_id {region.asset_id!r}")
            if region.parent_occurrence_id not in occurrences:
                raise ValueError(f"unknown ocr parent_occurrence_id {region.parent_occurrence_id!r}")
            occurrence = occurrences[region.parent_occurrence_id]
            if region.asset_id != occurrence.asset_id:
                raise ValueError(
                    f"ocr region {region.id} asset_id must match its parent occurrence asset_id"
                )
            asset = assets[region.asset_id]
            if asset.width is None or asset.height is None:
                raise ValueError(
                    f"ocr region {region.id} requires asset dimensions for bbox validation"
                )
            if region.bbox.x + region.bbox.width > asset.width:
                raise ValueError(f"ocr region {region.id} bbox exceeds asset width")
            if region.bbox.y + region.bbox.height > asset.height:
                raise ValueError(f"ocr region {region.id} bbox exceeds asset height")
        return self

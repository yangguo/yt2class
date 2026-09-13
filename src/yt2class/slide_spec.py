"""Versioned v2 interchange contract; not yet wired into the v1 build pipeline.

JSON Schema checks structure. Pydantic additionally checks reference integrity;
validate_assets checks file containment and hashes immediately before rendering.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yt2class.inputs import validate_youtube_url
from yt2class.scenes import file_sha256

Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
# Portable paths relative to run root; runtime also checks resolved symlinks.
RelativePath = Annotated[str, Field(min_length=1, pattern=r"^(?!/)(?!.*\\)(?!.*:)(?!.*(?:^|/)\.\.(?:/|$)).+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re")


class Source(StrictModel):
    kind: Literal["youtube", "local"]
    title: str = Field(min_length=1, max_length=160)
    media_path: RelativePath
    sha256: Digest
    duration_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    url: str | None = None

    @model_validator(mode="after")
    def check_url(self) -> Self:
        if self.kind == "youtube":
            if self.url is None:
                raise ValueError("youtube source requires url")
            validate_youtube_url(self.url)
        elif self.url is not None:
            raise ValueError("local source must not supply a remote url")
        return self


class Asset(StrictModel):
    id: Identifier
    path: RelativePath
    sha256: Digest
    timestamp_seconds: Seconds


class Evidence(StrictModel):
    id: Identifier
    kind: Literal["frame", "transcript"]
    start_seconds: Seconds
    end_seconds: Seconds
    asset_id: Identifier | None = None
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    origin: Literal["manual-caption", "auto-caption", "asr"] | None = None

    @model_validator(mode="after")
    def check_kind(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("evidence range is reversed")
        if self.kind == "frame":
            if self.asset_id is None or self.text is not None or self.origin is not None:
                raise ValueError("frame evidence requires only asset_id")
        elif self.text is None or self.origin is None or self.asset_id is not None:
            raise ValueError("transcript evidence requires text and origin, no asset_id")
        return self


class GroundedText(StrictModel):
    text: str = Field(min_length=1, max_length=240)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=20)


class LearningSlide(StrictModel):
    id: Identifier
    kind: Literal["grammar", "example", "table", "confusion", "other"]
    title: str = Field(min_length=1, max_length=80)
    asset_id: Identifier
    points: list[GroundedText] = Field(min_length=1, max_length=4)


class Quiz(StrictModel):
    prompt: str = Field(min_length=1, max_length=240)
    answer: GroundedText


class SlideSpec(StrictModel):
    schema_version: Literal["2.0"]
    title: str = Field(min_length=1, max_length=160)
    language: str = Field(min_length=2, max_length=35)
    source: Source
    assets: list[Asset] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    slides: list[LearningSlide] = Field(min_length=1, max_length=30)
    summary: list[GroundedText] = Field(max_length=8)
    quiz: list[Quiz] = Field(max_length=8)
    analysis_mode: Literal["model", "reviewed", "offline", "offline-fallback"]

    @model_validator(mode="after")
    def check_references(self) -> Self:
        for label, items in (("asset", self.assets), ("evidence", self.evidence), ("slide", self.slides)):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} id")
        assets = {item.id: item for item in self.assets}
        evidence = {item.id: item for item in self.evidence}
        for asset in self.assets:
            if asset.timestamp_seconds > self.source.duration_seconds:
                raise ValueError("asset timestamp exceeds source duration")
        for item in self.evidence:
            if item.end_seconds > self.source.duration_seconds:
                raise ValueError("evidence exceeds source duration")
            if item.kind == "frame":
                asset = assets.get(item.asset_id)
                if asset is None:
                    raise ValueError("unknown evidence asset")
                if not item.start_seconds <= asset.timestamp_seconds <= item.end_seconds:
                    raise ValueError("frame timestamp outside evidence range")
        timestamps = []
        claims = list(self.summary) + [item.answer for item in self.quiz]
        for slide in self.slides:
            if slide.asset_id not in assets:
                raise ValueError("unknown slide asset")
            timestamps.append(assets[slide.asset_id].timestamp_seconds)
            claims.extend(slide.points)
            cited = {ref for point in slide.points for ref in point.evidence_ids}
            if not any(evidence[ref].asset_id == slide.asset_id for ref in cited if ref in evidence):
                raise ValueError("slide must cite its displayed frame evidence")
        if timestamps != sorted(timestamps):
            raise ValueError("learning slides must follow source time order")
        for claim in claims:
            if not set(claim.evidence_ids).issubset(evidence):
                raise ValueError("unknown claim evidence")
        return self


def validate_assets(spec: SlideSpec, run_root: Path) -> None:
    """Check trusted binding output, not semantic truth or image decodability."""
    root = run_root.resolve()
    files = [(spec.source.media_path, spec.source.sha256)]
    files.extend((asset.path, asset.sha256) for asset in spec.assets)
    for relative, expected in files:
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"asset outside run root: {relative}")
        if not path.is_file():
            raise ValueError(f"asset missing: {relative}")
        if file_sha256(path) != expected:
            raise ValueError(f"asset hash mismatch: {relative}")

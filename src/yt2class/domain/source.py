"""SourceManifest: immutable media identity produced by ingestion."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    PositiveSeconds,
    RelativePath,
    Seconds,
    StrictModel,
    unique_ids,
)

SourceKind = Literal["youtube", "local"]
LocalMode = Literal["copy", "reference"]
StreamType = Literal["video", "audio", "subtitle"]
TransformKind = Literal["copy", "proxy", "clip", "audio-extract"]


class MediaStream(StrictModel):
    index: int = Field(ge=0)
    codec_type: StreamType
    codec_name: str = Field(min_length=1, max_length=64)
    language: str | None = Field(default=None, min_length=2, max_length=35)


class Transformation(StrictModel):
    kind: TransformKind
    parent_hash: Digest | None = None
    command_digest: Digest | None = None
    time_offset_seconds: Seconds = 0.0
    speed_ratio: float = Field(default=1.0, gt=0, allow_inf_nan=False)


class SourceManifest(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    kind: SourceKind
    title: str = Field(min_length=1, max_length=160)
    media_path: RelativePath
    sha256: Digest
    duration_seconds: PositiveSeconds
    url: str | None = None
    video_id: str | None = Field(default=None, min_length=1, max_length=32)
    streams: list[MediaStream] = Field(min_length=1)
    timebase: str = Field(min_length=1, max_length=32)
    local_mode: LocalMode = "copy"
    transformations: list[Transformation] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_source_identity(self) -> Self:
        unique_ids((stream for stream in self.streams), attr="index", label="stream index")
        if self.kind == "youtube":
            if self.url is None or self.video_id is None:
                raise ValueError("youtube source requires url and video_id")
        elif self.url is not None or self.video_id is not None:
            raise ValueError("local source must not supply a remote url or video_id")
        if self.kind == "local" and self.local_mode == "reference":
            # Documented only; M1 workspace tests enforce immutability of the original file.
            pass
        return self

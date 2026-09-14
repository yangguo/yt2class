"""Auditable record of remote media uploads for native / hybrid analysis."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Digest, Identifier, Seconds, StrictModel, validate_half_open
from yt2class.domain.slide_spec_v3 import AnalysisMode

RemoteRetention = Literal["none", "ephemeral", "unknown"]

UploadState = Literal[
    "planned",
    "uploaded",
    "ready",
    "analyzed",
    "deleted",
    "delete_failed",
    "retained",
    "skipped",
    "failed",
]


class MediaClipRange(StrictModel):
    start_seconds: Seconds
    end_seconds: Seconds
    segment_id: Identifier | None = None

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="media clip range")
        return self


class RedactedRemoteHandle(StrictModel):
    """Provider-side object id safe to persist (no secrets or signed URLs)."""

    provider: str = Field(min_length=1, max_length=80)
    handle_digest: Digest
    state: UploadState


class MediaUploadRecord(StrictModel):
    upload_id: Identifier
    local_sha256: Digest
    mime_type: str = Field(min_length=3, max_length=80)
    byte_length: int = Field(ge=0)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    source_range: MediaClipRange
    remote: RedactedRemoteHandle | None = None
    retention_policy: RemoteRetention = "none"
    state: UploadState
    usage_input_tokens: int = Field(default=0, ge=0)
    usage_output_tokens: int = Field(default=0, ge=0)
    usage_video_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    note: str = Field(default="", max_length=400)


class ProviderProbeRecord(StrictModel):
    provider: str = Field(min_length=1, max_length=80)
    doc_version: str = Field(min_length=1, max_length=80)
    supports_video: bool
    supports_images: bool
    max_video_seconds: float = Field(ge=0, allow_inf_nan=False)
    remote_retention: RemoteRetention
    can_delete_remote: bool


class MediaPrivacyAudit(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    analysis_mode: AnalysisMode
    media_uploaded: bool
    uploads: list[MediaUploadRecord] = Field(default_factory=list)
    provider_probe: ProviderProbeRecord | None = None
    budget_frames_remaining: int | None = Field(default=None, ge=0)
    budget_clip_seconds_remaining: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    summary: str = Field(default="", max_length=800)

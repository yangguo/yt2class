"""RunManifest: orchestrator stage ledger, versions, and usage."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Identifier, StrictModel, unique_ids
from yt2class.domain.slide_spec_v3 import AnalysisMode, QualityStatus

StageName = Literal[
    "ingest",
    "extract_evidence",
    "outline",
    "analyze_segments",
    "reduce_knowledge",
    "edit_deck",
    "verify_claims",
    "bind_spec",
    "render",
]
StageStatus = Literal["pending", "running", "complete", "degraded", "failed", "cancelled"]


class StageRecord(StrictModel):
    name: StageName
    status: StageStatus
    cache_key: str | None = Field(default=None, max_length=128)
    error: str | None = Field(default=None, max_length=400)


class ToolVersions(StrictModel):
    producer_version: str = Field(min_length=1, max_length=80)
    yt_dlp: str | None = Field(default=None, max_length=80)
    ffmpeg: str | None = Field(default=None, max_length=80)
    provider: str | None = Field(default=None, max_length=80)
    prompt: str | None = Field(default=None, max_length=80)


class RunUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    image_count: int = Field(ge=0)
    video_seconds: float = Field(ge=0, allow_inf_nan=False)
    estimated_usd: float = Field(ge=0, allow_inf_nan=False)


class RunManifest(StrictModel):
    schema_version: Literal["1.0"]
    run_id: Identifier
    source_id: Identifier
    analysis_mode: AnalysisMode
    quality_status: QualityStatus
    stages: list[StageRecord] = Field(min_length=1)
    tool_versions: ToolVersions
    usage: RunUsage
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_stages(self) -> Self:
        unique_ids(self.stages, attr="name", label="stage")
        return self

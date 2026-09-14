"""Course/run configuration with CLI overrides and desensitized snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from yt2class.domain.verification import QualityMode
from yt2class.orchestration.budget import BudgetLimits
from yt2class.orchestration.cache import canonical_json, digest_parts


class BudgetConfig(BaseModel):
    max_model_calls: int = 100
    max_input_tokens: int = 2_000_000
    max_output_tokens: int = 500_000
    max_images: int = 500
    max_video_seconds: float = 3600.0
    max_estimated_usd: float = 5.0

    def to_limits(self) -> BudgetLimits:
        return BudgetLimits(
            max_model_calls=self.max_model_calls,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_images=self.max_images,
            max_video_seconds=self.max_video_seconds,
            max_estimated_usd=self.max_estimated_usd,
        )

AnalysisMode = Literal["frames", "native-video", "hybrid"]
PreviewPolicy = Literal["off", "optional", "required"]


class AnalysisConfig(BaseModel):
    mode: AnalysisMode = "frames"
    model_profile: str = "vision-primary"
    model: str | None = None
    segment_seconds: float = 120.0
    overlap_seconds: float = 10.0
    max_images_per_batch: int = 8
    max_evidence_rounds: int = 2
    provider: str = "fake"
    openrouter_json_mode: Literal["auto", "on", "off"] = "auto"


class EditorConfig(BaseModel):
    target_pages: int = 12
    max_pages: int = 20
    order: Literal["chronological", "topic"] = "chronological"
    quiz: Literal["optional", "off", "on"] = "optional"


class QualityConfig(BaseModel):
    mode: QualityMode = "draft"
    external_knowledge: bool = False


class RenderConfig(BaseModel):
    backend: Literal["pptxgenjs"] = "pptxgenjs"
    preview: PreviewPolicy = "optional"


class CourseConfig(BaseModel):
    design_version: str = "1.0"
    source_language: str = "auto"
    output_language: str = "zh-CN"
    audience: str = "beginner"
    domain: str = "general"
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    editor: EditorConfig = Field(default_factory=EditorConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)

    def config_digest(self) -> str:
        payload = self.model_dump(mode="json")
        return digest_parts(canonical_json(payload))

    def desensitized(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("secrets", None)
        return data


@dataclass(frozen=True)
class BuildSource:
    url: str | None = None
    video: Path | None = None
    subtitles: Path | None = None
    run_id: str | None = None
    source_id: str | None = None


def load_config(path: Path | None) -> CourseConfig:
    if path is None:
        return CourseConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return CourseConfig.model_validate(raw)


def write_desensitized_snapshot(config: CourseConfig, run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    target = run_root / "config.snapshot.json"
    target.write_text(
        json.dumps(config.desensitized(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "AnalysisConfig",
    "BudgetConfig",
    "BuildSource",
    "CourseConfig",
    "EditorConfig",
    "QualityConfig",
    "RenderConfig",
    "load_config",
    "write_desensitized_snapshot",
]

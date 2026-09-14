"""Load and validate the annotated evaluation manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifest.yaml"


class AuthorizationBlock(BaseModel):
    policy: str
    how_to_add_real_media: str


class SegmentSlot(BaseModel):
    id: str
    duration_seconds: float = Field(gt=0)
    fixture: str
    media_slot: Literal["uncommitted", "local-only"] = "uncommitted"
    notes: str | None = None
    hybrid_relevant: bool = False

    @field_validator("fixture")
    @classmethod
    def fixture_path_exists_or_placeholder(cls, value: str) -> str:
        path = REPO_ROOT / value
        if not path.is_file():
            raise ValueError(f"fixture missing: {value}")
        return value


class CategoryBlock(BaseModel):
    id: str
    label: str
    segments: list[SegmentSlot] = Field(min_length=1)


class LongCourseSlot(BaseModel):
    id: str
    duration_seconds: float = Field(gt=0)
    target_minutes: int | None = None
    fixture: str
    media_slot: Literal["uncommitted", "local-only"] = "uncommitted"
    notes: str | None = None

    @field_validator("fixture")
    @classmethod
    def fixture_path_exists(cls, value: str) -> str:
        path = REPO_ROOT / value
        if not path.is_file():
            raise ValueError(f"fixture missing: {value}")
        return value


class LiveRow(BaseModel):
    id: str
    marker: str
    description: str


class EvalManifest(BaseModel):
    version: str
    design_reference: str
    authorization: AuthorizationBlock
    comparison_modes: list[str] = Field(min_length=5)
    categories: list[CategoryBlock] = Field(min_length=5)
    long_course: list[LongCourseSlot] = Field(min_length=1)
    live_rows: list[LiveRow] = Field(default_factory=list)

    @property
    def short_segment_count(self) -> int:
        return sum(len(category.segments) for category in self.categories)

    def segment_ids(self) -> list[str]:
        ids = [segment.id for category in self.categories for segment in category.segments]
        ids.extend(slot.id for slot in self.long_course)
        return ids


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - dev dependency
        raise RuntimeError("PyYAML required to load eval manifest (dev dependency)") from error
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"manifest root must be a mapping: {path}")
    return data


def load_manifest(path: Path | None = None) -> EvalManifest:
    manifest_path = path or DEFAULT_MANIFEST
    return EvalManifest.model_validate(_load_yaml(manifest_path))

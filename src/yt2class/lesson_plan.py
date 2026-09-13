"""Strict, source-frame-bound lesson plan models."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PlanValidationError(ValueError):
    """Raised when model output cannot be bound to source evidence."""


SlideKind = Literal["grammar", "example", "table", "confusion", "other"]


class QuizItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=240)
    answer: str = Field(min_length=1, max_length=120)


class SlidePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(min_length=1, max_length=100)
    kind: SlideKind = "other"
    title: str = Field(min_length=1, max_length=120)
    explanation_zh: str = Field(min_length=1, max_length=600)
    takeaway: str | None = Field(default=None, max_length=240)


class LessonPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=240)
    slides: list[SlidePlan] = Field(min_length=1, max_length=30)
    summary: list[str] = Field(min_length=1, max_length=8)
    quiz: list[QuizItem] = Field(min_length=1, max_length=8)


_FORBIDDEN_SOURCE_FIELDS = {"image", "image_url", "image_path", "path", "asset_path"}


def validate_plan(
    payload: Mapping[str, object], known_frame_ids: Collection[str]
) -> LessonPlan:
    """Validate model JSON and ensure every teaching slide cites a known frame."""

    if not isinstance(payload, Mapping):
        raise PlanValidationError("lesson plan must be a JSON object")
    slides = payload.get("slides")
    if not isinstance(slides, list) or not slides:
        raise PlanValidationError("lesson plan must contain at least one slide")
    known = set(known_frame_ids)
    for slide in slides:
        if not isinstance(slide, Mapping):
            raise PlanValidationError("each slide must be a JSON object")
        frame_id = slide.get("frame_id")
        if frame_id not in known:
            raise PlanValidationError(f"unknown frame: {frame_id}")
        forbidden = _FORBIDDEN_SOURCE_FIELDS.intersection(slide)
        if forbidden:
            fields = ", ".join(sorted(forbidden))
            raise PlanValidationError(f"model cannot provide image assets: {fields}")
    try:
        return LessonPlan.model_validate(payload)
    except ValidationError as error:
        raise PlanValidationError(f"invalid lesson plan: {error}") from error


def plan_to_dict(plan: LessonPlan) -> dict[str, object]:
    return plan.model_dump(mode="json")

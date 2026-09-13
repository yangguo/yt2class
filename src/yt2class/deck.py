"""JSON-serializable deck specification helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yt2class.lesson_plan import LessonPlan
from yt2class.scenes import FrameCandidate


class DeckSpecError(ValueError):
    """Raised when a lesson plan cannot be bound to candidate frame files."""


def build_deck_spec(
    source_url: str,
    plan: LessonPlan,
    candidates: Sequence[FrameCandidate],
    *,
    source_video: Path | None = None,
) -> dict[str, object]:
    """Join validated text with immutable frame paths and timestamps."""

    by_id = {candidate.frame_id: candidate for candidate in candidates}
    slides: list[dict[str, object]] = []
    for selected in plan.slides:
        frame = by_id.get(selected.frame_id)
        if frame is None:
            raise DeckSpecError(f"selected frame is unavailable: {selected.frame_id}")
        slides.append(
            {
                "frame_id": frame.frame_id,
                "timestamp": frame.timestamp,
                "frame_path": str(frame.path),
                "source_sha256": frame.source_sha256,
                "kind": selected.kind,
                "title": selected.title,
                "explanation_zh": selected.explanation_zh,
                "takeaway": selected.takeaway,
            }
        )
    return {
        "source_url": source_url,
        "source_video": str(source_video) if source_video else None,
        "title": plan.title,
        "subtitle": plan.subtitle,
        "slides": slides,
        "summary": list(plan.summary),
        "quiz": [item.model_dump(mode="json") for item in plan.quiz],
    }

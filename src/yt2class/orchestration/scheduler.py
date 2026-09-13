"""Segment scheduler: tile core windows, overlap context, and honor payload budgets."""

from __future__ import annotations

from dataclasses import dataclass
import re
from threading import Event

from yt2class.adapters.providers.base import ProviderCapabilities, RequestCancelled
from yt2class.domain.course_map import CourseMap
from yt2class.domain.segment import (
    AnalysisWindow,
    ImageBatch,
    SegmentManifest,
    cores_cover_duration,
)
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.stages.llm_util import (
    OUTPUT_RESERVE_TOKENS,
    evidence_in_range,
    frames_in_range,
)

DEFAULT_CORE_SECONDS = 120.0
DEFAULT_MAX_CORE_SECONDS = 180.0
DEFAULT_CONTEXT_SECONDS = 10.0
DEFAULT_IMAGE_BATCH = 8
MIN_CORE_SECONDS = 1.0
SENTENCE_END = re.compile(r"[.!?。！？][\"'”’)\]]*\s*$")


@dataclass(frozen=True)
class SchedulerConfig:
    core_seconds: float = DEFAULT_CORE_SECONDS
    max_core_seconds: float = DEFAULT_MAX_CORE_SECONDS
    context_seconds: float = DEFAULT_CONTEXT_SECONDS
    image_batch_size: int | None = None
    output_reserve_tokens: int = OUTPUT_RESERVE_TOKENS
    min_core_seconds: float = MIN_CORE_SECONDS


class SchedulerError(ValueError):
    """Raised when a timeline cannot be scheduled without a page-budget cheat."""


def sentence_boundaries(transcript: TranscriptDocument) -> list[float]:
    return sorted(
        {
            float(segment.end_seconds)
            for segment in transcript.segments
            if SENTENCE_END.search(segment.text_original)
        }
    )


def _choose_cut(
    cursor: float,
    duration: float,
    boundaries: list[float],
    *,
    core_seconds: float,
    max_core_seconds: float,
) -> float:
    target = min(duration, cursor + core_seconds)
    limit = min(duration, cursor + max_core_seconds)
    if abs(limit - duration) < 1e-9 or target >= duration:
        return duration
    candidates = [stamp for stamp in boundaries if cursor + 1e-6 < stamp <= limit + 1e-9]
    if not candidates:
        return limit
    preferred = [stamp for stamp in candidates if stamp <= target + 1e-9]
    if preferred:
        return max(preferred)
    later = [stamp for stamp in candidates if stamp > target]
    return later[0] if later else limit


def _scheduling_course_map(course_map: CourseMap | None, source_id: str) -> CourseMap:
    if course_map is not None:
        return course_map
    return CourseMap(schema_version="1.0", source_id=source_id, topics=[])


def _estimate_dispatch_tokens(
    window: AnalysisWindow,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    frame_ids: list[str],
    image_count: int,
) -> int:
    # Imported lazily: analyze_segments imports set_window_status from this module.
    from yt2class.stages.analyze_segments import estimate_segment_request_tokens

    return estimate_segment_request_tokens(
        window,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        batch_evidence_ids=frame_ids,
        image_count=image_count,
    )


def _batch_frames(
    frames: list[dict[str, object]],
    *,
    window: AnalysisWindow,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    max_images: int,
    output_tokens: int,
) -> tuple[list[ImageBatch], int]:
    if max_images <= 0 or not frames:
        tokens = _estimate_dispatch_tokens(
            window,
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            frame_ids=[],
            image_count=0,
        )
        batch = ImageBatch(
            id=f"{window.id}-batch-01",
            evidence_ids=[],
            estimated_input_tokens=tokens,
            estimated_output_tokens=output_tokens,
            image_count=0,
        )
        return [batch], batch.estimated_input_tokens

    batches: list[ImageBatch] = []
    total_tokens = 0
    for index in range(0, len(frames), max_images):
        chunk = frames[index : index + max_images]
        frame_ids = [str(frame["id"]) for frame in chunk]
        image_count = len(chunk)
        tokens = _estimate_dispatch_tokens(
            window,
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            frame_ids=frame_ids,
            image_count=image_count,
        )
        batches.append(
            ImageBatch(
                id=f"{window.id}-batch-{len(batches) + 1:02d}",
                evidence_ids=frame_ids,
                estimated_input_tokens=tokens,
                estimated_output_tokens=output_tokens,
                image_count=image_count,
            )
        )
        total_tokens += tokens
    return batches, total_tokens


def _window_fits_budget(
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    image_count: int,
    capabilities: ProviderCapabilities,
) -> bool:
    return (
        estimated_input_tokens <= capabilities.max_input_tokens
        and estimated_output_tokens <= capabilities.max_output_tokens
        and image_count <= capabilities.max_images
    )


def rebuild_window_batches(
    window: AnalysisWindow,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    capabilities: ProviderCapabilities,
    config: SchedulerConfig | None = None,
    course_map: CourseMap | None = None,
    source_id: str = "src-unknown",
) -> AnalysisWindow:
    """Refresh image batches from the current catalogue so refined frames are visible."""

    cfg = config or SchedulerConfig()
    max_images = (
        cfg.image_batch_size
        if cfg.image_batch_size is not None
        else min(DEFAULT_IMAGE_BATCH, capabilities.max_images)
    )
    if capabilities.max_images == 0:
        max_images = 0
    output_tokens = min(cfg.output_reserve_tokens, capabilities.max_output_tokens)
    frames = frames_in_range(visual, window.context_start_seconds, window.context_end_seconds)
    active_map = _scheduling_course_map(course_map, source_id)
    batches, token_total = _batch_frames(
        frames if max_images else [],
        window=window,
        transcript=transcript,
        visual=visual,
        course_map=active_map,
        max_images=max_images if max_images > 0 else 0,
        output_tokens=output_tokens,
    )
    return window.model_copy(
        update={
            "image_batches": batches,
            "evidence_ids": evidence_in_range(
                transcript, visual, window.context_start_seconds, window.context_end_seconds
            ),
            "estimated_input_tokens": token_total,
            "estimated_output_tokens": output_tokens,
        }
    )


def _build_window(
    *,
    window_id: str,
    core_start: float,
    core_end: float,
    duration: float,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    capabilities: ProviderCapabilities,
    cfg: SchedulerConfig,
    max_images: int,
    status: str,
    failure_reason: str | None,
    course_map: CourseMap,
) -> AnalysisWindow:
    context_start = max(0.0, core_start - cfg.context_seconds)
    context_end = min(duration, core_end + cfg.context_seconds)
    output_tokens = min(cfg.output_reserve_tokens, capabilities.max_output_tokens)
    context_frames = frames_in_range(visual, context_start, context_end)
    probe = AnalysisWindow(
        id=window_id,
        core_start_seconds=core_start,
        core_end_seconds=core_end,
        context_start_seconds=context_start,
        context_end_seconds=context_end,
        evidence_ids=evidence_in_range(transcript, visual, context_start, context_end),
        status=status,
        image_batches=[],
        estimated_input_tokens=0,
        estimated_output_tokens=output_tokens,
        failure_reason=failure_reason,
    )
    batches, token_total = _batch_frames(
        context_frames if max_images else [],
        window=probe,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        max_images=max_images if max_images > 0 else 0,
        output_tokens=output_tokens,
    )
    return probe.model_copy(
        update={"image_batches": batches, "estimated_input_tokens": token_total}
    )


def _fits(
    window: AnalysisWindow,
    capabilities: ProviderCapabilities,
) -> bool:
    heaviest = max((batch.image_count for batch in window.image_batches), default=0)
    heaviest_tokens = max((batch.estimated_input_tokens for batch in window.image_batches), default=0)
    return _window_fits_budget(
        heaviest_tokens,
        window.estimated_output_tokens,
        heaviest,
        capabilities,
    )


def _split_until_fit(
    core_start: float,
    core_end: float,
    *,
    duration: float,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    capabilities: ProviderCapabilities,
    cfg: SchedulerConfig,
    max_images: int,
    index_start: int,
    boundaries: list[float],
    course_map: CourseMap,
) -> list[AnalysisWindow]:
    window_id = f"seg-{index_start:04d}"
    candidate = _build_window(
        window_id=window_id,
        core_start=core_start,
        core_end=core_end,
        duration=duration,
        transcript=transcript,
        visual=visual,
        capabilities=capabilities,
        cfg=cfg,
        max_images=max_images,
        status="scheduled",
        failure_reason=None,
        course_map=course_map,
    )
    if _fits(candidate, capabilities):
        return [candidate]
    if core_end - core_start <= cfg.min_core_seconds + 1e-9:
        return [
            candidate.model_copy(
                update={
                    "status": "failed",
                    "failure_reason": "unschedulable: exceeds provider budget",
                }
            )
        ]
    mid = core_start + (core_end - core_start) / 2.0
    near = [
        stamp
        for stamp in boundaries
        if core_start + cfg.min_core_seconds <= stamp <= core_end - cfg.min_core_seconds
    ]
    if near:
        mid = min(near, key=lambda stamp: abs(stamp - mid))
    left = _split_until_fit(
        core_start,
        mid,
        duration=duration,
        transcript=transcript,
        visual=visual,
        capabilities=capabilities,
        cfg=cfg,
        max_images=max_images,
        index_start=index_start,
        boundaries=boundaries,
        course_map=course_map,
    )
    right = _split_until_fit(
        mid,
        core_end,
        duration=duration,
        transcript=transcript,
        visual=visual,
        capabilities=capabilities,
        cfg=cfg,
        max_images=max_images,
        index_start=index_start + len(left),
        boundaries=boundaries,
        course_map=course_map,
    )
    return left + right


def schedule_windows(
    *,
    source_id: str,
    duration_seconds: float,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    capabilities: ProviderCapabilities,
    config: SchedulerConfig | None = None,
    cancel_event: Event | None = None,
    course_map: CourseMap | None = None,
) -> SegmentManifest:
    """Tile ``[0, duration)`` with non-overlapping cores and overlapping context.

    Analysis is never truncated by a final PPT page budget. Image/token overflow
    splits batches or cores instead of dropping the tail of the video.
    """

    if duration_seconds <= 0:
        raise SchedulerError("cannot schedule analysis for non-positive duration")
    cfg = config or SchedulerConfig()
    if cfg.core_seconds <= 0 or cfg.max_core_seconds <= 0:
        raise SchedulerError("core window sizes must be positive")
    if cfg.max_core_seconds < cfg.core_seconds:
        raise SchedulerError("max_core_seconds must be >= core_seconds")

    max_images = (
        cfg.image_batch_size
        if cfg.image_batch_size is not None
        else min(DEFAULT_IMAGE_BATCH, capabilities.max_images)
    )
    if capabilities.max_images == 0:
        max_images = 0
    active_map = _scheduling_course_map(course_map, source_id)
    boundaries = sentence_boundaries(transcript)
    windows: list[AnalysisWindow] = []
    cursor = 0.0
    index = 1

    while cursor < duration_seconds - 1e-9:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled("segment scheduling cancelled")
        cut = _choose_cut(
            cursor,
            duration_seconds,
            boundaries,
            core_seconds=cfg.core_seconds,
            max_core_seconds=cfg.max_core_seconds,
        )
        if cut <= cursor:
            cut = min(duration_seconds, cursor + cfg.max_core_seconds)
        if cut <= cursor:
            raise SchedulerError("scheduler failed to advance the core cursor")
        pieces = _split_until_fit(
            cursor,
            cut,
            duration=duration_seconds,
            transcript=transcript,
            visual=visual,
            capabilities=capabilities,
            cfg=cfg,
            max_images=max_images,
            index_start=index,
            boundaries=boundaries,
            course_map=active_map,
        )
        windows.extend(pieces)
        cursor = pieces[-1].core_end_seconds
        index += len(pieces)

    # Re-number after recursive splits so IDs stay sequential.
    numbered: list[AnalysisWindow] = []
    for position, window in enumerate(windows, start=1):
        new_id = f"seg-{position:04d}"
        batches = [
            batch.model_copy(update={"id": batch.id.replace(window.id, new_id, 1)})
            for batch in window.image_batches
        ]
        numbered.append(window.model_copy(update={"id": new_id, "image_batches": batches}))

    manifest = SegmentManifest(
        schema_version="1.0",
        source_id=source_id,
        duration_seconds=duration_seconds,
        windows=numbered,
    )
    if not cores_cover_duration(manifest.windows, duration_seconds):
        raise SchedulerError("scheduled core windows do not tile [0, duration)")
    return manifest


def set_window_status(
    manifest: SegmentManifest,
    window_id: str,
    status: str,
    *,
    failure_reason: str | None = None,
) -> SegmentManifest:
    updated: list[AnalysisWindow] = []
    for window in manifest.windows:
        if window.id != window_id:
            updated.append(window)
            continue
        updated.append(
            window.model_copy(update={"status": status, "failure_reason": failure_reason})
        )
    return manifest.model_copy(update={"windows": updated})

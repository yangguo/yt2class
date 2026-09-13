"""Segment scheduler: tile core windows, overlap context, and honor payload budgets."""

from __future__ import annotations

from dataclasses import dataclass
import re
from threading import Event

from yt2class.adapters.providers.base import ProviderCapabilities, RequestCancelled
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
    estimate_tokens,
    frames_in_range,
    join_texts,
)

DEFAULT_CORE_SECONDS = 120.0
DEFAULT_MAX_CORE_SECONDS = 180.0
DEFAULT_CONTEXT_SECONDS = 10.0
DEFAULT_IMAGE_BATCH = 8
SENTENCE_END = re.compile(r"[.!?。！？][\"'”’)\]]*\s*$")


@dataclass(frozen=True)
class SchedulerConfig:
    core_seconds: float = DEFAULT_CORE_SECONDS
    max_core_seconds: float = DEFAULT_MAX_CORE_SECONDS
    context_seconds: float = DEFAULT_CONTEXT_SECONDS
    image_batch_size: int | None = None
    output_reserve_tokens: int = OUTPUT_RESERVE_TOKENS


class SchedulerError(ValueError):
    """Raised when a timeline cannot be scheduled without a page-budget cheat."""


def sentence_boundaries(transcript: TranscriptDocument) -> list[float]:
    ends: list[float] = []
    for segment in transcript.segments:
        if SENTENCE_END.search(segment.text_original):
            ends.append(float(segment.end_seconds))
        else:
            ends.append(float(segment.end_seconds))
    return sorted(set(ends))


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
    # Next sentence still inside the hard cap; otherwise split the ultra-long span.
    later = [stamp for stamp in candidates if stamp > target]
    return later[0] if later else limit


def _batch_frames(
    frames: list[dict[str, object]],
    *,
    window_id: str,
    max_images: int,
    transcript_text: str,
    output_tokens: int,
) -> tuple[list[ImageBatch], int]:
    if max_images <= 0:
        batch = ImageBatch(
            id=f"{window_id}-batch-01",
            evidence_ids=[],
            estimated_input_tokens=estimate_tokens(transcript_text, image_count=0),
            estimated_output_tokens=output_tokens,
            image_count=0,
        )
        return [batch], batch.estimated_input_tokens
    if not frames:
        batch = ImageBatch(
            id=f"{window_id}-batch-01",
            evidence_ids=[],
            estimated_input_tokens=estimate_tokens(transcript_text, image_count=0),
            estimated_output_tokens=output_tokens,
            image_count=0,
        )
        return [batch], batch.estimated_input_tokens

    batches: list[ImageBatch] = []
    total_tokens = 0
    for index in range(0, len(frames), max_images):
        chunk = frames[index : index + max_images]
        image_count = len(chunk)
        tokens = estimate_tokens(transcript_text, image_count=image_count)
        batches.append(
            ImageBatch(
                id=f"{window_id}-batch-{len(batches) + 1:02d}",
                evidence_ids=[str(frame["id"]) for frame in chunk],
                estimated_input_tokens=tokens,
                estimated_output_tokens=output_tokens,
                image_count=image_count,
            )
        )
        total_tokens += tokens
    return batches, total_tokens


def _window_fits_budget(
    estimated_input_tokens: int,
    image_count: int,
    capabilities: ProviderCapabilities,
) -> bool:
    return (
        estimated_input_tokens <= capabilities.max_input_tokens
        and image_count <= capabilities.max_images
    )


def schedule_windows(
    *,
    source_id: str,
    duration_seconds: float,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    capabilities: ProviderCapabilities,
    config: SchedulerConfig | None = None,
    cancel_event: Event | None = None,
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

        core_start, core_end = cursor, cut
        # Split further when even a single-image batch would overflow tokens.
        text = join_texts(
            row["text_original"]
            for row in [
                {
                    "text_original": segment.text_original,
                }
                for segment in transcript.segments
                if segment.end_seconds > core_start and segment.start_seconds < core_end
            ]
        )
        frames = frames_in_range(visual, core_start, core_end)
        probe_images = 0 if max_images == 0 else min(len(frames), max(1, max_images) if frames else 0)
        probe_tokens = estimate_tokens(text, image_count=probe_images)
        if frames and probe_tokens > capabilities.max_input_tokens and (core_end - core_start) > 1.0:
            cut = min(core_end, core_start + max(1.0, (core_end - core_start) / 2.0))
            core_end = cut
            text = join_texts(
                segment.text_original
                for segment in transcript.segments
                if segment.end_seconds > core_start and segment.start_seconds < core_end
            )
            frames = frames_in_range(visual, core_start, core_end)

        context_start = max(0.0, core_start - cfg.context_seconds)
        context_end = min(duration_seconds, core_end + cfg.context_seconds)
        window_id = f"seg-{index:04d}"
        context_frames = frames_in_range(visual, context_start, context_end)
        context_text = join_texts(
            segment.text_original
            for segment in transcript.segments
            if segment.end_seconds > context_start and segment.start_seconds < context_end
        )
        batches, token_total = _batch_frames(
            context_frames,
            window_id=window_id,
            max_images=max_images if max_images > 0 else 1,
            transcript_text=context_text,
            output_tokens=min(cfg.output_reserve_tokens, capabilities.max_output_tokens),
        )
        if max_images == 0:
            batches, token_total = _batch_frames(
                [],
                window_id=window_id,
                max_images=0,
                transcript_text=context_text,
                output_tokens=min(cfg.output_reserve_tokens, capabilities.max_output_tokens),
            )
        heaviest = max((batch.image_count for batch in batches), default=0)
        if not _window_fits_budget(max((batch.estimated_input_tokens for batch in batches), default=0), heaviest, capabilities):
            # Keep the window; ledger will mark it degraded when analysis hits overflow.
            status = "scheduled"
            failure = None
        else:
            status = "scheduled"
            failure = None
        windows.append(
            AnalysisWindow(
                id=window_id,
                core_start_seconds=core_start,
                core_end_seconds=core_end,
                context_start_seconds=context_start,
                context_end_seconds=context_end,
                evidence_ids=evidence_in_range(transcript, visual, context_start, context_end),
                status=status,
                image_batches=batches,
                estimated_input_tokens=token_total,
                estimated_output_tokens=min(cfg.output_reserve_tokens, capabilities.max_output_tokens),
                failure_reason=failure,
            )
        )
        cursor = core_end
        index += 1

    manifest = SegmentManifest(
        schema_version="1.0",
        source_id=source_id,
        duration_seconds=duration_seconds,
        windows=windows,
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

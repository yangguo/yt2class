"""Frame multimodal analyst: structured KnowledgeUnits with one contract repair."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any, Iterable

from pydantic import ValidationError

from yt2class.adapters.providers.base import (
    ContextOverflow,
    MissingStructuredOutput,
    Provider,
    ProviderError,
    RequestCancelled,
)
from yt2class.domain.course_map import CourseMap
from yt2class.domain.knowledge import KnowledgeUnit
from yt2class.domain.segment import AnalysisWindow, SegmentManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.scheduler import set_window_status
from yt2class.stages.llm_util import (
    OUTPUT_RESERVE_TOKENS,
    contains_path_literal,
    frames_in_range,
    load_prompt,
    model_request,
    text_has_negation,
    text_has_units,
    transcript_in_range,
)

TEMPORAL_MARKERS = (
    "然后",
    "接着",
    "随后",
    "之后",
    "step",
    "then",
    "after",
    "before",
    "next",
)
PATH_ID = re.compile(r"[/\\]|\.(?:jpg|jpeg|png|webp|gif|mp4)$", re.I)


class SegmentContractError(ValueError):
    """Structured output is HTTP-ok but fails the KnowledgeUnit contract."""


@dataclass
class SegmentAnalysisOutcome:
    window: AnalysisWindow
    payload: dict[str, Any]
    units: list[KnowledgeUnit] = field(default_factory=list)
    repaired: bool = False
    error: str | None = None


def topic_for_window(course_map: CourseMap, window: AnalysisWindow) -> str | None:
    overlapping = [
        topic
        for topic in course_map.topics
        if topic.end_seconds > window.core_start_seconds
        and topic.start_seconds < window.core_end_seconds
    ]
    if not overlapping:
        return course_map.topics[0].id if course_map.topics else None
    overlapping.sort(
        key=lambda topic: (
            -(
                min(topic.end_seconds, window.core_end_seconds)
                - max(topic.start_seconds, window.core_start_seconds)
            ),
            topic.start_seconds,
        )
    )
    return overlapping[0].id


def _clip_payloads(extra_clips: Iterable[object] | None) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for item in extra_clips or []:
        if isinstance(item, dict):
            clip_id = item.get("id")
            if clip_id:
                clips.append(
                    {
                        "id": clip_id,
                        "start_seconds": item.get("start_seconds"),
                        "end_seconds": item.get("end_seconds"),
                    }
                )
            continue
        as_payload = getattr(item, "as_payload", None)
        if callable(as_payload):
            clips.append(as_payload())
            continue
        asset = getattr(item, "asset", None)
        clip_id = getattr(item, "id", None) or getattr(asset, "id", None)
        if clip_id:
            clips.append(
                {
                    "id": clip_id,
                    "start_seconds": getattr(item, "start_seconds", None),
                    "end_seconds": getattr(item, "end_seconds", None),
                    "sha256": getattr(asset, "sha256", None),
                }
            )
    return clips


def local_allowed_ids(
    window: AnalysisWindow,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    batch_evidence_ids: list[str] | None = None,
    extra_clips: Iterable[object] | None = None,
) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    context_start = window.context_start_seconds
    context_end = window.context_end_seconds
    frames = frames_in_range(visual, context_start, context_end)
    if batch_evidence_ids is not None:
        allowed_batch = set(batch_evidence_ids)
        frames = [frame for frame in frames if frame["id"] in allowed_batch]
    frame_ids = {frame["id"] for frame in frames}
    local = {
        segment.id
        for segment in transcript.segments
        if segment.end_seconds > context_start and segment.start_seconds < context_end
    }
    local |= frame_ids
    local |= {
        region.id
        for region in visual.ocr_regions
        if region.parent_occurrence_id in frame_ids
    }
    clips = _clip_payloads(extra_clips)
    local |= {str(clip["id"]) for clip in clips}
    return local, frames, clips


def build_segment_payload(
    window: AnalysisWindow,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    analysis_mode: str = "frames",
    output_language: str = "zh-CN",
    batch_evidence_ids: list[str] | None = None,
    extra_clips: Iterable[object] | None = None,
) -> dict[str, Any]:
    context_start = window.context_start_seconds
    context_end = window.context_end_seconds
    allowed, frames, clips = local_allowed_ids(
        window,
        transcript=transcript,
        visual=visual,
        batch_evidence_ids=batch_evidence_ids,
        extra_clips=extra_clips,
    )
    topic_id = topic_for_window(course_map, window)
    topic = next((item for item in course_map.topics if item.id == topic_id), None)
    return {
        "schema_version": "1.0",
        "prompt": load_prompt("segment.md"),
        "segment_id": window.id,
        "core_range": [window.core_start_seconds, window.core_end_seconds],
        "context_range": [context_start, context_end],
        "course_context": {
            "topic_id": topic_id,
            "title": None if topic is None else topic.title,
            "speculative": False if topic is None else topic.speculative,
            "audience": "learners",
        },
        "evidence_ids": sorted(allowed),
        "allowed_frame_ids": [frame["id"] for frame in frames],
        "allowed_evidence_ids": sorted(allowed),
        "frames": frames,
        "clips": clips,
        "transcript": transcript_in_range(transcript, context_start, context_end),
        "ocr": [
            {"id": region.id, "parent_frame_id": region.parent_occurrence_id, "text": region.text}
            for region in visual.ocr_regions
            if region.parent_occurrence_id in {frame["id"] for frame in frames}
        ],
        "analysis_mode": analysis_mode,
        "output_language": output_language,
        "constraints": {
            "external_knowledge": False,
            "max_evidence_requests": 2,
            "page_budget": None,
        },
    }


def _looks_temporal(text: str) -> bool:
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in TEMPORAL_MARKERS)


def _collect_text(unit: KnowledgeUnit) -> str:
    return " ".join(claim.text for claim in unit.claims)


def _cited_temporal_support(unit: KnowledgeUnit, allowed_frames: set[str], clip_ids: set[str]) -> int:
    cited_frames = {
        item
        for claim in unit.claims
        for item in claim.evidence_ids
        if item in allowed_frames
    }
    cited_clips = {
        item
        for claim in unit.claims
        for item in claim.evidence_ids
        if item in clip_ids
    }
    return len(cited_frames) + (2 if cited_clips else 0)


def validate_knowledge_units(
    structured: dict[str, Any] | None,
    payload: dict[str, Any],
) -> list[KnowledgeUnit]:
    if not isinstance(structured, dict):
        raise SegmentContractError("segment result missing structured object")
    raw_units = structured.get("units")
    if not isinstance(raw_units, list):
        raise SegmentContractError("segment result must contain a units list")

    allowed = set(payload.get("allowed_evidence_ids") or [])
    allowed_frames = set(payload.get("allowed_frame_ids") or [])
    clip_ids = {str(clip.get("id")) for clip in payload.get("clips") or [] if clip.get("id")}
    topic_id = (payload.get("course_context") or {}).get("topic_id")
    context_start, context_end = payload.get("context_range") or [0.0, 0.0]
    transcript_text = " ".join(
        str(row.get("text_original") or "") for row in payload.get("transcript") or []
    )
    units: list[KnowledgeUnit] = []
    for raw in raw_units:
        if not isinstance(raw, dict):
            raise SegmentContractError("knowledge unit must be an object")
        dumped = dict(raw)
        dumped.setdefault("segment_ids", [payload["segment_id"]])
        if topic_id and "topic_id" not in dumped:
            dumped["topic_id"] = topic_id
        try:
            unit = KnowledgeUnit.model_validate(dumped)
        except ValidationError as error:
            raise SegmentContractError(f"knowledge unit failed schema: {error}") from error
        if payload["segment_id"] not in unit.segment_ids:
            raise SegmentContractError(f"unit {unit.id} missing current segment id")
        if topic_id and unit.topic_id != topic_id:
            raise SegmentContractError(f"unit {unit.id} cites unknown or other-window topic")
        if unit.end_seconds <= context_start or unit.start_seconds >= context_end:
            raise SegmentContractError(f"unit {unit.id} range is outside the analysis window")

        for claim in unit.claims:
            if not claim.evidence_ids:
                raise SegmentContractError(f"claim {claim.id} is unsourced")
            unknown = [item for item in claim.evidence_ids if item not in allowed]
            if unknown:
                raise SegmentContractError(f"claim {claim.id} cites unknown evidence {unknown}")
            for item in claim.evidence_ids:
                if contains_path_literal(item) or PATH_ID.search(item):
                    raise SegmentContractError(f"claim {claim.id} uses a bare image path as evidence")
            if contains_path_literal(claim.text):
                raise SegmentContractError(f"claim {claim.id} embeds a bare image path")

        for candidate in unit.visual_candidates:
            if candidate.frame_id not in allowed_frames and candidate.frame_id not in allowed:
                raise SegmentContractError(
                    f"unit {unit.id} visual candidate cites unknown frame {candidate.frame_id}"
                )

        temporal = (
            unit.kind == "procedure"
            or any(relation.kind == "step_before" for relation in unit.relations)
            or _looks_temporal(_collect_text(unit))
        )
        if temporal and _cited_temporal_support(unit, allowed_frames, clip_ids) < 2:
            raise SegmentContractError(
                f"unit {unit.id} claims a temporal sequence from a single frame"
            )

        if text_has_negation(transcript_text) and not text_has_negation(_collect_text(unit)):
            raise SegmentContractError(f"unit {unit.id} dropped a transcript negation")
        if text_has_units(transcript_text) and not text_has_units(_collect_text(unit)):
            raise SegmentContractError(f"unit {unit.id} dropped a transcript unit or quantity")
        units.append(unit)
    return units


def reserved_output_tokens(window: AnalysisWindow, provider: Provider) -> int:
    scheduled = window.estimated_output_tokens
    cap = provider.capabilities.max_output_tokens
    if scheduled > 0:
        return min(scheduled, cap)
    return min(OUTPUT_RESERVE_TOKENS, cap)


def _clip_seconds(extra_clips: Iterable[object] | None) -> float:
    total = 0.0
    for item in extra_clips or []:
        if isinstance(item, dict):
            start = item.get("start_seconds") or 0.0
            end = item.get("end_seconds") or 0.0
        else:
            start = getattr(item, "start_seconds", 0.0) or 0.0
            end = getattr(item, "end_seconds", 0.0) or 0.0
        total += max(0.0, float(end) - float(start))
    return total


def _complete_with_payload(
    provider: Provider,
    request_id: str,
    payload: dict[str, Any],
    *,
    image_count: int,
    output_tokens: int,
    video_seconds: float = 0.0,
    cancel_event: Event | None,
):
    request = model_request(
        request_id=request_id,
        role="segment",
        payload=payload,
        image_count=image_count,
        video_seconds=video_seconds,
        output_tokens=output_tokens,
    )
    if hasattr(provider, "last_payload"):
        provider.last_payload = payload
    return provider.complete(request, cancel_event=cancel_event)


def analyze_window(
    window: AnalysisWindow,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    provider: Provider,
    cancel_event: Event | None = None,
    analysis_mode: str = "frames",
    request_suffix: str = "",
    extra_clips: Iterable[object] | None = None,
) -> SegmentAnalysisOutcome:
    """Analyze one window. HTTP-ok contract failures get at most one repair."""

    batches = window.image_batches or [None]
    all_units: list[KnowledgeUnit] = []
    last_payload: dict[str, Any] | None = None
    repaired = False
    last_error: str | None = None
    output_tokens = reserved_output_tokens(window, provider)

    for batch in batches:
        batch_ids = None if batch is None else list(batch.evidence_ids)
        payload = build_segment_payload(
            window,
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            analysis_mode=analysis_mode,
            batch_evidence_ids=batch_ids,
            extra_clips=extra_clips,
        )
        last_payload = payload
        image_count = 0 if batch is None else batch.image_count
        suffix = "" if batch is None else f":{batch.id}"
        video_seconds = _clip_seconds(extra_clips)

        def _attempt(request_id: str, attempt_payload: dict[str, Any]):
            return _complete_with_payload(
                provider,
                request_id,
                attempt_payload,
                image_count=image_count,
                output_tokens=output_tokens,
                video_seconds=video_seconds,
                cancel_event=cancel_event,
            )

        try:
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelled(f"segment {window.id} cancelled")
            result = _attempt(f"seg:{window.id}{suffix}{request_suffix}", payload)
            if result.structured is None:
                raise SegmentContractError("missing structured output")
            all_units.extend(validate_knowledge_units(result.structured, payload))
            last_error = None
        except RequestCancelled as error:
            return SegmentAnalysisOutcome(
                window=window.model_copy(
                    update={"status": "failed", "failure_reason": "cancelled"}
                ),
                payload=payload,
                units=[],
                error=str(error),
            )
        except (SegmentContractError, MissingStructuredOutput) as error:
            last_error = str(error)
            repair_payload = dict(payload)
            repair_payload["repair"] = {"error": last_error, "attempt": 1}
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise RequestCancelled(f"segment {window.id} cancelled")
                repaired_result = _attempt(
                    f"seg:{window.id}{suffix}{request_suffix}:repair",
                    repair_payload,
                )
                if repaired_result.structured is None:
                    raise SegmentContractError("missing structured output")
                all_units.extend(validate_knowledge_units(repaired_result.structured, payload))
                repaired = True
                last_error = None
            except RequestCancelled as cancel_error:
                return SegmentAnalysisOutcome(
                    window=window.model_copy(
                        update={"status": "failed", "failure_reason": "cancelled"}
                    ),
                    payload=repair_payload,
                    units=[],
                    repaired=True,
                    error=str(cancel_error),
                )
            except (SegmentContractError, MissingStructuredOutput, ProviderError) as repair_error:
                last_error = str(repair_error)
                return SegmentAnalysisOutcome(
                    window=window.model_copy(
                        update={"status": "failed", "failure_reason": last_error[:400]}
                    ),
                    payload=repair_payload,
                    units=[],
                    repaired=True,
                    error=last_error,
                )
        except (ContextOverflow, ProviderError) as error:
            last_error = str(error)
            return SegmentAnalysisOutcome(
                window=window.model_copy(
                    update={"status": "failed", "failure_reason": last_error[:400]}
                ),
                payload=payload,
                units=[],
                error=last_error,
            )

    status = "complete"
    if last_error:
        status = "failed"
    elif any(unit.uncertainty or unit.evidence_requests for unit in all_units):
        status = "degraded"
    elif not all_units:
        status = "degraded"
        last_error = "no knowledge units produced"
    return SegmentAnalysisOutcome(
        window=window.model_copy(update={"status": status, "failure_reason": last_error}),
        payload=last_payload or {},
        units=all_units,
        repaired=repaired,
        error=last_error,
    )


def analyze_segments(
    manifest: SegmentManifest,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    provider: Provider,
    cancel_event: Event | None = None,
    extra_clips: Iterable[object] | None = None,
) -> tuple[SegmentManifest, list[KnowledgeUnit], list[SegmentAnalysisOutcome]]:
    """Run every scheduled window. Does not apply a PPT page budget."""

    outcomes: list[SegmentAnalysisOutcome] = []
    units: list[KnowledgeUnit] = []
    current = manifest
    remaining_ids = [window.id for window in manifest.windows]
    for window in manifest.windows:
        remaining_ids = remaining_ids[1:]
        if cancel_event is not None and cancel_event.is_set():
            current = set_window_status(
                current, window.id, "failed", failure_reason="cancelled"
            )
            for leftover in remaining_ids:
                current = set_window_status(
                    current, leftover, "failed", failure_reason="cancelled"
                )
            return current, units, outcomes
        running = set_window_status(current, window.id, "running")
        outcome = analyze_window(
            next(item for item in running.windows if item.id == window.id),
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            provider=provider,
            cancel_event=cancel_event,
            extra_clips=extra_clips,
        )
        current = set_window_status(
            running,
            window.id,
            outcome.window.status,
            failure_reason=outcome.window.failure_reason,
        )
        outcomes.append(outcome)
        units.extend(outcome.units)
        if outcome.window.failure_reason == "cancelled":
            for leftover in remaining_ids:
                current = set_window_status(
                    current, leftover, "failed", failure_reason="cancelled"
                )
            return current, units, outcomes
    return current, units, outcomes

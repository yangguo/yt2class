"""Frame multimodal analyst: structured KnowledgeUnits with one contract repair."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any

from pydantic import ValidationError

from yt2class.adapters.providers.base import (
    ContextOverflow,
    Provider,
    RequestCancelled,
)
from yt2class.domain.course_map import CourseMap
from yt2class.domain.knowledge import KnowledgeUnit
from yt2class.domain.segment import AnalysisWindow, SegmentManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.scheduler import set_window_status
from yt2class.stages.llm_util import (
    allowed_evidence_ids,
    contains_path_literal,
    evidence_in_range,
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
    "先",
    "再",
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


def build_segment_payload(
    window: AnalysisWindow,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    analysis_mode: str = "frames",
    output_language: str = "zh-CN",
    batch_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    allowed = sorted(allowed_evidence_ids(transcript, visual))
    context_start = window.context_start_seconds
    context_end = window.context_end_seconds
    frames = frames_in_range(visual, context_start, context_end)
    if batch_evidence_ids is not None:
        allowed_batch = set(batch_evidence_ids)
        frames = [frame for frame in frames if frame["id"] in allowed_batch]
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
        "evidence_ids": evidence_in_range(transcript, visual, context_start, context_end),
        "allowed_frame_ids": [frame["id"] for frame in frames],
        "allowed_evidence_ids": allowed,
        "frames": frames,
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
    topic_id = (payload.get("course_context") or {}).get("topic_id")
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

        frame_count = len(allowed_frames)
        temporal = unit.kind == "procedure" or any(
            relation.kind == "step_before" for relation in unit.relations
        ) or _looks_temporal(_collect_text(unit))
        if temporal and frame_count < 2 and unit.kind == "procedure":
            raise SegmentContractError(
                f"unit {unit.id} claims a temporal sequence from a single frame"
            )
        if temporal and frame_count < 2 and any(
            relation.kind == "step_before" for relation in unit.relations
        ):
            raise SegmentContractError(
                f"unit {unit.id} claims a temporal sequence from a single frame"
            )

        if text_has_negation(transcript_text) and not text_has_negation(_collect_text(unit)):
            raise SegmentContractError(f"unit {unit.id} dropped a transcript negation")
        if text_has_units(transcript_text) and not text_has_units(_collect_text(unit)):
            raise SegmentContractError(f"unit {unit.id} dropped a transcript unit or quantity")
        units.append(unit)
    return units


def _complete_with_payload(
    provider: Provider,
    request_id: str,
    payload: dict[str, Any],
    *,
    image_count: int,
    cancel_event: Event | None,
):
    request = model_request(
        request_id=request_id,
        role="segment",
        payload=payload,
        image_count=image_count,
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
) -> SegmentAnalysisOutcome:
    """Analyze one window. HTTP-ok contract failures get at most one repair."""

    batches = window.image_batches or [None]
    all_units: list[KnowledgeUnit] = []
    last_payload: dict[str, Any] | None = None
    repaired = False
    last_error: str | None = None

    for batch in batches:
        batch_ids = None if batch is None else list(batch.evidence_ids)
        payload = build_segment_payload(
            window,
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            analysis_mode=analysis_mode,
            batch_evidence_ids=batch_ids,
        )
        last_payload = payload
        image_count = 0 if batch is None else batch.image_count
        suffix = "" if batch is None else f":{batch.id}"
        try:
            result = _complete_with_payload(
                provider,
                f"seg:{window.id}{suffix}{request_suffix}",
                payload,
                image_count=image_count,
                cancel_event=cancel_event,
            )
            all_units.extend(validate_knowledge_units(result.structured, payload))
            last_error = None
        except SegmentContractError as error:
            last_error = str(error)
            repair_payload = dict(payload)
            repair_payload["repair"] = {"error": last_error, "attempt": 1}
            try:
                repaired_result = _complete_with_payload(
                    provider,
                    f"seg:{window.id}{suffix}{request_suffix}:repair",
                    repair_payload,
                    image_count=image_count,
                    cancel_event=cancel_event,
                )
                all_units.extend(validate_knowledge_units(repaired_result.structured, payload))
                repaired = True
                last_error = None
            except SegmentContractError as repair_error:
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
        except ContextOverflow as error:
            last_error = str(error)
            return SegmentAnalysisOutcome(
                window=window.model_copy(
                    update={"status": "failed", "failure_reason": last_error[:400]}
                ),
                payload=payload,
                units=[],
                error=last_error,
            )
        except RequestCancelled:
            raise

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
) -> tuple[SegmentManifest, list[KnowledgeUnit], list[SegmentAnalysisOutcome]]:
    """Run every scheduled window. Does not apply a PPT page budget."""

    outcomes: list[SegmentAnalysisOutcome] = []
    units: list[KnowledgeUnit] = []
    current = manifest
    for window in manifest.windows:
        if cancel_event is not None and cancel_event.is_set():
            current = set_window_status(
                current, window.id, "failed", failure_reason="cancelled"
            )
            raise RequestCancelled(f"segment analysis cancelled at {window.id}")
        running = set_window_status(current, window.id, "running")
        outcome = analyze_window(
            next(item for item in running.windows if item.id == window.id),
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            provider=provider,
            cancel_event=cancel_event,
        )
        current = set_window_status(
            running,
            window.id,
            outcome.window.status,
            failure_reason=outcome.window.failure_reason,
        )
        outcomes.append(outcome)
        units.extend(outcome.units)
    return current, units, outcomes

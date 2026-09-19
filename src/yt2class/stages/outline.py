"""CourseMap outline: partition the full transcript, then reduce dependencies."""

from __future__ import annotations

from threading import Event
from typing import Any

from pydantic import ValidationError

from yt2class.adapters.providers.base import Provider, RequestCancelled
from yt2class.orchestration.concurrency import map_parallel
from yt2class.domain.course_map import (
    CourseMap,
    OutlineBlock,
    Topic,
    TopicRelation,
)
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.stages.llm_util import (
    attach_provider_payload,
    evidence_in_range,
    frames_in_range,
    load_prompt,
    model_request,
    transcript_in_range,
)
from yt2class.stages.structured_coerce import allocate_unique_id, coerce_topic, extract_topics_list

DEFAULT_BLOCK_CHARS = 4000


class OutlineError(ValueError):
    """Raised when outline output cannot be reduced into a CourseMap."""


def partition_transcript(
    transcript: TranscriptDocument,
    *,
    duration_seconds: float,
    max_block_chars: int = DEFAULT_BLOCK_CHARS,
) -> list[OutlineBlock]:
    """Schedule every transcript segment. Empty speech becomes one visual-only block."""

    if duration_seconds <= 0:
        raise OutlineError("cannot outline a non-positive duration")
    if max_block_chars <= 0:
        raise OutlineError("max_block_chars must be positive")

    if not transcript.segments:
        return [
            OutlineBlock(
                id="block-0001",
                start_seconds=0.0,
                end_seconds=duration_seconds,
                evidence_ids=[],
                char_count=0,
            )
        ]

    blocks: list[OutlineBlock] = []
    current: list[Any] = []
    current_chars = 0
    index = 1

    def flush() -> None:
        nonlocal current, current_chars, index
        if not current:
            return
        start = float(current[0].start_seconds)
        end = float(current[-1].end_seconds)
        blocks.append(
            OutlineBlock(
                id=f"block-{index:04d}",
                start_seconds=start,
                end_seconds=end,
                evidence_ids=[segment.id for segment in current],
                char_count=current_chars,
            )
        )
        index += 1
        current = []
        current_chars = 0

    for segment in transcript.segments:
        text_len = len(segment.text_original)
        if current and current_chars + text_len > max_block_chars:
            flush()
        current.append(segment)
        current_chars += text_len
        if text_len > max_block_chars:
            flush()
    flush()
    return blocks


def _validate_topic_payload(
    raw: dict[str, Any],
    *,
    block: OutlineBlock,
    allowed: set[str],
    duration_seconds: float,
    index: int,
) -> Topic | str:
    coerced = coerce_topic(
        raw,
        block_id=block.id,
        block_start=block.start_seconds,
        block_end=block.end_seconds,
        index=index,
    )
    if coerced is None:
        return f"invalid topic in {block.id}: missing title, goal, or time range"
    try:
        topic = Topic.model_validate(coerced)
    except ValidationError as error:
        return f"invalid topic in {block.id}: {error}"
    if topic.end_seconds > duration_seconds + 1e-9 or topic.start_seconds >= duration_seconds:
        return f"out-of-range topic {topic.id}"
    if topic.end_seconds <= block.start_seconds or topic.start_seconds >= block.end_seconds:
        return f"out-of-range topic {topic.id}"
    missing = [item for item in topic.evidence_ids if item not in allowed]
    if missing:
        return f"out-of-range refs on topic {topic.id}: {missing}"
    if not topic.speculative and not topic.evidence_ids:
        return f"non-speculative topic {topic.id} missing block-local evidence"
    return topic


def validate_outline_topics(
    structured: dict[str, Any] | list[Any] | None,
    *,
    block: OutlineBlock,
    allowed: set[str],
    duration_seconds: float,
) -> tuple[list[Topic], list[str]]:
    if structured is None:
        return [], [f"{block.id}: missing structured outline"]
    raw_topics = extract_topics_list(structured)
    if not isinstance(raw_topics, list):
        return [], [f"{block.id}: outline topics must be a list"]
    topics: list[Topic] = []
    reasons: list[str] = []
    used_topic_ids: set[str] = set()
    for index, raw in enumerate(raw_topics, start=1):
        if not isinstance(raw, dict):
            reasons.append(f"{block.id}: topic is not an object")
            continue
        result = _validate_topic_payload(
            raw,
            block=block,
            allowed=allowed,
            duration_seconds=duration_seconds,
            index=index,
        )
        if isinstance(result, str):
            reasons.append(result)
        else:
            new_id = allocate_unique_id(result.id, used_topic_ids)
            topics.append(result if new_id == result.id else result.model_copy(update={"id": new_id}))
    extra_guesses = structured.get("unverified_guesses") if isinstance(structured, dict) else []
    if isinstance(extra_guesses, list):
        reasons.extend(str(item) for item in extra_guesses if item)
    return topics, reasons


def _ranges_conflict(left: Topic, right: Topic) -> bool:
    overlap = min(left.end_seconds, right.end_seconds) - max(left.start_seconds, right.start_seconds)
    if overlap <= 0:
        return False
    shorter = min(
        left.end_seconds - left.start_seconds,
        right.end_seconds - right.start_seconds,
    )
    if shorter <= 0:
        return False
    same_span = (
        abs(left.start_seconds - right.start_seconds) < 1e-6
        and abs(left.end_seconds - right.end_seconds) < 1e-6
    )
    heavy_overlap = overlap >= 0.5 * shorter
    return (same_span or heavy_overlap) and left.title != right.title


def reduce_outline(
    source_id: str,
    block_results: list[tuple[OutlineBlock, list[Topic], list[str]]],
    *,
    duration_seconds: float,
) -> CourseMap:
    """Merge block topics, drop invalid/conflicting blocks, and add follows edges."""

    kept: list[Topic] = []
    guesses: list[str] = []
    dropped_blocks: list[str] = []

    for block, topics, reasons in block_results:
        guesses.extend(reasons)
        if block.dropped or not topics:
            dropped_blocks.append(block.id)
            if block.drop_reason:
                guesses.append(f"dropped {block.id}: {block.drop_reason}")
            elif not topics:
                guesses.append(f"dropped {block.id}: empty or invalid outline")
            continue
        for topic in topics:
            conflict = next((existing for existing in kept if _ranges_conflict(existing, topic)), None)
            if conflict is not None:
                guesses.append(
                    f"conflicting topics {conflict.id} and {topic.id}; dropped {topic.id}"
                )
                continue
            kept.append(topic)

    if not kept:
        kept.append(
            Topic(
                id="topic-unverified",
                title="未覆盖主题",
                goal="记录缺失大纲，等待补证据",
                start_seconds=0.0,
                end_seconds=duration_seconds,
                evidence_ids=[],
                speculative=True,
            )
        )
        guesses.append("no verified outline topics; speculative placeholder only")

    kept.sort(key=lambda topic: (topic.start_seconds, topic.end_seconds, topic.id))
    unique: list[Topic] = []
    seen: set[str] = set()
    for topic in kept:
        new_id = allocate_unique_id(topic.id, seen)
        unique.append(topic if new_id == topic.id else topic.model_copy(update={"id": new_id}))
    relations: list[TopicRelation] = []
    for previous, current in zip(unique, unique[1:]):
        relations.append(
            TopicRelation(from_topic_id=previous.id, to_topic_id=current.id, kind="follows")
        )

    return CourseMap(
        schema_version="1.0",
        source_id=source_id,
        topics=unique,
        relations=relations,
        unverified_guesses=guesses[:50],
    )


def outline_course(
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider,
    *,
    source_id: str,
    duration_seconds: float,
    cancel_event: Event | None = None,
    max_block_chars: int = DEFAULT_BLOCK_CHARS,
) -> CourseMap:
    """Build a CourseMap that schedules the full transcript through the provider."""

    blocks = partition_transcript(
        transcript, duration_seconds=duration_seconds, max_block_chars=max_block_chars
    )
    prompt = load_prompt("outline.md")

    def outline_block(block: OutlineBlock) -> tuple[OutlineBlock, list[Topic], list[str]]:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"outline cancelled before {block.id}")
        visual_ids = evidence_in_range(transcript, visual, block.start_seconds, block.end_seconds)
        frame_rows = frames_in_range(visual, block.start_seconds, block.end_seconds)
        ocr_ids = {
            region.id
            for region in visual.ocr_regions
            if region.parent_occurrence_id in {row["id"] for row in frame_rows}
        }
        block_allowed = set(visual_ids) | set(block.evidence_ids) | ocr_ids
        payload = {
            "prompt": prompt,
            "block": block.model_dump(mode="json"),
            "transcript": transcript_in_range(transcript, block.start_seconds, block.end_seconds),
            "visual_overview": frame_rows,
            "allowed_evidence_ids": sorted(block_allowed),
            "constraints": {"external_knowledge": False, "page_budget": None},
        }
        def _dispatch(outline_payload: dict[str, Any], request_id: str):
            request = model_request(request_id=request_id, role="outline", payload=outline_payload)
            attach_provider_payload(provider, outline_payload)
            return provider.complete(request, cancel_event=cancel_event)

        result = _dispatch(payload, f"outline:{block.id}")
        topics, reasons = validate_outline_topics(
            result.structured,
            block=block,
            allowed=block_allowed,
            duration_seconds=duration_seconds,
        )
        if not topics:
            repair_payload = {
                **payload,
                "repair": {
                    "error": "; ".join(reasons) or "empty or invalid outline",
                    "attempt": 1,
                },
            }
            result = _dispatch(repair_payload, f"outline:{block.id}:repair")
            topics, reasons = validate_outline_topics(
                result.structured,
                block=block,
                allowed=block_allowed,
                duration_seconds=duration_seconds,
            )
        if not block_allowed and topics:
            for topic in topics:
                topic.speculative = True
        updated = block.model_copy(
            update={
                "evidence_ids": list(dict.fromkeys([*block.evidence_ids, *visual_ids])),
                "dropped": not topics,
                "drop_reason": None if topics else "; ".join(reasons) or "empty outline",
            }
        )
        return updated, topics, reasons

    results = map_parallel(blocks, outline_block, cancel_event=cancel_event)

    course_map = reduce_outline(source_id, results, duration_seconds=duration_seconds)
    if not transcript.segments:
        for topic in course_map.topics:
            topic.speculative = True
        guess = "no transcript; CourseMap topics are speculative and not source claims"
        if guess not in course_map.unverified_guesses:
            course_map.unverified_guesses.append(guess)
    return course_map

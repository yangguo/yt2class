"""Synthetic M2 fixtures. No licensed media and no live-model gold answers."""

from __future__ import annotations

from yt2class.adapters.providers.base import ProviderCapabilities
from yt2class.domain.course_map import CourseMap, Topic
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeRelation, KnowledgeUnit
from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptGap, TranscriptSegment
from yt2class.domain.visual import (
    FrameOccurrence,
    FrameQuality,
    OcrRegion,
    Scene,
    VisualAsset,
    VisualCatalogue,
    VisualGap,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def frames_caps(**overrides) -> ProviderCapabilities:
    data = dict(
        supports_images=True,
        supports_video=False,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=8000,
        max_output_tokens=2000,
        max_images=8,
        max_video_seconds=0.0,
    )
    data.update(overrides)
    return ProviderCapabilities.model_validate(data)


def _coverage(duration: float, covered: float) -> SpeechCoverage:
    ratio = 0.0 if duration == 0 else covered / duration
    return SpeechCoverage(
        speech_seconds=covered,
        covered_seconds=covered,
        denominator="timeline",
        coverage_ratio=ratio,
        denominator_seconds=duration,
    )


def make_transcript(
    segments: list[tuple[str, float, float, str]],
    *,
    source_id: str = "src-demo",
    duration: float | None = None,
    language: str = "zh-CN",
) -> TranscriptDocument:
    docs = [
        TranscriptSegment(
            id=item[0],
            start_seconds=item[1],
            end_seconds=item[2],
            text_original=item[3],
            language=language,
            origin="sidecar",
            alignment_status="aligned",
        )
        for item in segments
    ]
    end = duration if duration is not None else (max((item.end_seconds for item in docs), default=1.0))
    covered = 0.0
    cursor = 0.0
    merged: list[list[float]] = []
    for item in sorted(docs, key=lambda row: row.start_seconds):
        if merged and item.start_seconds <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], item.end_seconds)
        else:
            merged.append([item.start_seconds, item.end_seconds])
    covered = sum(stop - start for start, stop in merged)
    gaps: list[TranscriptGap] = []
    cursor = 0.0
    for start, stop in merged:
        if cursor < start:
            gaps.append(
                TranscriptGap(
                    id=f"tgap-{len(gaps)+1:04d}",
                    start_seconds=cursor,
                    end_seconds=start,
                    reason="uncovered",
                )
            )
        cursor = max(cursor, stop)
    if cursor < end:
        gaps.append(
            TranscriptGap(
                id=f"tgap-{len(gaps)+1:04d}",
                start_seconds=cursor,
                end_seconds=end,
                reason="uncovered",
            )
        )
    status = "complete" if docs and not gaps and covered >= end - 1e-9 else "degraded"
    return TranscriptDocument(
        schema_version="1.0",
        source_id=source_id,
        language=language,
        raw_artifact_hash=DIGEST_C if docs else None,
        alignment="sentence" if docs else "none",
        speech_coverage=_coverage(end, covered),
        segments=docs,
        duration_seconds=end,
        status=status,
        gaps=gaps,
    )


def make_visual(
    frames: list[tuple[str, float, str]],
    *,
    source_id: str = "src-demo",
    duration: float = 60.0,
    scenes: list[tuple[str, float, float]] | None = None,
    ocr: list[tuple[str, str, str]] | None = None,
) -> VisualCatalogue:
    if not frames:
        return VisualCatalogue(
            schema_version="1.0",
            source_id=source_id,
            scenes=[],
            assets=[],
            occurrences=[],
            ocr_regions=[],
            profile="content",
            status="degraded",
            gaps=[
                VisualGap(
                    id="vgap-0001",
                    start_seconds=0.0,
                    end_seconds=duration,
                    reason="no scenes",
                )
            ],
        )
    scene_rows = scenes or [("scene-001", 0.0, duration)]
    scene_models = [
        Scene(id=item[0], start_seconds=item[1], end_seconds=item[2], detector="content")
        for item in scene_rows
    ]
    assets: list[VisualAsset] = []
    occurrences: list[FrameOccurrence] = []
    for index, (frame_id, timestamp, scene_id) in enumerate(frames, start=1):
        digest = ("b" * 62) + f"{index:02x}"
        asset = VisualAsset(
            id=f"asset-{frame_id}",
            role="frame",
            path=f"frames/{frame_id}.jpg",
            sha256=digest,
            mime_type="image/jpeg",
            width=1280,
            height=720,
        )
        assets.append(asset)
        occurrences.append(
            FrameOccurrence(
                id=frame_id,
                scene_id=scene_id,
                asset_id=asset.id,
                requested_seconds=timestamp,
                timestamp_seconds=timestamp,
                actual_source_seconds=timestamp,
                quality=FrameQuality(
                    width=1280,
                    height=720,
                    brightness=0.5,
                    sharpness=0.7,
                    ocr_density=0.2,
                ),
            )
        )
    regions = []
    for region_id, parent_id, text in ocr or []:
        asset_id = next(item.asset_id for item in occurrences if item.id == parent_id)
        regions.append(
            OcrRegion(
                id=region_id,
                asset_id=asset_id,
                parent_occurrence_id=parent_id,
                bbox={"x": 1.0, "y": 1.0, "width": 40.0, "height": 12.0},
                text=text,
                engine="fixture",
                confidence=0.9,
            )
        )
    return VisualCatalogue(
        schema_version="1.0",
        source_id=source_id,
        scenes=scene_models,
        assets=assets,
        occurrences=occurrences,
        ocr_regions=regions,
        profile="content",
        status="complete",
        gaps=[],
    )


def empty_visual(*, source_id: str = "src-demo", duration: float = 60.0) -> VisualCatalogue:
    return make_visual([], source_id=source_id, duration=duration)


def sample_course_map(*, source_id: str = "src-demo", duration: float = 60.0) -> CourseMap:
    return CourseMap(
        schema_version="1.0",
        source_id=source_id,
        topics=[
            Topic(
                id="topic-1",
                title="课程开始",
                goal="建立主题",
                start_seconds=0.0,
                end_seconds=duration,
                evidence_ids=[],
                speculative=False,
            )
        ],
        relations=[],
        unverified_guesses=[],
    )


def claim(
    claim_id: str,
    text: str,
    evidence: list[str],
    *,
    qualifiers: list[str] | None = None,
    status: str = "draft",
) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=claim_id,
        text=text,
        evidence_ids=evidence,
        status=status,
        qualifiers=qualifiers or [],
        modality="both",
        provenance="source",
    )


def unit(
    unit_id: str,
    *,
    topic_id: str = "topic-1",
    segment_ids: list[str] | None = None,
    start: float = 0.0,
    end: float = 10.0,
    kind: str = "concept",
    claims: list[KnowledgeClaim],
    relations: list[KnowledgeRelation] | None = None,
) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=unit_id,
        topic_id=topic_id,
        segment_ids=segment_ids or ["seg-0001"],
        start_seconds=start,
        end_seconds=end,
        kind=kind,
        claims=claims,
        relations=relations or [],
    )


def lecture_fixture(*, duration: float = 300.0) -> tuple[TranscriptDocument, VisualCatalogue]:
    """5-minute synthetic lecture covering intro, example, procedure, recap."""

    transcript = make_transcript(
        [
            ("cap-001", 0.0, 60.0, "这不是自动词。先看定义。"),
            ("cap-002", 60.0, 120.0, "加热 3 分钟后观察变化。"),
            ("cap-003", 120.0, 180.0, "例如：把水倒入烧杯。"),
            ("cap-004", 180.0, 210.0, "步骤1 打开阀门。"),
            ("cap-005", 210.0, 240.0, "步骤2 然后记录读数。"),
            ("cap-006", 240.0, 300.0, "回顾：刚才的步骤必须按顺序完成。"),
        ],
        duration=duration,
    )
    visual = make_visual(
        [
            ("frame-001", 30.0, "scene-001"),
            ("frame-002", 90.0, "scene-001"),
            ("frame-003", 150.0, "scene-001"),
            ("frame-004", 200.0, "scene-001"),
            ("frame-005", 220.0, "scene-001"),
            ("frame-006", 270.0, "scene-001"),
        ],
        duration=duration,
        ocr=[("ocr-001", "frame-002", "3 分钟")],
    )
    return transcript, visual

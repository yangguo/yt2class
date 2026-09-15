"""Synthetic M3 fixtures. No live-model gold answers and no licensed media."""

from __future__ import annotations

from yt2class.domain.course_map import CourseMap, Topic, TopicRelation
from yt2class.domain.knowledge import (
    KnowledgeClaim,
    KnowledgeDocument,
    KnowledgeRelation,
    KnowledgeUnit,
    VisualCandidate,
)
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from tests.helpers.m2 import (
    claim,
    make_transcript,
    make_visual,
    unit,
)


def course_map(
    topics: list[tuple[str, str, float, float]],
    *,
    source_id: str = "src-demo",
    relations: list[tuple[str, str, str]] | None = None,
) -> CourseMap:
    return CourseMap(
        schema_version="1.0",
        source_id=source_id,
        topics=[
            Topic(
                id=item[0],
                title=item[1],
                goal=f"理解{item[1]}",
                start_seconds=item[2],
                end_seconds=item[3],
                evidence_ids=[],
            )
            for item in topics
        ],
        relations=[
            TopicRelation(from_topic_id=item[0], to_topic_id=item[1], kind=item[2])
            for item in relations or []
        ],
        unverified_guesses=[],
    )


def knowledge(*units: KnowledgeUnit, source_id: str = "src-demo") -> KnowledgeDocument:
    return KnowledgeDocument(schema_version="1.0", source_id=source_id, units=list(units))


def concept_unit(
    unit_id: str,
    claim_id: str,
    text: str,
    evidence: list[str],
    *,
    topic_id: str = "topic-1",
    start: float = 0.0,
    end: float = 10.0,
    kind: str = "concept",
    frames: list[str] | None = None,
    relations: list[KnowledgeRelation] | None = None,
    provenance: str = "source",
    modality: str = "both",
    qualifiers: list[str] | None = None,
) -> KnowledgeUnit:
    built = claim(claim_id, text, evidence, qualifiers=qualifiers)
    built = built.model_copy(update={"provenance": provenance, "modality": modality})
    candidates = [
        VisualCandidate(
            frame_id=frame_id,
            relevance=0.9,
            legibility=0.8,
            selection_reason="fixture candidate",
        )
        for frame_id in frames or []
    ]
    return unit(
        unit_id,
        topic_id=topic_id,
        start=start,
        end=end,
        kind=kind,
        claims=[built],
        relations=relations or [],
    ).model_copy(update={"visual_candidates": candidates})


def lecture_knowledge() -> tuple[KnowledgeDocument, CourseMap, TranscriptDocument, VisualCatalogue]:
    """A compact lecture covering concept, example, comparison, procedure, recap."""

    transcript = make_transcript(
        [
            ("cap-001", 0.0, 30.0, "这不是自动词。先看定义。"),
            ("cap-002", 30.0, 60.0, "例如：把水倒入烧杯。"),
            ("cap-003", 60.0, 90.0, "对比：自动词 versus 他动词。"),
            ("cap-004", 90.0, 120.0, "步骤1 打开阀门。如果温度低于 80 度则停止。"),
            ("cap-005", 120.0, 150.0, "步骤2 然后记录读数 3 分钟。"),
            ("cap-006", 150.0, 180.0, "回顾：刚才的步骤必须按顺序完成。"),
        ],
        duration=180.0,
    )
    visual = make_visual(
        [
            ("frame-001", 10.0, "scene-001"),
            ("frame-002", 40.0, "scene-001"),
            ("frame-003a", 70.0, "scene-001"),
            ("frame-003b", 80.0, "scene-001"),
            ("frame-004", 100.0, "scene-001"),
            ("frame-005", 130.0, "scene-001"),
            ("frame-006", 160.0, "scene-001"),
        ],
        duration=180.0,
        ocr=[
            ("ocr-001", "frame-001", "自动词 ではない"),
            ("ocr-002", "frame-004", "80 度"),
            ("ocr-003", "frame-005", "3 分钟"),
        ],
    )
    topics = course_map(
        [
            ("topic-def", "定义", 0.0, 30.0),
            ("topic-ex", "例子", 30.0, 60.0),
            ("topic-cmp", "对比", 60.0, 90.0),
            ("topic-proc", "步骤", 90.0, 150.0),
            ("topic-recap", "回顾", 150.0, 180.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-def",
            "claim-def",
            "这不是自动词。",
            ["cap-001", "frame-001"],
            topic_id="topic-def",
            start=0.0,
            end=30.0,
            frames=["frame-001"],
        ),
        concept_unit(
            "unit-ex",
            "claim-ex",
            "例如：把水倒入烧杯。",
            ["cap-002", "frame-002"],
            topic_id="topic-ex",
            start=30.0,
            end=60.0,
            kind="example",
            frames=["frame-002"],
        ),
        concept_unit(
            "unit-cmp",
            "claim-cmp",
            "对比：自动词 versus 他动词。",
            ["cap-003", "frame-003a", "frame-003b"],
            topic_id="topic-cmp",
            start=60.0,
            end=90.0,
            kind="comparison",
            frames=["frame-003a", "frame-003b"],
        ),
        KnowledgeUnit(
            id="unit-proc",
            topic_id="topic-proc",
            segment_ids=["seg-0001"],
            start_seconds=90.0,
            end_seconds=150.0,
            kind="procedure",
            claims=[
                KnowledgeClaim(
                    id="claim-step-1",
                    text="步骤1 打开阀门。如果温度低于 80 度则停止。",
                    evidence_ids=["cap-004", "frame-004"],
                    status="draft",
                    qualifiers=["温度低于80度"],
                    modality="both",
                    provenance="source",
                ),
                KnowledgeClaim(
                    id="claim-step-2",
                    text="步骤2 然后记录读数 3 分钟。",
                    evidence_ids=["cap-005", "frame-005"],
                    status="draft",
                    qualifiers=[],
                    modality="both",
                    provenance="source",
                ),
            ],
            relations=[
                KnowledgeRelation(from_id="claim-step-1", to_id="claim-step-2", kind="step_before"),
            ],
            visual_candidates=[
                VisualCandidate(
                    frame_id="frame-004",
                    relevance=0.9,
                    legibility=0.8,
                    selection_reason="step 1",
                ),
                VisualCandidate(
                    frame_id="frame-005",
                    relevance=0.85,
                    legibility=0.8,
                    selection_reason="step 2",
                ),
            ],
        ),
        concept_unit(
            "unit-recap",
            "claim-recap",
            "回顾：刚才的步骤必须按顺序完成。",
            ["cap-006", "frame-006"],
            topic_id="topic-recap",
            start=150.0,
            end=180.0,
            kind="recap",
            frames=["frame-006"],
        ),
    )
    return doc, topics, transcript, visual


def grounding_provider(*, verdict="supported", copy_verdict="supported"):
    """Scripted verdicts for pipeline tests, not an entailment implementation."""
    from yt2class.adapters.providers.base import FakeProvider
    from tests.helpers.m2 import frames_caps

    def respond(provider, request):
        payload = provider.last_payload or {}
        rows = []
        evidence = payload.get("evidence") or {}
        for claim in payload.get("claims", []):
            chosen = copy_verdict if claim["id"] == "copy" else verdict
            cited = list(claim.get("evidence_ids") or [])
            ids = [item for item in cited if str(evidence.get(item, "")).strip()]
            rows.append(
                dict(
                    claim_id=claim["id"],
                    verdict=chosen,
                    supporting_ids=ids if chosen == "supported" else [],
                    contradicting_ids=ids if chosen == "contradicted" else [],
                    reason="scripted fixture verdict",
                )
            )
        return {"verdicts": rows}
    return FakeProvider(frames_caps(), responder=respond)

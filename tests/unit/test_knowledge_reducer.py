from __future__ import annotations

from yt2class.domain.course_map import CourseMap, Topic
from yt2class.domain.knowledge import KnowledgeRelation
from yt2class.stages.reduce_knowledge import reduce_knowledge
from tests.helpers.m2 import claim, unit


def test_overlap_duplicates_merge_by_evidence_and_time():
    left = unit(
        "unit-a",
        start=0.0,
        end=20.0,
        claims=[claim("claim-a", "这是自动词", ["cap-001", "frame-001"])],
    )
    right = unit(
        "unit-b",
        segment_ids=["seg-0002"],
        start=10.0,
        end=30.0,
        claims=[claim("claim-b", "这是自动词", ["cap-001"])],
    )
    document = reduce_knowledge([left, right], source_id="src-demo")
    assert len(document.units) == 1
    merged = document.units[0]
    assert set(merged.segment_ids) == {"seg-0001", "seg-0002"}
    assert set(merged.claims[0].evidence_ids) == {"cap-001", "frame-001"}


def test_same_word_different_sense_is_not_merged():
    river = unit(
        "unit-bank-river",
        start=0.0,
        end=20.0,
        claims=[claim("claim-river", "bank", ["cap-001"], qualifiers=["river"])],
    )
    finance = unit(
        "unit-bank-money",
        start=5.0,
        end=25.0,
        claims=[claim("claim-money", "bank", ["cap-002"], qualifiers=["finance"])],
    )
    document = reduce_knowledge([river, finance], source_id="src-demo")
    assert len(document.units) == 2


def test_cross_segment_procedure_steps_are_linked_and_ordered():
    step1 = unit(
        "unit-step-1",
        segment_ids=["seg-0001"],
        start=0.0,
        end=20.0,
        kind="procedure",
        claims=[claim("claim-s1", "步骤1 打开阀门", ["cap-004"])],
    )
    step2 = unit(
        "unit-step-2",
        segment_ids=["seg-0002"],
        start=20.0,
        end=40.0,
        kind="procedure",
        claims=[claim("claim-s2", "步骤2 记录读数", ["cap-005"])],
    )
    document = reduce_knowledge([step1, step2], source_id="src-demo")
    kinds = [relation.kind for unit in document.units for relation in unit.relations]
    assert "step_before" in kinds
    assert any(
        relation.from_id == "unit-step-1" and relation.to_id == "unit-step-2"
        for item in document.units
        for relation in item.relations
    )


def test_recap_does_not_merge_into_original_concept():
    original = unit(
        "unit-def",
        start=0.0,
        end=20.0,
        kind="concept",
        claims=[claim("claim-def", "这不是自动词", ["cap-001"])],
    )
    recap = unit(
        "unit-recap",
        segment_ids=["seg-0002"],
        start=80.0,
        end=100.0,
        kind="recap",
        claims=[claim("claim-recap", "回顾：这不是自动词", ["cap-006"])],
    )
    document = reduce_knowledge([original, recap], source_id="src-demo")
    assert {item.kind for item in document.units} == {"concept", "recap"}
    assert any(relation.kind == "supports" for item in document.units for relation in item.relations)


def test_source_conflicts_are_kept_not_fused():
    yes = unit(
        "unit-yes",
        start=0.0,
        end=20.0,
        claims=[claim("claim-yes", "这是自动词", ["cap-001"])],
    )
    no = unit(
        "unit-no",
        start=10.0,
        end=30.0,
        claims=[claim("claim-no", "这不是自动词", ["cap-002"])],
    )
    document = reduce_knowledge([yes, no], source_id="src-demo")
    assert len(document.units) == 2
    notes = [item.note for unit in document.units for item in unit.uncertainty]
    assert any("source conflict" in note for note in notes)


def test_missing_procedure_step_is_recorded():
    first = unit(
        "unit-p1",
        start=0.0,
        end=10.0,
        kind="procedure",
        claims=[claim("claim-p1", "步骤1 开始", ["cap-1"])],
    )
    third = unit(
        "unit-p3",
        start=20.0,
        end=30.0,
        kind="procedure",
        claims=[claim("claim-p3", "步骤3 结束", ["cap-3"])],
        relations=[KnowledgeRelation(from_id="unit-p1", to_id="unit-p3", kind="step_before")],
    )
    document = reduce_knowledge([first, third], source_id="src-demo")
    assert any(
        "missing steps" in item.note for unit in document.units for item in unit.uncertainty
    )


def test_speculative_topics_cannot_become_source_claims():
    course_map = CourseMap(
        schema_version="1.0",
        source_id="src-demo",
        topics=[
            Topic(
                id="topic-guess",
                title="猜测",
                goal="未知",
                start_seconds=0.0,
                end_seconds=10.0,
                speculative=True,
            )
        ],
    )
    guessed = unit(
        "unit-guess",
        topic_id="topic-guess",
        claims=[claim("claim-guess", "可能是重点", ["cap-001"], status="draft")],
    )
    document = reduce_knowledge([guessed], source_id="src-demo", course_map=course_map)
    assert document.units[0].claims[0].status == "insufficient"
    assert "speculative-topic" in document.units[0].claims[0].qualifiers

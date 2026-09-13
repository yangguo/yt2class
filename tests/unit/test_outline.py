from __future__ import annotations

import pytest

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.course_map import OutlineBlock, Topic, forbid_speculative_source_claims
from yt2class.stages.outline import outline_course, partition_transcript, reduce_outline
from tests.helpers.m2 import empty_visual, frames_caps, make_transcript, make_visual


def _provider(structured=None, **kwargs) -> FakeProvider:
    return FakeProvider(frames_caps(), structured=structured, **kwargs)


def test_long_transcript_is_fully_scheduled():
    segments = [
        (f"cap-{index:03d}", float(index), float(index + 1), "主题内容。" * 80)
        for index in range(0, 40)
    ]
    transcript = make_transcript(segments, duration=40.0)
    blocks = partition_transcript(transcript, duration_seconds=40.0, max_block_chars=200)
    scheduled = [item for block in blocks for item in block.evidence_ids]
    assert [segment.id for segment in transcript.segments] == scheduled
    assert all(block.char_count <= 200 or len(block.evidence_ids) == 1 for block in blocks)


def test_start_and_end_topics_survive_reduce():
    transcript = make_transcript(
        [
            ("cap-start", 0.0, 20.0, "开始：字母あ。"),
            ("cap-mid", 20.0, 40.0, "中间内容。"),
            ("cap-end", 40.0, 60.0, "结束：总结本课。"),
        ],
        duration=60.0,
    )
    visual = make_visual([("frame-001", 10.0, "scene-001")], duration=60.0)
    from yt2class.adapters.providers.synthetic import course_responder

    provider = FakeProvider(frames_caps(), responder=course_responder)
    course_map = outline_course(
        transcript, visual, provider, source_id="src-demo", duration_seconds=60.0, max_block_chars=20
    )
    assert course_map.topics[0].start_seconds == 0.0
    assert course_map.topics[-1].end_seconds == 60.0
    assert "开始" in course_map.topics[0].title or "あ" in course_map.topics[0].title
    assert any("结束" in topic.title or "总结" in topic.title for topic in course_map.topics)


def test_no_transcript_yields_speculative_map_not_source_claims():
    transcript = make_transcript([], duration=45.0)
    visual = make_visual([("frame-001", 12.0, "scene-001")], duration=45.0)
    provider = FakeProvider(
        frames_caps(),
        structured={
            "topics": [
                {
                    "id": "topic-guess",
                    "title": "可能是板书",
                    "goal": "猜测主题",
                    "start_seconds": 0.0,
                    "end_seconds": 45.0,
                    "evidence_ids": ["frame-001"],
                    "speculative": False,
                }
            ]
        },
    )
    course_map = outline_course(
        transcript, visual, provider, source_id="src-demo", duration_seconds=45.0
    )
    assert course_map.topics
    assert all(topic.speculative for topic in course_map.topics)
    with pytest.raises(ValueError, match="speculative"):
        forbid_speculative_source_claims(
            course_map,
            [
                type(
                    "C",
                    (),
                    {
                        "id": "claim-1",
                        "topic_id": course_map.topics[0].id,
                        "provenance": "source",
                        "status": "supported",
                    },
                )()
            ],
        )


def test_conflicting_topics_are_dropped_by_reducer():
    block = OutlineBlock(id="block-0001", start_seconds=0.0, end_seconds=30.0, evidence_ids=["cap-001"])
    left = Topic(
        id="topic-a",
        title="这是自动词",
        goal="定义",
        start_seconds=0.0,
        end_seconds=30.0,
        evidence_ids=["cap-001"],
    )
    right = Topic(
        id="topic-b",
        title="这不是自动词",
        goal="否定定义",
        start_seconds=0.0,
        end_seconds=30.0,
        evidence_ids=["cap-001"],
    )
    course_map = reduce_outline(
        "src-demo",
        [(block, [left, right], [])],
        duration_seconds=30.0,
    )
    titles = [topic.title for topic in course_map.topics]
    assert titles.count("这是自动词") == 1
    assert "这不是自动词" not in titles
    assert any("conflicting" in guess for guess in course_map.unverified_guesses)


def test_out_of_range_refs_are_rejected():
    transcript = make_transcript([("cap-001", 0.0, 10.0, "开头。")], duration=10.0)
    visual = empty_visual(duration=10.0)
    provider = FakeProvider(
        frames_caps(),
        structured={
            "topics": [
                {
                    "id": "topic-bad-id",
                    "title": "越界引用",
                    "goal": "应被拒绝",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "evidence_ids": ["frame-missing"],
                    "speculative": False,
                },
                {
                    "id": "topic-bad-time",
                    "title": "越界时间",
                    "goal": "应被拒绝",
                    "start_seconds": 80.0,
                    "end_seconds": 90.0,
                    "evidence_ids": ["cap-001"],
                    "speculative": False,
                },
            ]
        },
    )
    course_map = outline_course(
        transcript, visual, provider, source_id="src-demo", duration_seconds=10.0
    )
    assert all(topic.id != "topic-bad-id" for topic in course_map.topics)
    assert all(topic.id != "topic-bad-time" for topic in course_map.topics)
    assert any("out-of-range" in guess for guess in course_map.unverified_guesses)


def test_reducer_drops_empty_blocks_and_keeps_neighbors():
    first = OutlineBlock(id="block-0001", start_seconds=0.0, end_seconds=20.0, evidence_ids=["cap-1"])
    middle = OutlineBlock(
        id="block-0002",
        start_seconds=20.0,
        end_seconds=40.0,
        evidence_ids=["cap-2"],
        dropped=True,
        drop_reason="empty outline",
    )
    last = OutlineBlock(id="block-0003", start_seconds=40.0, end_seconds=60.0, evidence_ids=["cap-3"])
    course_map = reduce_outline(
        "src-demo",
        [
            (
                first,
                [
                    Topic(
                        id="topic-start",
                        title="开始",
                        goal="引入",
                        start_seconds=0.0,
                        end_seconds=20.0,
                        evidence_ids=["cap-1"],
                    )
                ],
                [],
            ),
            (middle, [], ["empty outline"]),
            (
                last,
                [
                    Topic(
                        id="topic-end",
                        title="结束",
                        goal="收束",
                        start_seconds=40.0,
                        end_seconds=60.0,
                        evidence_ids=["cap-3"],
                    )
                ],
                [],
            ),
        ],
        duration_seconds=60.0,
    )
    assert [topic.id for topic in course_map.topics] == ["topic-start", "topic-end"]
    assert any("dropped block-0002" in guess for guess in course_map.unverified_guesses)
    assert course_map.relations[0].kind == "follows"


def test_non_speculative_topic_must_cite_block_local_evidence():
    transcript = make_transcript(
        [("cap-early", 0.0, 10.0, "开头。"), ("cap-late", 40.0, 50.0, "结尾。")],
        duration=60.0,
    )
    visual = make_visual([("frame-late", 45.0, "scene-001")], duration=60.0)
    provider = FakeProvider(
        frames_caps(),
        structured={
            "topics": [
                {
                    "id": "topic-stolen",
                    "title": "引用后段证据",
                    "goal": "应被拒绝",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "evidence_ids": ["cap-late"],
                    "speculative": False,
                },
                {
                    "id": "topic-empty",
                    "title": "无证据主题",
                    "goal": "应被拒绝",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "evidence_ids": [],
                    "speculative": False,
                },
            ]
        },
    )
    course_map = outline_course(
        transcript, visual, provider, source_id="src-demo", duration_seconds=60.0, max_block_chars=20
    )
    assert all(topic.id not in {"topic-stolen", "topic-empty"} for topic in course_map.topics)

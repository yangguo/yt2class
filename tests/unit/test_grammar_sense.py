from __future__ import annotations

from yt2class.stages.grammar_sense import (
    learner_page_title,
    pick_summary_unit,
    sense_heading,
    summary_bullet_for_sense,
    usage_topics,
)
from tests.helpers.m3 import concept_unit, course_map, knowledge


def test_usage_topics_skip_connective_head():
    topics = course_map(
        [
            ("topic-conn", "接续・名词＋につき", 0.0, 20.0),
            ("topic-u1", "用法1・原因", 20.0, 40.0),
            ("topic-u2", "比例単位", 40.0, 60.0),
            ("topic-u3", "用法3・について", 60.0, 80.0),
        ]
    )
    usage = usage_topics(topics)
    assert [topic.id for topic in usage] == ["topic-u1", "topic-u2", "topic-u3"]
    assert learner_page_title(
        topic_id="topic-u2",
        course_map=topics,
        fallback_title="一個につき五百円",
    ) == "用法二：比例・単位"


def test_summary_prefers_rate_example_for_proportional_sense():
    topics = course_map([("topic-u2", "比例単位", 40.0, 60.0)])
    topic = usage_topics(topics)[0]
    doc = knowledge(
        concept_unit(
            "unit-concept",
            "claim-c",
            "比例说明。",
            ["cap-c"],
            topic_id="topic-u2",
            kind="concept",
        ),
        concept_unit(
            "unit-rate",
            "claim-rate",
            "1時間1500円の駐車場。",
            ["cap-rate"],
            topic_id="topic-u2",
            kind="example",
        ),
    )
    unit = pick_summary_unit(topic, doc, course_map=topics)
    assert unit is not None
    assert unit.id == "unit-rate"


def test_summary_bullet_includes_sense_heading():
    line = summary_bullet_for_sense(1, "改装工事につき休業。")
    assert line.startswith("用法一：原因・理由")
    assert "休業" in line
    assert sense_heading(3).startswith("用法三")

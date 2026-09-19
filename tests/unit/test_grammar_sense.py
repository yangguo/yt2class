from __future__ import annotations

from yt2class.stages.grammar_sense import (
    learner_page_title,
    pick_summary_unit,
    sense_heading,
    sense_ordinal,
    summary_bullet_for_sense,
    topic_for_sense_ordinal,
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


def test_english_intro_overview_do_not_consume_usage_ordinals():
    topics = course_map(
        [
            ("topic-intro", "Lesson Intro", 0.0, 10.0),
            ("topic-overview", "Grammar Overview", 10.0, 20.0),
            ("topic-chat", "Small talk warm-up", 20.0, 30.0),
            ("topic-conn", "接续", 30.0, 40.0),
            ("topic-u1", "用法1", 40.0, 50.0),
            ("topic-u2", "用法2", 50.0, 60.0),
            ("topic-u3", "用法3", 60.0, 70.0),
        ]
    )
    assert sense_ordinal("topic-u1", topics) == 1
    assert sense_ordinal("topic-u2", topics) == 2
    assert sense_ordinal("topic-u3", topics) == 3
    assert learner_page_title(
        topic_id="topic-u2",
        course_map=topics,
        fallback_title="meta",
    ) == "用法二：比例・単位"
    assert topic_for_sense_ordinal(topics, 2).id == "topic-u2"


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


def test_rate_summary_from_knowledge_when_usage2_not_on_slides():
    from yt2class.stages.edit_deck import edit_deck
    from yt2class.adapters.providers.base import FakeProvider
    from tests.helpers.m2 import frames_caps, make_transcript, make_visual

    topics = course_map(
        [
            ("topic-intro", "Introduction", 0.0, 15.0),
            ("topic-u1", "用法1", 15.0, 35.0),
            ("topic-u2", "用法2", 35.0, 55.0),
            ("topic-u3", "用法3", 55.0, 75.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-u1",
            "claim-u1",
            "改装工事につき一時休業。",
            ["cap-u1"],
            topic_id="topic-u1",
            start=15.0,
            end=35.0,
            kind="example",
        ),
        concept_unit(
            "unit-u2",
            "claim-u2",
            "一個につき五百円です。",
            ["cap-u2"],
            topic_id="topic-u2",
            start=35.0,
            end=55.0,
            kind="example",
        ),
        concept_unit(
            "unit-u3",
            "claim-u3",
            "自衛隊について発言。",
            ["cap-u3"],
            topic_id="topic-u3",
            start=55.0,
            end=75.0,
            kind="example",
        ),
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([], duration=75.0),
        visual=make_visual([], duration=75.0),
        provider=FakeProvider(frames_caps()),
        target_pages=6,
        max_pages=8,
    )
    summary = next(page for page in plan.pages if page.type == "summary")
    joined = " ".join(summary.body_points)
    assert "用法二" in joined
    assert "五百円" in joined or "一個につき" in joined


def test_summary_bullet_includes_sense_heading():
    line = summary_bullet_for_sense(1, "改装工事につき休業。")
    assert line.startswith("用法一：原因・理由")
    assert "休業" in line
    assert sense_heading(3).startswith("用法三")

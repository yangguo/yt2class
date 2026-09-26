from __future__ import annotations

from yt2class.stages.grammar_sense import (
    learner_page_title,
    pick_summary_unit,
    sense_heading,
    sense_ordinal,
    sense_topic_ids,
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


def test_non_tsuki_course_does_not_force_japanese_sense_headings():
    topics = course_map(
        [
            ("topic-cause", "Root cause analysis", 0.0, 20.0),
            ("topic-rate", "Rate and proportion", 20.0, 40.0),
            ("topic-about", "About the theorem", 40.0, 60.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-cause",
            "claim-cause",
            "A fault causes the system to stop.",
            ["cap-cause"],
            topic_id="topic-cause",
        ),
        concept_unit(
            "unit-rate",
            "claim-rate",
            "The rate is 500 requests per second.",
            ["cap-rate"],
            topic_id="topic-rate",
        ),
        concept_unit(
            "unit-about",
            "claim-about",
            "This section is about the theorem.",
            ["cap-about"],
            topic_id="topic-about",
        ),
    )

    assert sense_topic_ids(topics, doc) == []
    for topic in topics.topics:
        assert learner_page_title(
            topic_id=topic.id,
            course_map=topics,
            fallback_title=topic.title,
            knowledge=doc,
        ) == topic.title


def test_english_intro_overview_do_not_consume_usage_ordinals():
    topics = course_map(
        [
            ("topic-intro", "Lesson Intro", 0.0, 10.0),
            ("topic-overview", "Grammar Overview", 10.0, 20.0),
            ("topic-chat", "Small talk warm-up", 20.0, 30.0),
            ("topic-conn", "接续・名词＋につき", 30.0, 40.0),
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
    topics = course_map([("topic-u2", "～につき：比例単位", 40.0, 60.0)])
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
    assert "用法二：比例・単位（每个单位）" in joined
    assert "claim-u2" in summary.claim_ids


def test_topic_kind_inferred_from_knowledge_when_title_is_opaque():
    topics = course_map(
        [
            ("topic-block-0001-04", "讲解段B", 99.0, 135.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-rate",
            "claim-rate",
            "駐車場は1時間1500円、一個につき五百円の販売。",
            ["cap-rate"],
            topic_id="topic-block-0001-04",
            start=99.0,
            end=120.0,
            kind="example",
        ),
    )
    topic = topics.topics[0]
    assert topic_for_sense_ordinal(topics, 2, knowledge=doc).id == "topic-block-0001-04"
    assert sense_ordinal("topic-block-0001-04", topics, knowledge=doc) == 2


def test_rate_content_and_summary_when_only_u1_u3_would_fit_budget():
    from yt2class.stages.edit_deck import edit_deck
    from yt2class.adapters.providers.base import FakeProvider
    from tests.helpers.m2 import frames_caps, make_transcript, make_visual

    topics = course_map(
        [
            ("topic-intro", "Lesson Overview", 0.0, 10.0),
            ("topic-block-0001-02", "讲解段A", 10.0, 50.0),
            ("topic-block-0001-04", "讲解段B", 99.0, 135.0),
            ("topic-block-0001-06", "讲解段C", 135.0, 170.0),
        ]
    )
    fillers = [
        concept_unit(
            f"unit-fill-{index}",
            f"claim-fill-{index}",
            f"补充说明{index}。",
            [f"cap-fill-{index}"],
            topic_id="topic-block-0001-02",
            start=10.0 + index,
            end=11.0 + index,
        )
        for index in range(8)
    ]
    doc = knowledge(
        *fillers,
        concept_unit(
            "unit-cause",
            "claim-cause",
            "改装工事につき一時休業いたします。",
            ["cap-cause"],
            topic_id="topic-block-0001-02",
            start=20.0,
            end=45.0,
            kind="example",
        ),
        concept_unit(
            "unit-rate",
            "claim-rate",
            "一個につき五百円です。",
            ["cap-rate"],
            topic_id="topic-block-0001-04",
            start=99.0,
            end=120.0,
            kind="example",
        ),
        concept_unit(
            "unit-about",
            "claim-about",
            "自衛隊について発言した。",
            ["cap-about"],
            topic_id="topic-block-0001-06",
            start=140.0,
            end=160.0,
            kind="example",
        ),
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([], duration=170.0),
        visual=make_visual([], duration=170.0),
        provider=FakeProvider(frames_caps()),
        target_pages=5,
        max_pages=5,
    )
    content_titles = [page.title for page in plan.pages if page.type == "content"]
    assert any("用法二" in title for title in content_titles)
    summary = next(page for page in plan.pages if page.type == "summary")
    joined = " ".join(summary.body_points)
    assert "用法二：比例・単位（每个单位）" in joined
    assert "claim-rate" in summary.claim_ids


def test_summary_bullet_includes_sense_heading():
    line = summary_bullet_for_sense(1, "改装工事につき休業。")
    assert line.startswith("用法一：原因・理由")
    assert "休業" in line
    assert sense_heading(3).startswith("用法三")

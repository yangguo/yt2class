"""Plan11: ～につき decks keep a 接续 page and do not stack 用法一."""

from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.stages.edit_deck import (
    apply_model_organization,
    build_deterministic_plan,
    edit_deck,
    score_candidates,
    select_candidates,
)
from yt2class.stages.grammar_sense import (
    CONNECTIVE_HEADING,
    learner_page_title,
    sense_ordinal,
    topic_for_connective,
)
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge


def _plan11_topics():
    return course_map(
        [
            ("topic-intro", "Introduction: what goes before につき", 0.0, 15.0),
            ("topic-cause", "用法1・原因", 20.0, 90.0),
            ("topic-cause-b", "讲解段B", 90.0, 100.0),
            ("topic-cause-c", "讲解段C", 100.0, 110.0),
            ("topic-cause-d", "讲解段D", 110.0, 120.0),
            ("topic-cause-e", "讲解段E", 120.0, 130.0),
            ("topic-cause-f", "讲解段F", 130.0, 140.0),
            ("topic-rate", "用法2・比例", 140.0, 160.0),
            ("topic-about", "用法3・について", 160.0, 175.0),
            ("topic-conn", "接续・名词＋につき", 175.0, 190.0),
        ]
    )


def _plan11_knowledge():
    cause_examples = [
        concept_unit(
            "unit-cause-1",
            "claim-cause-1",
            "改装工事につき一時休業。",
            ["cap-cause-1"],
            topic_id="topic-cause",
            start=20.0,
            end=35.0,
            kind="example",
        ),
        concept_unit(
            "unit-cause-2",
            "claim-cause-2",
            "大雨につき試合は中止。",
            ["cap-cause-2"],
            topic_id="topic-cause",
            start=36.0,
            end=50.0,
            kind="example",
        ),
        concept_unit(
            "unit-cause-meta",
            "claim-cause-meta",
            "老师过渡：ASR→误听为2月。原因说明。",
            ["cap-cause-meta"],
            topic_id="topic-cause",
            start=51.0,
            end=60.0,
            kind="concept",
        ),
    ]
    extra_causes = [
        concept_unit(
            f"unit-cause-{label}",
            f"claim-cause-{label}",
            f"改装工事につき補足{label}。",
            [f"cap-cause-{label}"],
            topic_id=f"topic-cause-{label}",
            start=90.0 + offset,
            end=98.0 + offset,
            kind="example",
        )
        for offset, label in enumerate(("b", "c", "d", "e", "f"))
    ]
    return knowledge(
        *cause_examples,
        *extra_causes,
        concept_unit(
            "unit-rate",
            "claim-rate",
            "駐車場は1時間につき1500円です。",
            ["cap-rate"],
            topic_id="topic-rate",
            start=140.0,
            end=155.0,
            kind="example",
        ),
        concept_unit(
            "unit-about",
            "claim-about",
            "自衛隊について発言した。",
            ["cap-about"],
            topic_id="topic-about",
            start=160.0,
            end=172.0,
            kind="example",
        ),
        concept_unit(
            "unit-conn",
            "claim-conn",
            "名詞／数量詞＋につき。",
            ["cap-conn"],
            topic_id="topic-conn",
            start=176.0,
            end=188.0,
            kind="example",
        ),
    )


def test_plan11_cause_crowd_keeps_connective_and_caps_usage_one():
    topics = _plan11_topics()
    doc = _plan11_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([], duration=190.0),
        visual=make_visual([], duration=190.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
    )
    content = [page for page in plan.pages if page.type == "content"]
    titles = [page.title for page in content]
    usage_one = [title for title in titles if title.startswith("用法一")]
    assert any(title == CONNECTIVE_HEADING for title in titles), titles
    assert 1 <= len(usage_one) <= 2, titles
    assert any(title.startswith("用法二：比例・単位") for title in titles), titles
    assert any(title.startswith("用法三") for title in titles), titles
    usage_one_pages = [page for page in content if page.title.startswith("用法一")]
    usage_one_body = " ".join(point for page in usage_one_pages for point in page.body_points)
    assert "休業" in usage_one_body or "中止" in usage_one_body
    assert "ASR" not in usage_one_body
    assert all("ASR" not in page.notes for page in content)
    connective = next(page for page in content if page.title == CONNECTIVE_HEADING)
    assert "mandatory-connective lock" in connective.selection_reason
    summary = next(page for page in plan.pages if page.type == "summary")
    joined = " ".join(summary.body_points)
    assert "用法一" in joined
    assert "用法二" in joined
    assert "用法三" in joined
    assert "1500" in joined or "1時間" in joined


def test_connective_gloss_does_not_consume_cause_ordinal():
    topics = course_map(
        [
            ("topic-conn", "Introduction: what goes before につき", 0.0, 20.0),
            ("topic-u1", "用法1・原因", 20.0, 40.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-conn",
            "claim-conn",
            "名詞／数量詞＋につき。",
            ["cap-conn"],
            topic_id="topic-conn",
            start=1.0,
            end=15.0,
            kind="example",
        ),
        concept_unit(
            "unit-cause",
            "claim-cause",
            "改装工事につき一時休業。",
            ["cap-cause"],
            topic_id="topic-u1",
            start=21.0,
            end=35.0,
            kind="example",
        ),
    )
    assert topic_for_connective(topics, doc).id == "topic-conn"
    assert sense_ordinal("topic-conn", topics, knowledge=doc) is None
    assert sense_ordinal("topic-u1", topics, knowledge=doc) == 1
    assert (
        learner_page_title(
            topic_id="topic-conn",
            course_map=topics,
            fallback_title="meta",
            knowledge=doc,
        )
        == CONNECTIVE_HEADING
    )


def test_rate_topic_noun_title_stays_usage_two_not_connective():
    topics = course_map(
        [
            ("topic-rate", "名词＋につき导入", 40.0, 80.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-rate",
            "claim-rate",
            "駐車場は1時間につき1500円です。",
            ["cap-rate"],
            topic_id="topic-rate",
            start=40.0,
            end=60.0,
            kind="example",
        ),
    )
    assert topic_for_connective(topics, doc) is None
    assert (
        learner_page_title(
            topic_id="topic-rate",
            course_map=topics,
            fallback_title="meta",
            knowledge=doc,
        )
        == "用法二：比例・単位"
    )


def test_connective_heading_locked_against_model_overlay():
    topics = _plan11_topics()
    doc = _plan11_knowledge()
    visual = make_visual([], duration=190.0)
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, omissions = select_candidates(
        scored,
        target_pages=8,
        max_pages=10,
        knowledge=doc,
        course_map=topics,
    )
    fallback = build_deterministic_plan(
        selected,
        omissions=omissions,
        source_id="src-demo",
        target_pages=8,
        max_pages=10,
        order="chronological",
        course_map=topics,
        knowledge=doc,
    )
    content = next(page for page in fallback.pages if page.title == CONNECTIVE_HEADING)
    structured = {
        "pages": [
            {
                **page.model_dump(mode="json"),
                "title": "停车场例子",
                "body_points": ["改写"],
            }
            if page.id == content.id
            else page.model_dump(mode="json")
            for page in fallback.pages
        ],
        "omissions": [],
    }
    merged = apply_model_organization(
        fallback,
        structured,
        allowed_claim_ids={claim.id for claim in doc.iter_claims()},
        allowed_frame_ids=set(),
        order="chronological",
    )
    page = next(item for item in merged.pages if item.id == content.id)
    assert page.title == CONNECTIVE_HEADING

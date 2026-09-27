"""Plan12: attachment units filed under 用法一 still produce a 接续 page, in sense order."""

from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.grammar_sense import CONNECTIVE_HEADING, text_is_connective_attachment
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge


def test_attachment_patterns_are_not_cause_or_rate_sentences():
    assert text_is_connective_attachment("接续：名詞／数量詞＋につき。")
    assert text_is_connective_attachment("What goes before つき is a noun.")
    assert text_is_connective_attachment("接続は名詞＋につき。")
    assert not text_is_connective_attachment("改装工事につき一時休業。")
    assert not text_is_connective_attachment("駐車場は1時間につき1500円です。")


def test_plan12_empty_intro_attachment_under_cause_locks_connective_before_senses():
    topics = course_map(
        [
            ("topic-intro", "Introduction: what goes before につき", 0.0, 15.0),
            ("topic-rate", "用法2・比例", 20.0, 40.0),
            ("topic-about", "用法3・について", 40.0, 70.0),
            ("topic-cause", "用法1・原因", 80.0, 150.0),
            ("topic-cause-b", "讲解段B", 150.0, 160.0),
            ("topic-cause-c", "讲解段C", 160.0, 170.0),
            ("topic-cause-d", "讲解段D", 170.0, 180.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-rate",
            "claim-rate",
            "駐車場は1時間につき1500円です。",
            ["cap-rate"],
            topic_id="topic-rate",
            start=20.0,
            end=35.0,
            kind="example",
        ),
        concept_unit(
            "unit-about",
            "claim-about",
            "自衛隊について発言した。",
            ["cap-about"],
            topic_id="topic-about",
            start=42.0,
            end=60.0,
            kind="example",
        ),
        concept_unit(
            "unit-attach",
            "claim-attach",
            "What goes before つき：名詞／数量詞＋につき。",
            ["cap-attach"],
            topic_id="topic-cause",
            start=82.0,
            end=95.0,
            kind="concept",
        ),
        concept_unit(
            "unit-cause-1",
            "claim-cause-1",
            "改装工事につき一時休業。",
            ["cap-cause-1"],
            topic_id="topic-cause",
            start=100.0,
            end=115.0,
            kind="example",
        ),
        concept_unit(
            "unit-cause-2",
            "claim-cause-2",
            "大雨につき試合は中止。",
            ["cap-cause-2"],
            topic_id="topic-cause",
            start=116.0,
            end=130.0,
            kind="example",
        ),
        *[
            concept_unit(
                f"unit-cause-{label}",
                f"claim-cause-{label}",
                f"改装工事につき補足{label}。",
                [f"cap-cause-{label}"],
                topic_id=f"topic-cause-{label}",
                start=150.0 + offset,
                end=158.0 + offset,
                kind="example",
            )
            for offset, label in enumerate(("b", "c", "d"))
        ],
    )
    assert not any(unit.topic_id == "topic-intro" for unit in doc.units)
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([], duration=180.0),
        visual=make_visual([], duration=180.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
        order="chronological",
    )
    titles = [page.title for page in plan.pages]
    content = [page for page in plan.pages if page.type == "content"]
    usage_one = [page for page in content if page.title.startswith("用法一")]
    connective = [page for page in content if page.title == CONNECTIVE_HEADING]
    assert len(connective) >= 1, titles
    assert "mandatory-connective lock" in connective[0].selection_reason
    assert "claim-attach" in connective[0].claim_ids
    assert 1 <= len(usage_one) <= 2, titles
    usage_one_body = " ".join(point for page in usage_one for point in page.body_points)
    assert "休業" in usage_one_body or "中止" in usage_one_body
    assert any(page.title.startswith("用法二：比例・単位") for page in content)
    assert any(page.title.startswith("用法三") for page in content)

    def indexes(prefix: str) -> list[int]:
        return [index for index, title in enumerate(titles) if title.startswith(prefix)]

    conn_at = indexes("接续")
    cause_at = indexes("用法一")
    rate_at = indexes("用法二")
    about_at = indexes("用法三")
    assert conn_at and cause_at and rate_at and about_at
    assert max(conn_at) < min(cause_at)
    assert max(cause_at) < min(rate_at)
    assert max(rate_at) < min(about_at)
    assert plan.pages[0].type == "cover"
    assert plan.pages[-1].type == "summary"
    summary = plan.pages[-1]
    joined = " ".join(summary.body_points)
    body_claim_ids = {
        claim_id
        for page in plan.pages
        if page.type in {"content", "quiz"}
        for claim_id in page.claim_ids
    }
    assert summary.claim_ids
    assert set(summary.claim_ids) <= body_claim_ids
    assert "用法一：原因・理由（公告等）" in joined
    assert "用法二：比例・単位（每个单位）" in joined
    assert "用法三：关于" in joined

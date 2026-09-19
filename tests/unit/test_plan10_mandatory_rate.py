from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeUnit
from yt2class.stages.edit_deck import build_deterministic_plan, edit_deck, score_candidates, select_candidates
from tests.helpers.m2 import claim, frames_caps, make_transcript, make_visual, unit
from tests.helpers.m3 import course_map, knowledge


def _rate_unit(
    unit_id: str,
    claim_id: str,
    text: str,
    *,
    start: float,
    kind: str = "example",
    status: str = "insufficient",
) -> KnowledgeUnit:
    built = claim(claim_id, text, ["cap-rate"], qualifiers=["pending_review"])
    built = built.model_copy(update={"status": status})
    return unit(
        unit_id,
        topic_id="topic-block-0001-04",
        start=start,
        end=start + 12.0,
        kind=kind,
        claims=[built],
    )


def test_plan10_connective_title_rate_topic_still_gets_usage_two_content_page():
    topics = course_map(
        [
            ("topic-intro", "Introduction", 0.0, 20.0),
            ("topic-block-0001-02", "用法1・原因", 20.0, 95.0),
            ("topic-block-0001-04", "名词＋につき导入", 99.0, 135.0),
            ("topic-block-0001-06", "用法3・について", 135.0, 170.0),
        ]
    )
    cause_units = [
        unit(
            f"unit-cause-{index}",
            topic_id="topic-block-0001-02",
            start=25.0 + index,
            end=26.0 + index,
            kind="concept" if index % 2 else "example",
            claims=[
                KnowledgeClaim(
                    id=f"claim-cause-{index}",
                    text=f"改装工事につき補足{index}。",
                    evidence_ids=[f"cap-cause-{index}"],
                    status="supported",
                )
            ],
        )
        for index in range(4)
    ]
    rate_units = [
        _rate_unit(
            f"unit-rate-{index}",
            f"claim-rate-{index}",
            f"駐車場は1時間につき{1500 + index}円です。",
            start=99.0 + index * 3,
            status="insufficient" if index else "draft",
        )
        for index in range(7)
    ]
    about_unit = unit(
        "unit-about",
        topic_id="topic-block-0001-06",
        start=140.0,
        end=160.0,
        kind="example",
        claims=[
            KnowledgeClaim(
                id="claim-about",
                text="自衛隊について発言した。",
                evidence_ids=["cap-about"],
                status="supported",
            )
        ],
    )
    doc = knowledge(*cause_units, *rate_units, about_unit)
    visual = make_visual([], duration=170.0)
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, _ = select_candidates(
        scored,
        target_pages=8,
        max_pages=10,
        knowledge=doc,
        course_map=topics,
    )
    rate_row = next(
        (item for item in selected if item.topic_id == "topic-block-0001-04"),
        None,
    )
    assert rate_row is not None, "rate topic must be selected"
    assert "mandatory-sense lock" in rate_row.selection_reason
    plan = build_deterministic_plan(
        selected,
        omissions=[],
        source_id="src-demo",
        target_pages=8,
        max_pages=10,
        order="chronological",
        course_map=topics,
        knowledge=doc,
    )
    usage_two = [
        page.title
        for page in plan.pages
        if page.type == "content" and page.title.startswith("用法二：比例・単位")
    ]
    assert usage_two, f"expected 用法二 content page, got {[p.title for p in plan.pages if p.type=='content']}"


def test_edit_deck_plan10_end_to_end():
    topics = course_map(
        [
            ("topic-intro", "Introduction", 0.0, 20.0),
            ("topic-block-0001-02", "用法1", 20.0, 95.0),
            ("topic-block-0001-04", "名词＋につき", 99.0, 135.0),
            ("topic-block-0001-06", "用法3", 135.0, 170.0),
        ]
    )
    doc = knowledge(
        *[
            _rate_unit(
                f"unit-rate-{index}",
                f"claim-rate-{index}",
                f"1時間につき{1500 + index}円の駐車場。",
                start=99.0 + index,
                status="insufficient",
            )
            for index in range(7)
        ],
        unit(
            "unit-cause",
            topic_id="topic-block-0001-02",
            start=30.0,
            end=50.0,
            kind="example",
            claims=[
                KnowledgeClaim(
                    id="claim-cause",
                    text="改装工事につき一時休業。",
                    evidence_ids=["cap-cause"],
                    status="supported",
                )
            ],
        ),
        unit(
            "unit-about",
            topic_id="topic-block-0001-06",
            start=140.0,
            end=160.0,
            kind="example",
            claims=[
                KnowledgeClaim(
                    id="claim-about",
                    text="自衛隊について発言。",
                    evidence_ids=["cap-about"],
                    status="supported",
                )
            ],
        ),
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([], duration=170.0),
        visual=make_visual([], duration=170.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
    )
    assert any(
        page.type == "content" and page.title.startswith("用法二：比例・単位")
        for page in plan.pages
    )

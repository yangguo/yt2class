from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.editorial import PageIntent
from yt2class.stages.bind_spec import _bind_summary
from yt2class.stages.edit_deck import (
    apply_model_organization,
    build_deterministic_plan,
    edit_deck,
    score_candidates,
    select_candidates,
)
from yt2class.domain.knowledge import KnowledgeClaim
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge


def _slide_claim(claim_id: str, text: str) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=claim_id,
        text=text,
        evidence_ids=["cap-1"],
        status="draft",
        provenance="source",
    )


def test_knowledge_only_rate_topic_gets_usage_two_content_page():
    topics = course_map(
        [
            ("topic-intro", "Lesson Overview", 0.0, 20.0),
            ("topic-block-0001-02", "讲解段A", 20.0, 90.0),
            ("topic-block-0001-06", "讲解段C", 140.0, 170.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-cause",
            "claim-cause",
            "改装工事につき一時休業。",
            ["cap-cause"],
            topic_id="topic-block-0001-02",
            start=25.0,
            end=50.0,
            kind="example",
        ),
        concept_unit(
            "unit-rate",
            "claim-rate",
            "駐車場は1時間につき1500円です。",
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
            start=145.0,
            end=165.0,
            kind="example",
        ),
    )
    visual = make_visual([], duration=170.0)
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, _ = select_candidates(
        scored,
        target_pages=6,
        max_pages=8,
        knowledge=doc,
        course_map=topics,
    )
    rate_pages = [
        item
        for item in selected
        if item.topic_id == "topic-block-0001-04" or item.unit_id == "unit-rate"
    ]
    assert rate_pages, "rate sense unit must be force-selected"
    plan = build_deterministic_plan(
        selected,
        omissions=[],
        source_id="src-demo",
        target_pages=6,
        max_pages=8,
        order="chronological",
        course_map=topics,
        knowledge=doc,
    )
    content_titles = [page.title for page in plan.pages if page.type == "content"]
    assert any(title.startswith("用法二：比例・単位") for title in content_titles)


def test_llm_overlay_cannot_replace_fixed_sense_titles():
    topics = course_map([("topic-u2", "用法2", 40.0, 60.0)])
    doc = knowledge(
        concept_unit(
            "unit-rate",
            "claim-rate",
            "一個につき五百円です。",
            ["cap-rate"],
            topic_id="topic-u2",
            start=40.0,
            end=55.0,
            kind="example",
        ),
    )
    visual = make_visual([], duration=60.0)
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, omissions = select_candidates(
        scored,
        target_pages=5,
        max_pages=6,
        knowledge=doc,
        course_map=topics,
    )
    fallback = build_deterministic_plan(
        selected,
        omissions=omissions,
        source_id="src-demo",
        target_pages=5,
        max_pages=6,
        order="chronological",
        course_map=topics,
        knowledge=doc,
    )
    content = next(page for page in fallback.pages if page.type == "content")
    structured = {
        "pages": [
            {
                **page.model_dump(mode="json"),
                "title": "駐車場の例文だけ",
                "body_points": ["五百円の例"],
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
    assert page.title.startswith("用法二：比例・単位")


def test_bind_summary_uses_editorial_body_points_for_display_bullets():
    page = PageIntent(
        id="intent-summary",
        type="summary",
        title="本课总结",
        claim_ids=["claim-rate"],
        frame_ids=[],
        notes="",
        selection_reason="summary",
        quality_label="draft",
        body_points=["用法二：比例・単位：1時間につき1500円。"],
    )
    claims = {
        "claim-rate": _slide_claim("claim-rate", "1時間につき1500円。"),
    }
    bound = _bind_summary(page, claims=claims)[0]
    assert bound.bullets == ["用法二：比例・単位：1時間につき1500円。"]


def test_edit_deck_plan9_style_opaque_blocks(tmp_path):
    del tmp_path
    topics = course_map(
        [
            ("topic-intro", "Introduction", 0.0, 15.0),
            ("topic-block-0001-02", "讲解段A", 15.0, 95.0),
            ("topic-block-0001-06", "讲解段C", 135.0, 170.0),
        ]
    )
    doc = knowledge(
        concept_unit(
            "unit-cause",
            "claim-cause",
            "改装工事につき一時休業。",
            ["cap-cause"],
            topic_id="topic-block-0001-02",
            start=20.0,
            end=40.0,
            kind="example",
        ),
        concept_unit(
            "unit-rate",
            "claim-rate",
            "駐車場は1時間につき1500円です。",
            ["cap-rate"],
            topic_id="topic-block-0001-04",
            start=99.0,
            end=120.0,
            kind="example",
        ),
        concept_unit(
            "unit-about",
            "claim-about",
            "自衛隊について発言。",
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
        provider=FakeProvider(frames_caps(), structured={"pages": []}),
        target_pages=8,
        max_pages=10,
    )
    content_titles = [page.title for page in plan.pages if page.type == "content"]
    assert any(t.startswith("用法二：比例・単位") for t in content_titles)
    summary = next(page for page in plan.pages if page.type == "summary")
    rate_line = next(line for line in summary.body_points if line.startswith("用法二"))
    assert "比例・単位" in rate_line
    assert "每个单位" in rate_line
    assert "1500" not in rate_line and "1時間" not in rate_line

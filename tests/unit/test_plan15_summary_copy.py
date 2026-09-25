from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.slide_spec_v3 import SlideClaim
from yt2class.stages.bind_spec import _bind_summary
from yt2class.stages.edit_deck import PageCandidate, _content_page, _summary_page, edit_deck
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit as make_unit, course_map, knowledge


def _summary_plan(*, include_about: bool = True):
    topic_rows = [
        ("topic-intro", "Introduction: what goes before につき", 0.0, 10.0),
        ("topic-cause", "用法1・原因", 10.0, 20.0),
        ("topic-rate", "用法2・比例", 20.0, 30.0),
    ]
    units = [
        make_unit(
            "unit-conn", "claim-conn", "名詞／数量詞＋につき。", ["cap-conn"],
            topic_id="topic-intro", start=1.0, end=9.0, kind="concept",
        ),
        make_unit(
            "unit-cause", "claim-cause", "店内改装中につき、今月は臨時休業いたします。",
            ["cap-cause"], topic_id="topic-cause", start=11.0, end=19.0, kind="example",
        ),
        make_unit(
            "unit-rate", "claim-rate", "駐車場は1時間につき1500円です。",
            ["cap-rate"], topic_id="topic-rate", start=21.0, end=29.0, kind="example",
        ),
    ]
    transcript_rows = [
        ("cap-conn", 1.0, 9.0, "名詞／数量詞＋につき。"),
        ("cap-cause", 11.0, 19.0, "店内改装中につき、今月は臨時休業いたします。"),
        ("cap-rate", 21.0, 29.0, "駐車場は1時間につき1500円です。"),
    ]
    if include_about:
        topic_rows.append(("topic-about", "用法3・について", 30.0, 40.0))
        long_about = (
            "自衛隊の海外派遣について発言した；老师指出此处是关于的意思；"
            "保留否定「使わないで」；关于这一用法较少见，通常使用「について」。"
        )
        units.append(
            make_unit(
                "unit-about", "claim-about", long_about, ["cap-about"],
                topic_id="topic-about", start=31.0, end=39.0, kind="example",
            )
        )
        transcript_rows.append(("cap-about", 31.0, 39.0, long_about))
    topics = course_map(topic_rows)
    transcript = make_transcript(transcript_rows, duration=40.0)
    plan = edit_deck(
        knowledge(*units),
        course_map=topics,
        transcript=transcript,
        visual=make_visual([], duration=40.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
        order="teaching",
    )
    return plan


def test_summary_compares_senses_in_three_short_role_lines_and_binds_exactly():
    plan = _summary_plan()
    summary = plan.pages[-1]
    assert summary.type == "summary"
    assert len(summary.body_points) == 3
    joined = " ".join(summary.body_points)
    assert "用法一" in summary.body_points[0] and "原因" in summary.body_points[0]
    assert "用法二" in summary.body_points[1] and "每个单位" in summary.body_points[1]
    assert "用法三" in summary.body_points[2] and "通常用「について」" in summary.body_points[2]
    assert not any(marker in joined for marker in ("对比点", "保留否定", "老师指出"))
    assert "店内改装中につき" not in joined
    assert "1500" not in joined and "ポイント" not in joined and "300円" not in joined
    assert len(summary.body_points) == len(summary.claim_ids)
    assert len(set(summary.body_points)) == 3
    assert all(len(point) <= 80 for point in summary.body_points)

    claims = {
        claim_id: SlideClaim(
            id=claim_id,
            text="源 claim text",
            evidence_ids=[f"ev-{claim_id}"],
            verdict="supported",
            provenance="source",
        )
        for claim_id in summary.claim_ids
    }
    bound = _bind_summary(summary, claims=claims)[0]
    assert bound.bullets == summary.body_points
    about = next(
        page for page in plan.pages
        if page.type == "content" and page.title.startswith("用法三")
    )
    assert any("通常使用「について」" in point for point in about.body_points)


def test_summary_does_not_claim_about_sense_when_missing():
    summary = _summary_plan(include_about=False).pages[-1]
    assert len(summary.body_points) == 2
    assert not any(line.startswith("用法三") for line in summary.body_points)


def test_summary_uses_source_backed_usage_advice_across_about_topics():
    topics = course_map([
        ("topic-cause", "用法1・原因", 0.0, 10.0),
        ("topic-about-main", "用法3・について", 10.0, 20.0),
        ("topic-about-example", "用法3・についての例句", 20.0, 30.0),
    ])
    doc = knowledge(
        make_unit(
            "unit-about-main", "claim-about-main",
            "关于的意思：～について的中止形。通常使用「について」。",
            ["cap-about-main"], topic_id="topic-about-main", start=11.0, end=19.0,
        ),
        make_unit(
            "unit-about-example", "claim-about-example",
            "自衛隊の海外派遣につき、国会で与党と野党が激しく議論している。",
            ["cap-about-example"], topic_id="topic-about-example",
            start=21.0, end=29.0, kind="example",
        ),
    )
    selected = [PageCandidate(
        id="candidate-about-example", unit_id="unit-about-example",
        topic_id="topic-about-example", kind="example",
        claim_ids=["claim-about-example"], frame_ids=[], layout="text",
        start_seconds=21.0, end_seconds=29.0, score=1.0,
        required_prerequisite=False, selection_reason="source example",
        title="用法三", notes="关于用法的例句",
    )]
    content = _content_page(selected[0], course_map=topics, knowledge=doc)
    assert content.title.startswith("用法三")

    summary = _summary_page(selected, course_map=topics, knowledge=doc)
    assert summary.body_points == ["用法三：关于（罕用，通常用「について」）"]
    assert summary.claim_ids == ["claim-about-main"]

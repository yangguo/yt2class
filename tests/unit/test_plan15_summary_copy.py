from __future__ import annotations

import pytest

from yt2class.adapters.providers.base import FakeProvider
from yt2class.stages.edit_deck import PageCandidate, build_deterministic_plan, edit_deck
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit as make_unit, course_map, knowledge


def _pipeline_plan(*, topic_title: str, claim_text: str, kind: str = "concept"):
    topics = course_map([("topic-main", topic_title, 0.0, 10.0)])
    doc = knowledge(
        make_unit(
            "unit-main",
            "claim-main",
            claim_text,
            ["cap-main"],
            topic_id="topic-main",
            start=1.0,
            end=9.0,
            kind=kind,
        )
    )
    return edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([("cap-main", 1.0, 9.0, claim_text)], duration=10.0),
        visual=make_visual([], duration=10.0),
        provider=FakeProvider(frames_caps()),
        target_pages=4,
        max_pages=8,
        order="chronological",
    )


@pytest.mark.parametrize(
    ("topic_title", "claim_text", "kind"),
    [
        ("Definition", "A nonempty set excludes the null value.", "concept"),
        ("Measurement", "Close the valve before recording pressure.", "procedure"),
        (
            "Topic marker",
            "「は」 marks the sentence topic and does not mark the grammatical subject by itself.",
            "concept",
        ),
    ],
    ids=["concept-lecture", "procedure", "grammar"],
)
def test_pipeline_summary_uses_complete_source_claims_across_domains(
    topic_title: str,
    claim_text: str,
    kind: str,
):
    plan = _pipeline_plan(topic_title=topic_title, claim_text=claim_text, kind=kind)

    summaries = [page for page in plan.pages if page.type == "summary"]
    content_claim_ids = {
        claim_id
        for page in plan.pages
        if page.type in {"content", "quiz"}
        for claim_id in page.claim_ids
    }
    assert len(summaries) == 1
    assert summaries[0].claim_ids == ["claim-main"]
    assert set(summaries[0].claim_ids) <= content_claim_ids
    assert summaries[0].body_points == [claim_text]


def _topic_candidates(count: int) -> tuple[list[PageCandidate], object, object, object]:
    topics = course_map(
        [(f"topic-{index}", f"Topic {index}", float(index * 10), float(index * 10 + 10)) for index in range(count)]
    )
    units = []
    transcript_rows = []
    selected = []
    for index in range(count):
        claim_id = f"claim-{index}"
        cap_id = f"cap-{index}"
        text = f"Topic {index} preserves property {index}."
        units.append(
            make_unit(
                f"unit-{index}",
                claim_id,
                text,
                [cap_id],
                topic_id=f"topic-{index}",
                start=float(index * 10 + 1),
                end=float(index * 10 + 9),
                kind="concept",
            )
        )
        transcript_rows.append((cap_id, float(index * 10 + 1), float(index * 10 + 9), text))
        selected.append(
            PageCandidate(
                id=f"intent-unit-{index}",
                unit_id=f"unit-{index}",
                topic_id=f"topic-{index}",
                kind="concept",
                claim_ids=[claim_id],
                frame_ids=[],
                layout="text",
                start_seconds=float(index * 10 + 1),
                end_seconds=float(index * 10 + 9),
                score=1.0,
                required_prerequisite=False,
                selection_reason="source concept",
                title=f"Topic {index}",
                notes=text,
                body_points=[text],
            )
        )
    doc = knowledge(*units)
    transcript = make_transcript(transcript_rows, duration=float(count * 10))
    visual = make_visual([], duration=float(count * 10))
    return selected, doc, topics, (transcript, visual)


def test_pipeline_paginates_summary_for_more_than_four_topics():
    _selected, doc, topics, (transcript, visual) = _topic_candidates(6)
    # A larger target lets selection retain every topic and leaves room for
    # the deterministic summary to continue onto another page.
    from yt2class.stages.edit_deck import edit_deck

    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        target_pages=12,
        max_pages=16,
        order="chronological",
    )

    summaries = [page for page in plan.pages if page.type == "summary"]
    body_claims = {
        claim_id
        for page in plan.pages
        if page.type in {"content", "quiz"}
        for claim_id in page.claim_ids
    }
    assert len(summaries) == 2
    assert [len(page.body_points) for page in summaries] == [4, 2]
    assert all(page.claim_ids == [f"claim-{index}" for index in range(i * 4, min(i * 4 + 4, 6))]
               for i, page in enumerate(summaries))
    assert all(set(page.claim_ids) <= body_claims for page in summaries)
    assert len(plan.pages) <= plan.max_pages


def test_summary_prefers_complete_concept_claim_over_example_for_same_topic():
    topics = course_map([("topic-main", "Energy balance", 0.0, 20.0)])
    doc = knowledge(
        make_unit(
            "unit-example",
            "claim-example",
            "For example, the heater warms the water.",
            ["cap-example"],
            topic_id="topic-main",
            start=11.0,
            end=19.0,
            kind="example",
        ),
        make_unit(
            "unit-concept",
            "claim-concept",
            "Energy remains conserved in this system.",
            ["cap-concept"],
            topic_id="topic-main",
            start=1.0,
            end=9.0,
            kind="concept",
        ),
    )
    selected = [
        PageCandidate(
            id="intent-unit-example",
            unit_id="unit-example",
            topic_id="topic-main",
            kind="example",
            claim_ids=["claim-example"],
            frame_ids=[],
            layout="text",
            start_seconds=11.0,
            end_seconds=19.0,
            score=1.0,
            required_prerequisite=False,
            selection_reason="source example",
            title="Energy balance",
            notes="For example, the heater warms the water.",
            body_points=["For example, the heater warms the water."],
        ),
        PageCandidate(
            id="intent-unit-concept",
            unit_id="unit-concept",
            topic_id="topic-main",
            kind="concept",
            claim_ids=["claim-concept"],
            frame_ids=[],
            layout="text",
            start_seconds=1.0,
            end_seconds=9.0,
            score=1.0,
            required_prerequisite=False,
            selection_reason="source concept",
            title="Energy balance",
            notes="Energy remains conserved in this system.",
            body_points=["Energy remains conserved in this system."],
        ),
    ]
    plan = build_deterministic_plan(
        selected,
        omissions=[],
        source_id=doc.source_id,
        target_pages=4,
        max_pages=8,
        order="chronological",
        course_map=topics,
        knowledge=doc,
        transcript=make_transcript(
            [("cap-concept", 1.0, 9.0, "Energy remains conserved in this system."),
             ("cap-example", 11.0, 19.0, "For example, the heater warms the water.")],
            duration=20.0,
        ),
        visual=make_visual([], duration=20.0),
    )

    summary = next(page for page in plan.pages if page.type == "summary")
    assert summary.claim_ids == ["claim-concept"]
    assert summary.body_points == ["Energy remains conserved in this system."]


def test_long_source_claim_uses_clean_topic_title_without_mid_sentence_cut():
    long_claim = (
        "The controller does not guarantee stable pressure when the ambient temperature changes, "
        "and the instructor says the warning condition must remain active throughout the test. "
    ) * 3
    doc = knowledge(
        make_unit(
            "unit-long",
            "claim-long",
            long_claim,
            ["cap-long"],
            topic_id="topic-long",
            start=1.0,
            end=9.0,
            kind="recap",
        )
    )
    selected = [
        PageCandidate(
            id="intent-unit-long",
            unit_id="unit-long",
            topic_id="topic-long",
            kind="recap",
            claim_ids=["claim-long"],
            frame_ids=[],
            layout="text",
            start_seconds=1.0,
            end_seconds=9.0,
            score=1.0,
            required_prerequisite=False,
            selection_reason="source recap",
            title="Thermal limits",
            notes="",
            body_points=[],
        )
    ]
    plan = build_deterministic_plan(
        selected,
        omissions=[],
        source_id=doc.source_id,
        target_pages=4,
        max_pages=8,
        order="chronological",
        course_map=None,
        knowledge=doc,
        transcript=make_transcript([("cap-long", 1.0, 9.0, long_claim)], duration=10.0),
        visual=make_visual([], duration=10.0),
    )

    summary = next(page for page in plan.pages if page.type == "summary")
    assert summary.body_points == ["Thermal limits"]
    assert "topic-long" not in " ".join(summary.body_points)
    assert "does not guarantee" not in " ".join(summary.body_points)


def test_missing_source_evidence_does_not_create_summary_claim():
    doc = knowledge(
        make_unit(
            "unit-practice",
            "claim-practice",
            "The calculated example uses a hypothetical value.",
            ["cap-not-in-source"],
            topic_id="topic-practice",
            start=1.0,
            end=9.0,
            kind="example",
            provenance="generated-practice",
        )
    )
    selected = [
        PageCandidate(
            id="intent-unit-practice",
            unit_id="unit-practice",
            topic_id="topic-practice",
            kind="example",
            claim_ids=["claim-practice"],
            frame_ids=[],
            layout="text",
            start_seconds=1.0,
            end_seconds=9.0,
            score=1.0,
            required_prerequisite=False,
            selection_reason="generated practice",
            title="Practice calculation",
            notes="The calculated example uses a hypothetical value.",
            body_points=["The calculated example uses a hypothetical value."],
        )
    ]
    plan = build_deterministic_plan(
        selected,
        omissions=[],
        source_id=doc.source_id,
        target_pages=4,
        max_pages=8,
        order="chronological",
        course_map=course_map([("topic-practice", "Practice calculation", 0.0, 10.0)]),
        knowledge=doc,
        transcript=make_transcript([], duration=10.0),
        visual=make_visual([], duration=10.0),
    )

    assert not any(page.type == "summary" for page in plan.pages)
    assert any(
        omission.topic_id == "topic-practice"
        and omission.claim_id is None
        and "source-backed summary" in omission.reason
        for omission in plan.omissions
    )


def test_tight_budget_keeps_body_and_reports_topics_missing_from_summary():
    selected, doc, topics, (transcript, visual) = _topic_candidates(7)
    plan = build_deterministic_plan(
        selected,
        omissions=[],
        source_id=doc.source_id,
        target_pages=4,
        max_pages=8,
        order="chronological",
        course_map=topics,
        knowledge=doc,
        transcript=transcript,
        visual=visual,
    )

    body_claims = {
        claim_id
        for page in plan.pages
        if page.type in {"content", "quiz"}
        for claim_id in page.claim_ids
    }
    summaries = [page for page in plan.pages if page.type == "summary"]
    summarized_claims = {claim_id for page in summaries for claim_id in page.claim_ids}
    missing_summary_topics = {
        omission.topic_id
        for omission in plan.omissions
        if omission.topic_id is not None
        and omission.claim_id is None
        and "summary coverage" in omission.reason
    }
    assert len(plan.pages) <= plan.max_pages
    assert len([page for page in plan.pages if page.type in {"content", "quiz"}]) == 6
    assert len(summaries) == 1
    assert len(summarized_claims) == 4
    assert summarized_claims <= body_claims
    assert len(missing_summary_topics) == 2


def _tsuki_summary_plan(*, include_about: bool = True):
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
    return edit_deck(
        knowledge(*units),
        course_map=topics,
        transcript=make_transcript(transcript_rows, duration=40.0),
        visual=make_visual([], duration=40.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
        order="teaching",
    )


def test_summary_compares_senses_in_three_short_role_lines():
    plan = _tsuki_summary_plan()
    summary = next(page for page in plan.pages if page.type == "summary")
    assert summary.body_points == [
        "用法一：原因・理由（公告等）",
        "用法二：比例・単位（每个单位）",
        "用法三：关于（罕用，通常用「について」）",
    ]
    joined = " ".join(summary.body_points)
    assert not any(marker in joined for marker in ("对比点", "保留否定", "老师指出"))
    assert "店内改装中につき" not in joined
    assert "1500" not in joined
    assert len(summary.body_points) == len(summary.claim_ids)
    assert all(len(point) <= 80 for point in summary.body_points)
    about = next(
        page for page in plan.pages
        if page.type == "content" and page.title.startswith("用法三")
    )
    assert any("通常使用「について」" in point or "通常用「について」" in point for point in about.body_points)


def test_summary_does_not_claim_about_sense_when_missing():
    summary = next(
        page for page in _tsuki_summary_plan(include_about=False).pages if page.type == "summary"
    )
    assert summary.body_points == [
        "用法一：原因・理由（公告等）",
        "用法二：比例・単位（每个单位）",
    ]
    assert not any(line.startswith("用法三") for line in summary.body_points)


def test_summary_cites_source_usage_advice_for_about_sense():
    topics = course_map([
        ("topic-cause", "用法1・原因", 0.0, 10.0),
        ("topic-rate", "用法2・比例", 10.0, 20.0),
        ("topic-about-main", "用法3・について", 20.0, 30.0),
        ("topic-about-example", "用法3・についての例句", 30.0, 40.0),
    ])
    advice = "关于的意思：～について的中止形。通常使用「について」。"
    example = "自衛隊の海外派遣につき、国会で与党と野党が激しく議論している。"
    doc = knowledge(
        make_unit(
            "unit-cause", "claim-cause", "本日雨天につき、運動会は延期します。",
            ["cap-cause"], topic_id="topic-cause", start=1.0, end=9.0, kind="example",
        ),
        make_unit(
            "unit-rate", "claim-rate", "駐車場は1時間につき1500円です。",
            ["cap-rate"], topic_id="topic-rate", start=11.0, end=19.0, kind="example",
        ),
        make_unit(
            "unit-about-main", "claim-about-main", advice,
            ["cap-about-main"], topic_id="topic-about-main", start=21.0, end=29.0,
        ),
        make_unit(
            "unit-about-example", "claim-about-example", example,
            ["cap-about-example"], topic_id="topic-about-example",
            start=31.0, end=39.0, kind="example",
        ),
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript(
            [
                ("cap-cause", 1.0, 9.0, "本日雨天につき、運動会は延期します。"),
                ("cap-rate", 11.0, 19.0, "駐車場は1時間につき1500円です。"),
                ("cap-about-main", 21.0, 29.0, advice),
                ("cap-about-example", 31.0, 39.0, example),
            ],
            duration=40.0,
        ),
        visual=make_visual([], duration=40.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
        order="teaching",
    )
    summary = next(page for page in plan.pages if page.type == "summary")
    assert "用法三：关于（罕用，通常用「について」）" in summary.body_points
    assert "claim-about-main" in summary.claim_ids
    assert example not in " ".join(summary.body_points)


def test_summary_does_not_claim_about_topic_without_selected_source_content():
    plan = _pipeline_plan(
        topic_title="About markers",
        claim_text="A quoted marker can introduce a topic.",
    )
    summary = next(page for page in plan.pages if page.type == "summary")
    body_claim_ids = {
        claim_id
        for page in plan.pages
        if page.type in {"content", "quiz"}
        for claim_id in page.claim_ids
    }
    assert set(summary.claim_ids) <= body_claim_ids
    assert "claim-about" not in summary.claim_ids
    assert all("通常用「について」" not in point for point in summary.body_points)

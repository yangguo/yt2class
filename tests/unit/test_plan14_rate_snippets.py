"""Plan14: one complete snippet per 用法二 kind, and no timing notes on 接续."""

from __future__ import annotations

import re

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.knowledge import KnowledgeClaim
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.grammar_sense import (
    CONNECTIVE_HEADING,
    representative_rate_snippets,
    text_is_rate_proportion,
)
from yt2class.stages.student_copy import LEARNER_ARTIFACT_RE
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge

_TIMING_RE = re.compile(r"\d+(?:\.\d+)?s")


def test_truncated_hourly_asr_does_not_crowd_out_other_rate_kinds():
    assert not text_is_rate_proportion("ましょう 活動センターの会議室は1時間につき")
    assert text_is_rate_proportion("活動センターの会議室は1時間につき1500円で利用できます。")
    snippets = representative_rate_snippets(
        [
            "ましょう 活動センターの会議室は1時間につき",
            "活動センターの会議室は1時間につき",
            "活動センターの会議室は1時間につき 1500円で利用できますつまり利用料は",
            "会員カードをお持ちのお客様は1000円分のお買い物につき10ポイント貯まります",
            "返却期限を過ぎた場合延滞料金として1日 につき300円を支払いいただきます",
            "老师解析：「1000円分のお買い物という単位、そしてそこに10ポイントたまります」。",
        ]
    )
    assert snippets == [
        "1時間につき1500円",
        "1000円分のお買い物につき10ポイント",
        "1日につき300円",
    ]


def test_plan14_usage_two_keeps_three_kinds_from_other_topics():
    topics = course_map(
        [
            ("topic-intro", "Introduction: what goes before につき", 0.0, 20.0),
            ("topic-cause", "用法1・原因", 20.0, 90.0),
            ("topic-rate", "用法2・比例", 90.0, 140.0),
            ("topic-about", "用法3・について", 140.0, 180.0),
        ]
    )
    gloss = concept_unit(
        "unit-attach",
        "claim-attach",
        "接续：名詞／数量詞＋につき。",
        ["cap-attach"],
        topic_id="topic-intro",
        start=2.0,
        end=8.0,
        kind="concept",
    )
    attach = gloss.model_copy(
        update={
            "claims": [
                *gloss.claims,
                KnowledgeClaim(
                    id="claim-board",
                    text=(
                        "：0.033s 的板书帧（）已写出本课词头「～につき」及接续「名詞／数量詞」，"
                        "与 5.78–8.45s 的口头开场白相对应。"
                    ),
                    evidence_ids=["cap-board"],
                    status="draft",
                    qualifiers=[],
                    modality="both",
                    provenance="source",
                ),
            ]
        }
    )
    cause = concept_unit(
        "unit-cause",
        "claim-cause",
        "店内改装中につき、今月は臨時休業いたします。",
        ["cap-cause"],
        topic_id="topic-cause",
        start=22.0,
        end=40.0,
        kind="example",
    )
    daily = concept_unit(
        "unit-daily",
        "claim-daily",
        "返却期限を過ぎた場合、延滞料金として一日につき300円お支払いいたします。",
        ["cap-daily"],
        topic_id="topic-cause",
        start=42.0,
        end=60.0,
        kind="example",
    )
    points = concept_unit(
        "unit-points",
        "claim-points",
        "会員カードをお持ちのお客様は1000円分のお買い物につき10ポイント貯まります。",
        ["cap-points"],
        topic_id="topic-cause",
        start=62.0,
        end=78.0,
        kind="example",
    )
    fragments = concept_unit(
        "unit-fragments",
        "claim-fragment",
        "ましょう 活動センターの会議室は1時間につき",
        ["cap-h1"],
        topic_id="topic-rate",
        start=92.0,
        end=100.0,
        kind="example",
    )
    hourly = concept_unit(
        "unit-hourly",
        "claim-hourly",
        "活動センターの会議室は1時間につき1500円で利用できます。",
        ["cap-hourly"],
        topic_id="topic-rate",
        start=102.0,
        end=120.0,
        kind="example",
    )
    about = concept_unit(
        "unit-about",
        "claim-about",
        "自衛隊の海外派遣について、国会で議論している。",
        ["cap-about"],
        topic_id="topic-about",
        start=142.0,
        end=160.0,
        kind="example",
    )
    doc = knowledge(attach, cause, daily, points, fragments, hourly, about)
    transcript = make_transcript(
        [
            ("cap-h1", 92.0, 96.0, "ましょう 活動センターの会議室は1時間につき"),
            ("cap-h2", 96.0, 99.0, "活動センターの会議室は1時間につき"),
            (
                "cap-h3",
                99.0,
                104.0,
                "活動センターの会議室は1時間につき 1500円で利用できますつまり利用料は",
            ),
            ("cap-p1", 62.0, 66.0, "10ポイント貯まりますねこれは 1000円分のお買い物という単位"),
            ("cap-p2", 66.0, 70.0, "1000円分のお買い物という単位 そしてそこに10ポイント"),
            (
                "cap-daily",
                42.0,
                50.0,
                "返却期限を過ぎた場合延滞料金として1日 につき300円を支払いいただきます",
            ),
        ],
        duration=180.0,
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=make_visual([], duration=180.0),
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
        order="chronological",
    )
    content = [page for page in plan.pages if page.type == "content"]
    titles = [page.title for page in plan.pages]
    connective = next(page for page in content if page.title == CONNECTIVE_HEADING)
    assert "mandatory-connective lock" in connective.selection_reason
    connective_copy = " ".join([connective.title, connective.notes, *connective.body_points])
    assert _TIMING_RE.search(connective_copy) is None
    assert "板书帧（）" not in connective_copy
    assert "板书帧()" not in connective_copy

    usage_one = [page for page in content if page.title.startswith("用法一")]
    assert 1 <= len(usage_one) <= 2
    cause_copy = " ".join(point for page in usage_one for point in (*page.body_points, page.notes))
    assert "300円" not in cause_copy
    assert "ポイント" not in cause_copy
    assert "休業" in cause_copy

    usage_two = [page for page in content if page.title.startswith("用法二")]
    assert len(usage_two) == 1
    rate_copy = " ".join([*usage_two[0].body_points, usage_two[0].notes])
    assert "1時間につき1500円" in rate_copy
    assert "1000円分のお買い物につき10ポイント" in rate_copy
    assert "一日につき300円" in rate_copy or "1日につき300円" in rate_copy
    assert "ましょう" not in rate_copy
    assert rate_copy.count("1時間につき") == 2  # body + notes share the one snippet
    assert "claim-daily" in usage_two[0].claim_ids
    assert "claim-points" in usage_two[0].claim_ids

    summary = next(page for page in plan.pages if page.type == "summary")
    rate_summary = next(point for point in summary.body_points if point.startswith("用法二"))
    assert rate_summary.startswith("用法二：比例・単位")
    assert "每个单位" in rate_summary
    assert "1時間につき1500円" not in rate_summary
    assert "10ポイント" not in rate_summary
    assert "300円" not in rate_summary

    def indexes(prefix: str) -> list[int]:
        return [index for index, title in enumerate(titles) if title.startswith(prefix)]

    assert max(indexes("接续")) < min(indexes("用法一")) < min(indexes("用法二")) < min(indexes("用法三"))
    for page in plan.pages:
        blob = " ".join([page.title, page.notes, *page.body_points])
        assert LEARNER_ARTIFACT_RE.search(blob) is None
        assert _TIMING_RE.search(blob) is None
        assert "板书帧（）" not in blob

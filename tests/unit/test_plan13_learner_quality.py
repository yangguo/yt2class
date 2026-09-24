"""Plan13: proportion examples stay on 用法二, and learner copy hides automation ids."""

from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.knowledge import KnowledgeClaim
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.grammar_sense import (
    CONNECTIVE_HEADING,
    text_is_rate_only,
    text_is_rate_proportion,
)
from yt2class.stages.student_copy import LEARNER_ARTIFACT_RE
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge


def test_rate_proportion_is_not_a_cause_sentence():
    fee = "返却期限を過ぎた場合、延滞料金として一日につき300円お支払いいただきます。"
    points = "1000円分のお買い物につき10ポイント"
    assert text_is_rate_proportion(fee)
    assert text_is_rate_only(fee)
    assert text_is_rate_proportion(points)
    assert text_is_rate_proportion("駐車場は1時間につき1500円です。")
    assert not text_is_rate_proportion("本日雨天につき、運動会は来週に延期します。")
    assert not text_is_rate_only("本日雨天につき、運動会は来週に延期します。")
    assert not text_is_rate_proportion("接续：名詞／数量詞＋につき。")


def test_plan13_rate_examples_frames_and_learner_copy():
    topics = course_map(
        [
            ("topic-intro", "Introduction: what goes before につき", 0.0, 20.0),
            ("topic-cause", "用法1・原因", 20.0, 90.0),
            ("topic-rate", "用法2・比例", 90.0, 130.0),
            ("topic-about", "用法3・について", 130.0, 200.0),
        ]
    )
    gloss = concept_unit(
        "unit-attach",
        "claim-attach",
        "接续：名詞／数量詞＋につき。",
        ["cap-attach", "frame-grammar"],
        topic_id="topic-intro",
        start=2.0,
        end=12.0,
        kind="concept",
        frames=["frame-grammar", "frame-kaiwa"],
    )
    attach = gloss.model_copy(
        update={
            "claims": [
                *gloss.claims,
                KnowledgeClaim(
                    id="claim-align",
                    text="语音与画面对应 cap-0148 occ-0014。",
                    evidence_ids=["frame-kaiwa"],
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
        "本日雨天につき、運動会は来週に延期します。cap-0148 occ-0012 d344652e6eddd447.webm @ 04:17 语音与画面对应。",
        ["cap-cause", "frame-board", "frame-cause"],
        topic_id="topic-cause",
        start=30.0,
        end=50.0,
        kind="example",
        frames=["frame-board", "frame-cause"],
    )
    daily = concept_unit(
        "unit-daily",
        "claim-daily",
        "返却期限を過ぎた場合、延滞料金として一日につき300円お支払いいただきます。",
        ["cap-daily"],
        topic_id="topic-cause",
        start=52.0,
        end=70.0,
        kind="example",
    )
    hourly = concept_unit(
        "unit-hourly",
        "claim-hourly",
        "駐車場は1時間につき1500円です。",
        ["cap-hourly"],
        topic_id="topic-rate",
        start=92.0,
        end=110.0,
        kind="example",
    )
    about_a = concept_unit(
        "unit-about-a",
        "claim-about-a",
        "この使い方は使わないでください。自衛隊の派遣について発言した。についてが普通です。",
        ["cap-about-a"],
        topic_id="topic-about",
        start=132.0,
        end=150.0,
        kind="example",
    )
    about_b = concept_unit(
        "unit-about-b",
        "claim-about-b",
        "もう一度。この使い方は使わないでください。自衛隊の派遣について発言した。についてが普通です。",
        ["cap-about-b"],
        topic_id="topic-about",
        start=152.0,
        end=170.0,
        kind="concept",
    )
    doc = knowledge(attach, cause, daily, hourly, about_a, about_b)
    visual = make_visual(
        [
            ("frame-grammar", 8.0, "scene-001"),
            ("frame-kaiwa", 10.0, "scene-001"),
            ("frame-board", 40.0, "scene-001"),
            ("frame-cause", 48.0, "scene-001"),
            ("frame-points", 100.0, "scene-001"),
        ],
        duration=200.0,
        ocr=[
            ("ocr-grammar", "frame-grammar", "接续：名詞＋につき　数量詞＋につき"),
            ("ocr-kaiwa", "frame-kaiwa", "会話：ネットカフェでチケットを買う"),
            ("ocr-board", "frame-board", "例文 雨天につき延期 一日につき300円"),
            ("ocr-cause", "frame-cause", "本日雨天につき、運動会は来週に延期します。"),
            ("ocr-points", "frame-points", "1000円分のお買い物につき10ポイント"),
        ],
    )
    transcript = make_transcript(
        [
            ("cap-attach", 2.0, 12.0, "接续は名詞と数量詞です。"),
            ("cap-cause", 30.0, 50.0, "本日雨天につき、運動会は来週に延期します。"),
            (
                "cap-daily",
                52.0,
                70.0,
                "返却期限を過ぎた場合、延滞料金として一日につき300円お支払いいただきます。",
            ),
            ("cap-hourly", 92.0, 110.0, "駐車場は1時間につき1500円です。"),
        ],
        duration=200.0,
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
        order="chronological",
    )
    titles = [page.title for page in plan.pages]
    content = [page for page in plan.pages if page.type == "content"]
    assert plan.pages[0].type == "cover"
    assert plan.pages[0].title == "～につき"
    assert "Introduction" not in plan.pages[0].title

    connective = next(page for page in content if page.title == CONNECTIVE_HEADING)
    assert "frame-kaiwa" not in connective.frame_ids
    assert connective.frame_ids == ["frame-grammar"]
    assert "mandatory-connective lock" in connective.selection_reason

    usage_one = [page for page in content if page.title.startswith("用法一")]
    assert 1 <= len(usage_one) <= 2
    cause_learner = " ".join(
        part
        for page in usage_one
        for part in (page.title, page.notes, *page.body_points, *page.frame_ids)
    )
    assert "300円" not in cause_learner
    assert "ポイント" not in cause_learner
    assert "frame-board" not in cause_learner
    assert "claim-daily" not in {claim_id for page in usage_one for claim_id in page.claim_ids}
    assert any("延期" in " ".join(page.body_points) for page in usage_one)

    usage_two = [page for page in content if page.title.startswith("用法二")]
    assert len(usage_two) == 1
    rate_learner = " ".join([*usage_two[0].body_points, usage_two[0].notes])
    assert "1時間につき1500円" in rate_learner or "1500円" in rate_learner
    assert "1000円分のお買い物につき10ポイント" in rate_learner
    assert "一日につき300円" in rate_learner
    assert "claim-daily" in usage_two[0].claim_ids

    about = [page for page in content if page.title.startswith("用法三")]
    assert len(about) == 1
    assert "使わない" in " ".join(about[0].body_points)

    summary = plan.pages[-1]
    assert summary.type == "summary"
    summary_text = " ".join(summary.body_points)
    assert "比例" in summary_text
    assert "每个单位" in summary_text
    assert "1500" not in summary_text
    assert "ポイント" not in summary_text
    assert "300円" not in summary_text
    about_summary = next(point for point in summary.body_points if point.startswith("用法三"))
    assert "使わない" not in about_summary
    assert len(about_summary) < 80

    def indexes(prefix: str) -> list[int]:
        return [index for index, title in enumerate(titles) if title.startswith(prefix)]

    assert max(indexes("接续")) < min(indexes("用法一"))
    assert max(indexes("用法一")) < min(indexes("用法二"))
    assert max(indexes("用法二")) < min(indexes("用法三"))

    for page in plan.pages:
        blob = " ".join([page.title, page.notes, *page.body_points])
        assert LEARNER_ARTIFACT_RE.search(blob) is None, blob

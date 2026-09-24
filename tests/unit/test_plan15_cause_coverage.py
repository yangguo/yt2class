from __future__ import annotations

from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge
from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.editorial import PageIntent
from yt2class.domain.knowledge import KnowledgeClaim
from yt2class.stages.bind_spec import BindError, _content_bullets
from yt2class.stages.edit_deck import edit_deck


def test_four_source_backed_cause_examples_fit_two_complete_bullets_per_page():
    examples = [
        ("repair", "店内改装中につき、今月は臨時休業いたします。", "cap-repair"),
        ("work", "工事中につき、通路を変更しています。", "cap-work"),
        ("rain", "本日雨天につき、運動会は来週に延期します。", "cap-rain"),
        ("limited", "こちらは限定品につき、お一人様一点までとさせていただきます。", "cap-limited"),
    ]
    topics = course_map(
        [
            ("topic-conn", "Introduction: what goes before につき", 0.0, 10.0),
            *[(f"topic-{key}", "用法1・原因", 10.0 + i * 10, 20.0 + i * 10) for i, (key, _, _) in enumerate(examples)],
            ("topic-rate", "用法2・比例", 60.0, 70.0),
            ("topic-about", "用法3・について", 70.0, 80.0),
        ]
    )
    units = [
        concept_unit(
            f"unit-{key}", f"claim-{key}", sentence, [cap],
            topic_id=f"topic-{key}", start=10.0 + i * 10, end=19.0 + i * 10,
            kind="example",
        )
        for i, (key, sentence, cap) in enumerate(examples)
    ]
    rain = units[2]
    rain = rain.model_copy(
        update={
            "claims": [
                *rain.claims,
                KnowledgeClaim(
                    id="claim-rain-zh",
                    text="因今日下雨，运动会延期至下周。",
                    evidence_ids=["cap-rain"],
                    status="draft",
                    qualifiers=[],
                    modality="audio",
                    provenance="source",
                ),
            ]
        }
    )
    units[2] = rain
    limited = units[3]
    units[3] = limited.model_copy(
        update={
            "claims": [
                *limited.claims,
                KnowledgeClaim(
                    id="claim-limited-unsupported",
                    text="臨時休業につき、入口を閉鎖します。",
                    evidence_ids=["cap-invalid"],
                    status="draft",
                    qualifiers=[],
                    modality="audio",
                    provenance="source",
                ),
            ]
        }
    )
    units.extend(
        [
            concept_unit(
                "unit-cause-concept", "claim-cause-concept",
                "原因・理由常见于公告等场合。", ["cap-cause-concept"],
                topic_id="topic-cause-concept", start=49.0, end=59.0, kind="concept",
            ),
            concept_unit(
                "unit-long", "claim-long", "本日雨天につき、" + "あ" * 220 + "。",
                ["cap-long"], topic_id="topic-long", start=59.0, end=60.0, kind="example",
            ),
            concept_unit(
                "unit-unsupported", "claim-unsupported", "臨時休業につき、入口を閉鎖します。",
                ["cap-does-not-exist"], topic_id="topic-repair", start=18.0, end=19.0,
                kind="example",
            ),
            concept_unit(
                "unit-rate", "claim-rate", "駐車場は1時間につき1500円です。", ["cap-rate"],
                topic_id="topic-rate", start=60.0, end=69.0, kind="example",
            ),
            concept_unit(
                "unit-about", "claim-about", "このテーマについて説明します。", ["cap-about"],
                topic_id="topic-about", start=70.0, end=79.0, kind="example",
            ),
            concept_unit(
                "unit-conn", "claim-conn", "名詞／数量詞＋につき。", ["cap-conn"],
                topic_id="topic-conn", start=1.0, end=9.0, kind="example",
            ),
        ]
    )
    doc = knowledge(*units)
    topics.topics.insert(
        5,
        topics.topics[0].model_copy(
            update={
                "id": "topic-cause-concept",
                "title": "用法1・原因",
                "goal": "理解原因・理由",
                "start_seconds": 49.0,
                "end_seconds": 59.0,
            }
        ),
    )
    topics.topics.insert(
        6,
        topics.topics[0].model_copy(
            update={
                "id": "topic-long",
                "title": "用法1・原因",
                "goal": "理解原因・理由",
                "start_seconds": 59.0,
                "end_seconds": 60.0,
            }
        ),
    )
    transcript = make_transcript(
        [(cap, 10.0 + index * 10, 19.0 + index * 10, sentence) for index, (key, sentence, cap) in enumerate(examples)]
        + [
            ("cap-rate", 60.0, 69.0, "駐車場は1時間につき1500円です。"),
            ("cap-about", 70.0, 79.0, "このテーマについて説明します。"),
            ("cap-conn", 1.0, 9.0, "名詞／数量詞＋につき。"),
            ("cap-cause-concept", 49.0, 59.0, "原因・理由常见于公告等场合。"),
            ("cap-long", 59.0, 60.0, "本日雨天につき、" + "あ" * 220 + "。"),
        ],
        duration=80.0,
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=make_visual([], duration=80.0),
        provider=FakeProvider(frames_caps()),
        target_pages=7,
        max_pages=7,
        order="teaching",
    )

    content = [page for page in plan.pages if page.type == "content"]
    assert len(plan.pages) <= 7
    cause_pages = [page for page in content if page.title.startswith("用法一")]
    assert len(cause_pages) == 2
    visible_bullets = [bullet for page in cause_pages for bullet in page.body_points]
    visible = "\n".join(visible_bullets)
    for _key, sentence, _cap in examples:
        assert sentence in visible, (
            visible,
            [(item.claim_id, item.reason) for item in plan.omissions],
            [(page.title, page.claim_ids) for page in content],
        )
    assert "因今日下雨，运动会延期至下周。" in visible
    assert not any("臨時休業につき、入口を閉鎖" in bullet for bullet in visible_bullets)
    assert "claim-rain" in {claim for page in cause_pages for claim in page.claim_ids}
    assert "claim-rain-zh" in {claim for page in cause_pages for claim in page.claim_ids}
    assert sum("雨天につき" in bullet for bullet in visible_bullets) == 1
    assert any(item.claim_id == "claim-unsupported" for item in plan.omissions)
    assert any(item.claim_id == "claim-limited-unsupported" for item in plan.omissions)
    assert any(
        item.claim_id == "claim-long"
        and "200-character limit" in item.reason
        for item in plan.omissions
    )
    assert "claim-limited-unsupported" not in {
        claim_id for page in cause_pages for claim_id in page.claim_ids
    }
    assert any("公告" in bullet for bullet in visible_bullets)
    assert any(page.title.startswith("用法二") for page in content)
    assert any(page.title.startswith("用法三") for page in content)
    assert any(page.title.startswith("接续") for page in content)
    # Japanese and its existing Chinese translation stay together in one teaching unit.
    rain_bullet = next(bullet for bullet in visible_bullets if "雨天につき" in bullet)
    assert "因今日下雨" in rain_bullet
    claims = {claim.id: claim for claim in doc.iter_claims()}
    valid_ids = {segment.id for segment in transcript.segments}
    for _key, sentence, _cap in examples:
        page = next(page for page in cause_pages if any(sentence in bullet for bullet in page.body_points))
        matching_claims = [claims[claim_id] for claim_id in page.claim_ids if sentence.rstrip("。") in claims[claim_id].text]
        assert matching_claims
        assert any(claim.provenance == "source" and set(claim.evidence_ids) & valid_ids for claim in matching_claims)


def test_binder_rejects_overlong_bullet_instead_of_truncating_japanese():
    page = PageIntent(
        id="page-long",
        type="content",
        title="例句",
        claim_ids=["claim-long"],
        selection_reason="fixture",
        body_points=["短句"],
    ).model_copy(update={"body_points": ["一" * 201]})
    try:
        _content_bullets(page)
    except BindError as error:
        assert "200-character limit" in str(error)
    else:
        raise AssertionError("binder must reject overlong copy rather than cut it")

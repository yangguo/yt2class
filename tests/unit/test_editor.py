from __future__ import annotations

import pytest

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.knowledge import KnowledgeRelation, VisualCandidate
from yt2class.stages.edit_deck import (
    EditorContractError,
    edit_deck,
    score_candidates,
    select_candidates,
)
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import grounding_provider, concept_unit, course_map, knowledge, lecture_knowledge


def _provider(structured=None, **kwargs) -> FakeProvider:
    return FakeProvider(frames_caps(), structured=structured, **kwargs)


def _many_concepts(count: int, *, start: float = 0.0):
    units = []
    frames = []
    caps = []
    for index in range(count):
        claim_id = f"claim-{index:03d}"
        frame_id = f"frame-{index:03d}"
        cap_id = f"cap-{index:03d}"
        t0 = start + index * 10.0
        units.append(
            concept_unit(
                f"unit-{index:03d}",
                claim_id,
                f"概念{index}：定义要点。",
                [cap_id, frame_id],
                start=t0,
                end=t0 + 10.0,
                frames=[frame_id],
            )
        )
        frames.append((frame_id, t0 + 2.0, "scene-001"))
        caps.append((cap_id, t0, t0 + 10.0, f"概念{index}：定义要点。"))
    duration = start + count * 10.0
    return (
        knowledge(*units),
        course_map([("topic-1", "主题", 0.0, duration)]),
        make_transcript(caps, duration=duration),
        make_visual(frames, duration=duration),
    )


def test_target_and_max_pages_are_respected():
    doc, topics, transcript, visual = _many_concepts(12)
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=_provider(),
        target_pages=6,
        max_pages=8,
    )
    assert plan.target_pages == 6
    assert plan.max_pages == 8
    assert 4 <= len(plan.pages) <= 8
    assert len(plan.pages) <= plan.max_pages
    assert plan.pages[0].type == "cover"
    assert plan.pages[-1].type == "summary"
    content = [page for page in plan.pages if page.type == "content"]
    assert len(content) == 4


def test_required_prerequisites_are_kept():
    base = concept_unit(
        "unit-base",
        "claim-base",
        "先掌握五十音。",
        ["cap-base", "frame-base"],
        start=50.0,
        end=60.0,
        frames=["frame-base"],
        relations=[KnowledgeRelation(from_id="claim-base", to_id="claim-adv", kind="prerequisite")],
    )
    adv = concept_unit(
        "unit-adv",
        "claim-adv",
        "才能学习自动词。",
        ["cap-adv", "frame-adv"],
        start=10.0,
        end=20.0,
        frames=["frame-adv"],
    )
    fillers = [
        concept_unit(
            f"unit-recap-{index}",
            f"claim-recap-{index}",
            f"回顾{index}。",
            [f"cap-r{index}", f"frame-r{index}"],
            start=70.0 + index,
            end=71.0 + index,
            kind="recap",
            frames=[f"frame-r{index}"],
        )
        for index in range(6)
    ]
    frames = [
        ("frame-base", 52.0, "scene-001"),
        ("frame-adv", 12.0, "scene-001"),
        *[(f"frame-r{index}", 70.5 + index, "scene-001") for index in range(6)],
    ]
    caps = [
        ("cap-base", 50.0, 60.0, "先掌握五十音。"),
        ("cap-adv", 10.0, 20.0, "才能学习自动词。"),
        *[(f"cap-r{index}", 70.0 + index, 71.0 + index, f"回顾{index}。") for index in range(6)],
    ]
    doc = knowledge(base, adv, *fillers)
    topics = course_map([("topic-1", "主题", 0.0, 80.0)])
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript(caps, duration=80.0),
        visual=make_visual(frames, duration=80.0),
        provider=_provider(),
        target_pages=4,
        max_pages=6,
    )
    selected = {claim_id for page in plan.pages for claim_id in page.claim_ids}
    assert "claim-base" in selected
    assert "claim-adv" in selected


def test_representative_examples_are_preferred_over_all_examples():
    concept = concept_unit(
        "unit-concept",
        "claim-concept",
        "定义：自动词。",
        ["cap-c", "frame-c"],
        start=0.0,
        end=10.0,
        frames=["frame-c"],
    )
    examples = [
        concept_unit(
            f"unit-ex-{index}",
            f"claim-ex-{index}",
            f"例如{index}：例句。",
            [f"cap-e{index}", f"frame-e{index}"],
            start=20.0 + index * 5,
            end=24.0 + index * 5,
            kind="example",
            frames=[f"frame-e{index}"],
        )
        for index in range(4)
    ]
    frames = [("frame-c", 2.0, "scene-001")] + [
        (f"frame-e{index}", 21.0 + index * 5, "scene-001") for index in range(4)
    ]
    caps = [("cap-c", 0.0, 10.0, "定义：自动词。")] + [
        (f"cap-e{index}", 20.0 + index * 5, 24.0 + index * 5, f"例如{index}：例句。")
        for index in range(4)
    ]
    plan = edit_deck(
        knowledge(concept, *examples),
        course_map=course_map([("topic-1", "主题", 0.0, 50.0)]),
        transcript=make_transcript(caps, duration=50.0),
        visual=make_visual(frames, duration=50.0),
        provider=_provider(),
        target_pages=8,
        max_pages=10,
    )
    example_claims = [
        claim_id
        for page in plan.pages
        if page.type == "content"
        for claim_id in page.claim_ids
        if claim_id.startswith("claim-ex-")
    ]
    assert "claim-concept" in {item for page in plan.pages for item in page.claim_ids}
    assert 1 <= len(example_claims) <= 2


def test_duplicate_images_are_not_repeated():
    shared = concept_unit(
        "unit-a",
        "claim-a",
        "第一页定义。",
        ["cap-a", "frame-dup"],
        start=0.0,
        end=10.0,
        frames=["frame-dup"],
    )
    other = concept_unit(
        "unit-b",
        "claim-b",
        "第二页也想用同一张图。",
        ["cap-b", "frame-dup"],
        start=10.0,
        end=20.0,
        frames=["frame-dup"],
    )
    visual = make_visual(
        [("frame-dup", 4.0, "scene-001"), ("frame-alt", 14.0, "scene-001")],
        duration=20.0,
    )
    other = other.model_copy(
        update={
            "visual_candidates": [
                VisualCandidate(
                    frame_id="frame-dup",
                    relevance=0.95,
                    legibility=0.8,
                    selection_reason="same board",
                )
            ]
        }
    )
    plan = edit_deck(
        knowledge(shared, other),
        course_map=course_map([("topic-1", "主题", 0.0, 20.0)]),
        transcript=make_transcript(
            [("cap-a", 0.0, 10.0, "第一页定义。"), ("cap-b", 10.0, 20.0, "第二页也想用同一张图。")],
            duration=20.0,
        ),
        visual=visual,
        provider=_provider(),
        target_pages=4,
        max_pages=6,
    )
    frame_uses = [page.id for page in plan.pages if "frame-dup" in page.frame_ids]
    assert len(frame_uses) == 1
    second = next(page for page in plan.pages if "claim-b" in page.claim_ids)
    assert second.layout == "text" or "frame-dup" not in second.frame_ids


def test_text_only_when_no_usable_frames():
    spoken = concept_unit(
        "unit-audio",
        "claim-audio",
        "这段只有口播定义。",
        ["cap-audio"],
        start=0.0,
        end=20.0,
        modality="audio",
        frames=[],
    )
    plan = edit_deck(
        knowledge(spoken),
        course_map=course_map([("topic-1", "口播", 0.0, 20.0)]),
        transcript=make_transcript([("cap-audio", 0.0, 20.0, "这段只有口播定义。")], duration=20.0),
        visual=make_visual([], duration=20.0),
        provider=_provider(),
        target_pages=4,
        max_pages=6,
    )
    content = [page for page in plan.pages if page.type == "content"]
    assert content
    assert content[0].layout == "text"
    assert content[0].frame_ids == []


def test_comparison_uses_two_frames():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=_provider(),
        target_pages=10,
        max_pages=12,
    )
    comparison = next(page for page in plan.pages if "claim-cmp" in page.claim_ids)
    assert comparison.layout == "comparison"
    assert len(comparison.frame_ids) == 2


def test_sequence_uses_two_to_three_frames():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=_provider(),
        target_pages=10,
        max_pages=12,
    )
    sequence = next(
        page
        for page in plan.pages
        if "claim-step-1" in page.claim_ids or "claim-step-2" in page.claim_ids
    )
    assert sequence.layout == "sequence"
    assert 2 <= len(sequence.frame_ids) <= 3


def test_chronological_order_follows_source_time():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=_provider(),
        target_pages=10,
        max_pages=12,
        order="chronological",
    )
    content = [page for page in plan.pages if page.type == "content"]
    starts = []
    by_claim = {claim.id: unit for unit in doc.units for claim in unit.claims}
    for page in content:
        unit_starts = [by_claim[claim_id].start_seconds for claim_id in page.claim_ids if claim_id in {c.id for u in doc.units for c in u.claims}]
        # map claim -> unit start
        claim_to_start = {claim.id: unit.start_seconds for unit in doc.units for claim in unit.claims}
        starts.append(min(claim_to_start[claim_id] for claim_id in page.claim_ids))
    assert starts == sorted(starts)


def test_teaching_order_puts_prerequisites_first():
    base = concept_unit(
        "unit-base",
        "claim-base",
        "前提：五十音。",
        ["cap-base", "frame-base"],
        start=50.0,
        end=60.0,
        frames=["frame-base"],
        relations=[KnowledgeRelation(from_id="claim-base", to_id="claim-adv", kind="prerequisite")],
    )
    adv = concept_unit(
        "unit-adv",
        "claim-adv",
        "后续：自动词。",
        ["cap-adv", "frame-adv"],
        start=10.0,
        end=20.0,
        frames=["frame-adv"],
    )
    doc = knowledge(base, adv)
    kwargs = dict(
        course_map=course_map([("topic-1", "主题", 0.0, 60.0)]),
        transcript=make_transcript(
            [("cap-adv", 10.0, 20.0, "后续：自动词。"), ("cap-base", 50.0, 60.0, "前提：五十音。")],
            duration=60.0,
        ),
        visual=make_visual(
            [("frame-adv", 12.0, "scene-001"), ("frame-base", 52.0, "scene-001")],
            duration=60.0,
        ),
        provider=_provider(),
        target_pages=4,
        max_pages=6,
    )
    chrono = edit_deck(doc, order="chronological", **kwargs)
    teaching = edit_deck(doc, order="teaching", **kwargs)
    chrono_ids = [page.claim_ids[0] for page in chrono.pages if page.claim_ids]
    teaching_ids = [page.claim_ids[0] for page in teaching.pages if page.claim_ids]
    assert chrono_ids.index("claim-adv") < chrono_ids.index("claim-base")
    assert teaching_ids.index("claim-base") < teaching_ids.index("claim-adv")


def test_omissions_record_dropped_topics_and_claims():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=_provider(),
        target_pages=4,
        max_pages=4,
    )
    selected = {claim_id for page in plan.pages for claim_id in page.claim_ids}
    assert plan.omissions
    omitted_claims = {item.claim_id for item in plan.omissions if item.claim_id}
    assert omitted_claims
    assert omitted_claims.isdisjoint(selected)


def test_model_may_only_cite_knowledge_and_allowed_frame_ids():
    doc, topics, transcript, visual = lecture_knowledge()
    provider = FakeProvider(
        frames_caps(),
        structured={
            "pages": [
                {
                    "id": "intent-cover",
                    "type": "cover",
                    "title": "坏引用",
                    "claim_ids": ["no-such-claim"],
                    "frame_ids": ["frame-unknown"],
                    "notes": "/tmp/secret.jpg",
                    "selection_reason": "伪造",
                }
            ],
            "omissions": [],
        },
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=8,
        max_pages=10,
    )
    cited_claims = {claim_id for page in plan.pages for claim_id in page.claim_ids}
    cited_frames = {frame_id for page in plan.pages for frame_id in page.frame_ids}
    allowed_claims = {claim.id for claim in doc.iter_claims()}
    allowed_frames = {item.id for item in visual.occurrences}
    assert cited_claims <= allowed_claims
    assert cited_frames <= allowed_frames
    assert "no-such-claim" not in cited_claims
    assert "frame-unknown" not in cited_frames
    assert all("/tmp/" not in page.notes and ".jpg" not in page.title for page in plan.pages)
    assert any(request.role == "editor" for request in provider.requests)


def test_score_then_select_is_deterministic_before_llm():
    doc, topics, transcript, visual = lecture_knowledge()
    first = score_candidates(doc, visual=visual, course_map=topics)
    second = score_candidates(doc, visual=visual, course_map=topics)
    assert [item.id for item in first] == [item.id for item in second]
    selected, omissions = select_candidates(
        first, target_pages=8, max_pages=10, knowledge=doc, course_map=topics
    )
    assert selected
    assert all(item.score == item.score for item in selected)
    assert isinstance(omissions, list)


def test_model_cannot_drop_required_coverage():
    from yt2class.stages.edit_deck import validate_editorial_payload

    doc, topics, transcript, visual = lecture_knowledge()
    provider = FakeProvider(
        frames_caps(),
        structured={
            "pages": [
                {
                    "id": "intent-cover",
                    "type": "cover",
                    "title": "合成示例课程",
                    "claim_ids": [],
                    "frame_ids": [],
                    "notes": "",
                    "selection_reason": "封面",
                }
            ],
            "omissions": [],
        },
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=8,
        max_pages=10,
    )
    assert any(page.type == "content" for page in plan.pages)
    assert any(page.type == "summary" for page in plan.pages)
    assert {claim.id for claim in doc.iter_claims()} & {
        claim_id for page in plan.pages for claim_id in page.claim_ids
    }
    with pytest.raises(EditorContractError, match="required|coverage|partition"):
        validate_editorial_payload(
            {
                "pages": [
                    {
                        "id": "intent-cover",
                        "type": "cover",
                        "title": "合成示例课程",
                        "claim_ids": [],
                        "frame_ids": [],
                        "notes": "",
                        "selection_reason": "封面",
                    }
                ],
                "omissions": [],
            },
            allowed_claim_ids={claim.id for claim in doc.iter_claims()},
            allowed_frame_ids={item.id for item in visual.occurrences},
            max_pages=10,
            source_id="src-demo",
            target_pages=8,
            order="chronological",
            required_page_ids={"intent-cover", "intent-unit-def", "intent-summary"},
        )


def test_injected_editor_title_cannot_become_strict_verified():
    from yt2class.adapters.providers.synthetic import synthetic_editor_from_payload
    from yt2class.stages.verify_claims import verify_claims

    unit = concept_unit(
        "unit-ok",
        "claim-ok",
        "这不是自动词。",
        ["cap-ok", "frame-ok"],
        start=0.0,
        end=10.0,
        frames=["frame-ok"],
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "通过", 0.0, 10.0)])
    transcript = make_transcript([("cap-ok", 0.0, 10.0, "这不是自动词。")], duration=10.0)
    visual = make_visual(
        [("frame-ok", 4.0, "scene-001")],
        duration=10.0,
        ocr=[("ocr-ok", "frame-ok", "自动词 ではない")],
    )

    def inject_dosage(provider, request):
        if request.role != "editor":
            return {"verdicts": (provider.last_payload or {}).get("draft_verdicts") or []}
        result = synthetic_editor_from_payload(provider.last_payload)
        for page in result["pages"]:
            if page.get("type") == "content":
                page["title"] = "服用 500 毫克"
                page["notes"] = "每日三次，饭后服用。"
                page["body_points"] = ["服用 500 毫克"]
        return result

    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps(), responder=inject_dosage),
        target_pages=4,
        max_pages=6,
    )
    assert any(page.title == "服用 500 毫克" for page in plan.pages)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(copy_verdict="insufficient"),
        quality_mode="strict",
    )
    injected = [page for page in outcome.plan.pages if page.title == "服用 500 毫克"]
    assert injected
    assert all(page.quality_label != "verified" for page in injected)
    assert all(page.quality_label == "draft" for page in injected)


def test_editor_rejects_path_literals_during_contract_check():
    from yt2class.stages.edit_deck import validate_editorial_payload

    with pytest.raises(EditorContractError, match="path|unknown"):
        validate_editorial_payload(
            {
                "pages": [
                    {
                        "id": "intent-1",
                        "type": "content",
                        "layout": "text",
                        "title": "x",
                        "claim_ids": ["claim-def"],
                        "frame_ids": [],
                        "notes": "see C:\\\\frames\\\\a.jpg",
                        "selection_reason": "x",
                    }
                ]
            },
            allowed_claim_ids={"claim-def"},
            allowed_frame_ids=set(),
            max_pages=8,
            source_id="src-demo",
            target_pages=4,
            order="chronological",
        )


def test_empty_knowledge_does_not_emit_empty_summary():
    plan = edit_deck(knowledge(), transcript=make_transcript([], duration=10),
                     visual=make_visual([], duration=10), provider=_provider())
    assert not any(page.type == "summary" for page in plan.pages)


def test_select_candidates_reserves_one_page_per_course_map_topic():
    topics = course_map(
        [
            ("topic-conn", "接续", 0.0, 20.0),
            ("topic-u1", "用法1", 20.0, 40.0),
            ("topic-u2", "用法2", 40.0, 60.0),
            ("topic-u3", "用法3", 60.0, 80.0),
        ]
    )
    units = [
        concept_unit(
            "unit-conn-main",
            "claim-conn-main",
            "名词＋につき",
            ["cap-conn-main", "frame-conn-main"],
            topic_id="topic-conn",
            start=0.0,
            end=20.0,
            frames=["frame-conn-main"],
        ),
        concept_unit(
            "unit-conn-filler",
            "claim-conn-filler",
            "接续补充说明",
            ["cap-conn-filler", "frame-conn-filler"],
            topic_id="topic-conn",
            start=0.0,
            end=20.0,
            frames=["frame-conn-filler"],
        ),
        concept_unit(
            "unit-u1",
            "claim-u1",
            "用法1：原因理由",
            ["cap-u1", "frame-u1"],
            topic_id="topic-u1",
            start=20.0,
            end=40.0,
            frames=["frame-u1"],
        ),
        concept_unit(
            "unit-u2",
            "claim-u2",
            "用法2：比例单位",
            ["cap-u2", "frame-u2"],
            topic_id="topic-u2",
            start=40.0,
            end=60.0,
            frames=["frame-u2"],
        ),
        concept_unit(
            "unit-u3",
            "claim-u3",
            "用法3：について中止形",
            ["cap-u3", "frame-u3"],
            topic_id="topic-u3",
            start=60.0,
            end=80.0,
            frames=["frame-u3"],
        ),
    ]
    doc = knowledge(*units)
    visual = make_visual(
        [
            ("frame-conn-main", 2.0, "scene-001"),
            ("frame-conn-filler", 4.0, "scene-001"),
            ("frame-u1", 22.0, "scene-001"),
            ("frame-u2", 42.0, "scene-001"),
            ("frame-u3", 62.0, "scene-001"),
        ],
        duration=80.0,
    )
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, _ = select_candidates(
        scored,
        target_pages=6,
        max_pages=8,
        knowledge=doc,
        course_map=topics,
    )
    covered = {item.topic_id for item in selected}
    assert covered == {"topic-conn", "topic-u1", "topic-u2", "topic-u3"}


def test_select_candidates_demotes_ja_zh_translation_pair_units():
    topics = course_map(
        [
            ("topic-u2", "用法2", 40.0, 60.0),
            ("topic-u3", "用法3", 60.0, 80.0),
        ]
    )
    shared_evidence = ["cap-shared", "frame-shared"]
    doc = knowledge(
        concept_unit(
            "unit-u2-ja",
            "claim-u2-ja",
            "一個につき五百円です。",
            shared_evidence,
            topic_id="topic-u2",
            start=40.0,
            end=60.0,
            frames=["frame-shared"],
        ),
        concept_unit(
            "unit-u2-zh",
            "claim-u2-zh",
            "每个五百日元。",
            shared_evidence,
            topic_id="topic-u2",
            start=40.0,
            end=60.0,
            frames=["frame-shared"],
        ),
        concept_unit(
            "unit-u3",
            "claim-u3",
            "用法3：についてを使う。",
            ["cap-u3", "frame-u3"],
            topic_id="topic-u3",
            start=60.0,
            end=80.0,
            frames=["frame-u3"],
        ),
    )
    visual = make_visual(
        [("frame-shared", 45.0, "scene-001"), ("frame-u3", 65.0, "scene-001")],
        duration=80.0,
    )
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, _ = select_candidates(
        scored,
        target_pages=5,
        max_pages=6,
        knowledge=doc,
        course_map=topics,
    )
    picked_u2 = [item for item in selected if item.topic_id == "topic-u2"]
    assert len(picked_u2) == 1
    assert picked_u2[0].unit_id == "unit-u2-ja"
    assert any(item.topic_id == "topic-u3" for item in selected)


def test_summary_lists_each_course_topic():
    topics = course_map(
        [
            ("topic-u1", "用法1", 0.0, 30.0),
            ("topic-u2", "用法2", 30.0, 60.0),
            ("topic-u3", "用法3", 60.0, 90.0),
        ]
    )
    units = [
        concept_unit(
            "unit-u1",
            "claim-u1",
            "用法1：原因理由の例。",
            ["cap-u1"],
            topic_id="topic-u1",
            start=0.0,
            end=30.0,
        ),
        concept_unit(
            "unit-u2",
            "claim-u2",
            "用法2：一個につき五百円。",
            ["cap-u2"],
            topic_id="topic-u2",
            start=30.0,
            end=60.0,
            kind="example",
        ),
        concept_unit(
            "unit-u3",
            "claim-u3",
            "用法3：自衛隊について。",
            ["cap-u3"],
            topic_id="topic-u3",
            start=60.0,
            end=90.0,
            kind="example",
        ),
    ]
    doc = knowledge(*units)
    visual = make_visual([], duration=90.0)
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=make_transcript([], duration=90.0),
        visual=visual,
        provider=_provider(),
        target_pages=8,
        max_pages=10,
    )
    summary = next(page for page in plan.pages if page.type == "summary")
    joined = " ".join(summary.body_points)
    assert set(summary.claim_ids) == {"claim-u1", "claim-u2", "claim-u3"}
    assert "用法1：原因理由の例。" in joined
    assert "用法2：一個につき五百円。" in joined
    assert "用法3：自衛隊について。" in joined


def test_meta_transition_units_are_not_selected_for_slides():
    topics = course_map([("topic-u1", "用法1", 0.0, 40.0)])
    doc = knowledge(
        concept_unit(
            "unit-good",
            "claim-good",
            "改装工事につき一時休業。",
            ["cap-good"],
            topic_id="topic-u1",
            start=5.0,
            end=20.0,
            kind="example",
        ),
        concept_unit(
            "unit-meta",
            "claim-meta",
            "老师过渡：板书对应 ASR→误听",
            ["cap-meta"],
            topic_id="topic-u1",
            start=1.0,
            end=4.0,
            kind="recap",
        ),
    )
    visual = make_visual([], duration=40.0)
    scored = score_candidates(doc, visual=visual, course_map=topics)
    selected, _ = select_candidates(
        scored,
        target_pages=4,
        max_pages=6,
        knowledge=doc,
        course_map=topics,
    )
    assert all(item.unit_id != "unit-meta" for item in selected)

from __future__ import annotations

import pytest
from pydantic import ValidationError

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeRelation
from yt2class.domain.verification import VerificationReport
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.verify_claims import (
    StrictVerificationError,
    VerifyOutcome,
    affirmed_directions,
    copy_is_affirmed,
    scalar_directions,
    scalar_terms,
    unsettled_directions,
    verify_claims,
)
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge, lecture_knowledge


def _plan_for(doc, topics, transcript, visual, **kwargs):
    return edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        target_pages=kwargs.pop("target_pages", 10),
        max_pages=kwargs.pop("max_pages", 12),
        **kwargs,
    )


def test_numbers_must_appear_in_evidence():
    unit = concept_unit(
        "unit-n",
        "claim-n",
        "加热 15 分钟。",
        ["cap-n"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "数字", 0.0, 10.0)])
    transcript = make_transcript([("cap-n", 0.0, 10.0, "加热 3 分钟。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-n")
    assert verdict.verdict in {"contradicted", "insufficient"}
    assert "claim-n" in set(outcome.report.removed_from_formal) | set(outcome.report.pending_review)


def test_negation_must_be_preserved():
    unit = concept_unit(
        "unit-neg",
        "claim-neg",
        "这是自动词。",
        ["cap-neg"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "否定", 0.0, 10.0)])
    transcript = make_transcript([("cap-neg", 0.0, 10.0, "这不是自动词。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-neg")
    assert verdict.verdict == "contradicted"


def test_conditions_must_be_preserved():
    unit = concept_unit(
        "unit-cond",
        "claim-cond",
        "打开阀门。",
        ["cap-cond"],
        start=0.0,
        end=10.0,
        modality="audio",
        qualifiers=[],
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "条件", 0.0, 10.0)])
    transcript = make_transcript(
        [("cap-cond", 0.0, 10.0, "如果温度低于 80 度则打开阀门。")],
        duration=10.0,
    )
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-cond")
    assert verdict.verdict in {"contradicted", "insufficient"}


def test_proper_names_must_match_evidence():
    unit = concept_unit(
        "unit-name",
        "claim-name",
        "这是 Tanaka 老师的规则。",
        ["cap-name"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "专名", 0.0, 10.0)])
    transcript = make_transcript([("cap-name", 0.0, 10.0, "这是 Suzuki 老师的规则。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-name")
    assert verdict.verdict in {"contradicted", "insufficient"}


def test_translation_retains_source_terms():
    unit = concept_unit(
        "unit-tr",
        "claim-tr",
        "这叫自动词。",
        ["cap-tr"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "翻译", 0.0, 10.0)])
    transcript = make_transcript([("cap-tr", 0.0, 10.0, "これは自動詞です。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-tr")
    assert verdict.verdict in {"insufficient", "contradicted"}


def test_procedure_steps_keep_order():
    from yt2class.domain.knowledge import KnowledgeUnit

    unit = KnowledgeUnit(
        id="unit-proc",
        topic_id="topic-1",
        segment_ids=["seg-0001"],
        start_seconds=0.0,
        end_seconds=30.0,
        kind="procedure",
        claims=[
            KnowledgeClaim(
                id="claim-s1",
                text="步骤1 打开阀门。",
                evidence_ids=["cap-s1", "frame-late"],
                status="draft",
                modality="both",
            ),
            KnowledgeClaim(
                id="claim-s2",
                text="步骤2 然后记录读数。",
                evidence_ids=["cap-s2", "frame-early"],
                status="draft",
                modality="both",
            ),
        ],
        relations=[KnowledgeRelation(from_id="claim-s1", to_id="claim-s2", kind="step_before")],
        visual_candidates=[],
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "步骤", 0.0, 30.0)])
    transcript = make_transcript(
        [("cap-s1", 20.0, 25.0, "步骤1 打开阀门。"), ("cap-s2", 5.0, 10.0, "步骤2 然后记录读数。")],
        duration=30.0,
    )
    visual = make_visual(
        [("frame-early", 6.0, "scene-001"), ("frame-late", 22.0, "scene-001")],
        duration=30.0,
    )
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    assert {"claim-s1", "claim-s2"} & set(outcome.report.pending_review) or {
        "claim-s1",
        "claim-s2",
    } <= set(outcome.report.removed_from_formal)
    assert not any(
        page.layout == "sequence" and page.quality_label == "verified" for page in outcome.plan.pages
    )


def test_image_text_correspondence():
    unit = concept_unit(
        "unit-img",
        "claim-img",
        "画面写着 80 度。",
        ["cap-img", "frame-001"],
        start=0.0,
        end=10.0,
        frames=["frame-001"],
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "图文", 0.0, 10.0)])
    transcript = make_transcript([("cap-img", 0.0, 10.0, "看这个数字。")], duration=10.0)
    visual = make_visual(
        [("frame-001", 4.0, "scene-001")],
        duration=10.0,
        ocr=[("ocr-001", "frame-001", "室温")],
    )
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-img")
    assert verdict.verdict in {"insufficient", "contradicted"}


def test_unknown_refs_are_structural_errors():
    unit = concept_unit(
        "unit-bad",
        "claim-bad",
        "引用了不存在的证据。",
        ["cap-missing"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "未知", 0.0, 10.0)])
    transcript = make_transcript([("cap-real", 0.0, 10.0, "真实字幕。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(
        knowledge(
            concept_unit(
                "unit-ok",
                "claim-ok",
                "真实字幕。",
                ["cap-real"],
                start=0.0,
                end=10.0,
                modality="audio",
            )
        ),
        topics,
        transcript,
        visual,
        target_pages=4,
        max_pages=6,
    )
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    assert outcome.report.structural_errors
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-bad")
    assert verdict.verdict == "insufficient"


def test_contradictory_evidence_is_not_supported():
    unit = concept_unit(
        "unit-c",
        "claim-c",
        "这是自动词。",
        ["cap-pos", "cap-neg"],
        start=0.0,
        end=20.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "矛盾", 0.0, 20.0)])
    transcript = make_transcript(
        [("cap-pos", 0.0, 10.0, "这是自动词。"), ("cap-neg", 10.0, 20.0, "这不是自动词。")],
        duration=20.0,
    )
    visual = make_visual([], duration=20.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-c")
    assert verdict.verdict == "contradicted"
    assert verdict.contradicting_ids
    assert "claim-c" in set(outcome.report.pending_review) | set(outcome.report.removed_from_formal)


def test_generated_practice_is_tagged_and_not_source():
    practice = concept_unit(
        "unit-q",
        "claim-q",
        "练习：请写出自动词的否定。答案：不是自动词。",
        ["cap-src"],
        start=0.0,
        end=10.0,
        modality="audio",
        provenance="generated-practice",
        kind="recap",
    )
    source = concept_unit(
        "unit-src",
        "claim-src",
        "这不是自动词。",
        ["cap-src"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(source, practice)
    topics = course_map([("topic-1", "练习", 0.0, 10.0)])
    transcript = make_transcript([("cap-src", 0.0, 10.0, "这不是自动词。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=6, max_pages=8)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    practice_claim = next(claim for claim in outcome.knowledge.iter_claims() if claim.id == "claim-q")
    assert practice_claim.provenance == "generated-practice"
    quiz_or_notes = [
        page
        for page in outcome.plan.pages
        if "claim-q" in page.claim_ids
    ]
    for page in quiz_or_notes:
        assert page.type == "quiz" or "练习" in page.notes or page.quality_label != "verified"
        assert "原视频给出该题" not in page.notes


def test_repair_is_once_per_claim_lifetime():
    unit = concept_unit(
        "unit-r",
        "claim-r",
        "加热 15 分钟。",
        ["cap-r"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "修复", 0.0, 10.0)])
    transcript = make_transcript([("cap-r", 0.0, 10.0, "加热 3 分钟。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    repairs: list[str] = []

    def refuse_and_count(provider, request):
        if request.role == "verifier" and "repair" in request.request_id:
            repairs.append(request.request_id)
            return {"text": "加热 15 分钟。", "evidence_ids": ["cap-r"]}
        return {"verdicts": (provider.last_payload or {}).get("draft_verdicts") or []}

    provider = FakeProvider(frames_caps(), responder=refuse_and_count)
    first = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
    )
    assert first.repaired is True
    assert "claim-r" in first.report.repaired_claim_ids
    assert repairs == ["verifier:repair:claim-r"]
    second = verify_claims(
        doc,
        plan=first.plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
        existing=first.report,
        only_claim_ids={"claim-r"},
    )
    assert repairs == ["verifier:repair:claim-r"]
    assert "claim-r" in second.report.repaired_claim_ids


def test_one_repair_then_remove_or_pending():
    unit = concept_unit(
        "unit-r",
        "claim-r",
        "加热 15 分钟。",
        ["cap-r"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "修复", 0.0, 10.0)])
    transcript = make_transcript([("cap-r", 0.0, 10.0, "加热 3 分钟。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)

    def refuse_repair(provider, request):
        return {"text": "加热 15 分钟。", "evidence_ids": ["cap-r"]}

    provider = FakeProvider(frames_caps(), responder=refuse_repair)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
    )
    assert outcome.repaired is True
    assert "claim-r" in set(outcome.report.removed_from_formal) | set(outcome.report.pending_review)
    assert all(page.quality_label != "verified" or "claim-r" not in page.claim_ids for page in outcome.plan.pages)
    verifier_calls = [item for item in provider.requests if item.role == "verifier"]
    assert verifier_calls


def test_strict_mode_refuses_unresolved_critical_claims():
    unit = concept_unit(
        "unit-s",
        "claim-s",
        "加热 15 分钟。",
        ["cap-s"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "严格", 0.0, 10.0)])
    transcript = make_transcript([("cap-s", 0.0, 10.0, "加热 3 分钟。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    with pytest.raises(StrictVerificationError, match="unresolved critical|strict"):
        verify_claims(
            doc,
            plan=plan,
            transcript=transcript,
            visual=visual,
            provider=FakeProvider(frames_caps()),
            quality_mode="strict",
        )
    with pytest.raises(ValidationError, match="strict verification"):
        VerificationReport(
            schema_version="1.0",
            source_id="src-demo",
            quality_mode="strict",
            verdicts=[
                {
                    "claim_id": "claim-s",
                    "verdict": "insufficient",
                    "reason": "数字不符",
                }
            ],
        )


def test_human_sampling_is_required_and_not_model_only_pass():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = _plan_for(doc, topics, transcript, visual)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    assert isinstance(outcome, VerifyOutcome)
    assert outcome.report.human_sampling_required is True
    assert outcome.report.human_samples
    kinds = {kind for sample in outcome.report.human_samples for kind in sample.check_kinds}
    assert kinds & {"number", "negation", "condition", "proper_name", "step", "translation"}


def test_evidence_only_cannot_emit_verified_or_supported_labels():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = _plan_for(doc, topics, transcript, visual)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="evidence-only",
    )
    assert outcome.report.quality_mode == "evidence-only"
    assert all(item.verdict != "supported" for item in outcome.report.verdicts)
    assert all(page.quality_label == "evidence-only" for page in outcome.plan.pages)
    assert all("[EVIDENCE-ONLY]" in page.notes or page.type == "cover" for page in outcome.plan.pages)
    assert not any(page.quality_label == "verified" for page in outcome.plan.pages)


def test_increase_versus_decrease_is_never_supported():
    unit = concept_unit(
        "unit-flow",
        "claim-flow",
        "阀门打开后水流增加",
        ["cap-flow"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "水流", 0.0, 10.0)])
    transcript = make_transcript([("cap-flow", 0.0, 10.0, "阀门打开后水流减少")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-flow")
    assert verdict.verdict != "supported"
    assert verdict.verdict == "contradicted"
    assert not any(
        page.quality_label == "verified" and "claim-flow" in page.claim_ids
        for page in outcome.plan.pages
    )
    with pytest.raises(StrictVerificationError):
        verify_claims(
            doc,
            plan=plan,
            transcript=transcript,
            visual=visual,
            provider=FakeProvider(frames_caps()),
            quality_mode="strict",
        )


def _polarity_outcome(claim_text: str, evidence_text: str, *, quality_mode="draft"):
    unit = concept_unit(
        "unit-p",
        "claim-p",
        claim_text,
        ["cap-p"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "极性", 0.0, 10.0)])
    transcript = make_transcript([("cap-p", 0.0, 10.0, evidence_text)], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    return verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode=quality_mode,
    ), (doc, topics, transcript, visual, plan)


@pytest.mark.parametrize(
    ("claim_text", "evidence_text"),
    [
        ("阀门打开后水流变多", "阀门打开后水流变少"),
        ("阀门打开后水流变少", "阀门打开后水流变多"),
        ("温度升高", "温度降低"),
        ("温度降低", "温度升高"),
        ("阀门打开后水流增加", "阀门打开后水流减少"),
        ("转速变快", "转速变慢"),
        ("读数偏高", "读数偏低"),
        ("水位上升", "水位下降"),
        ("流量增大", "流量减小"),
        ("流速加快", "流速减慢"),
        ("电机加速", "电机减速"),
        ("锅炉升温", "锅炉降温"),
        ("水压提高", "水压降低"),
        ("电压高于五伏", "电压低于五伏"),
        ("需要更多的水", "需要更少的水"),
        ("the flow increases", "the flow decreases"),
    ],
)
def test_opposite_scalar_predicates_are_contradicted(claim_text, evidence_text):
    """Comparative predicates normalize to polarity, so unlisted opposites still fail."""

    outcome, fixtures = _polarity_outcome(claim_text, evidence_text)
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict == "contradicted"
    assert not any(
        page.quality_label == "verified" and "claim-p" in page.claim_ids
        for page in outcome.plan.pages
    )
    doc, _topics, transcript, visual, plan = fixtures
    with pytest.raises(StrictVerificationError):
        verify_claims(
            doc,
            plan=plan,
            transcript=transcript,
            visual=visual,
            provider=FakeProvider(frames_caps()),
            quality_mode="strict",
        )


def test_unlisted_predicate_swap_is_never_supported():
    """No antonym entry covers 湍急/平缓; missing predicate support must fail closed."""

    outcome, _fixtures = _polarity_outcome("阀门打开后水流湍急", "阀门打开后水流平缓")
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict != "supported"
    assert verdict.verdict == "insufficient"
    assert not verdict.supporting_ids


def test_matching_scalar_predicates_stay_supported():
    outcome, _fixtures = _polarity_outcome(
        "阀门打开后水流变多", "阀门打开后水流变多", quality_mode="strict"
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict == "supported"
    assert outcome.report.quality_mode == "strict"


@pytest.mark.parametrize(
    "text",
    ["水流变多少", "转速变快慢", "水位变高低", "尺寸变大小", "时间变长短", "增多少"],
)
def test_measure_interrogatives_carry_no_polarity(text):
    """变多少 asks how much changed; reading 变多 out of it invents a direction."""

    assert scalar_terms(text) == []
    assert scalar_directions(text) == set()


@pytest.mark.parametrize(
    "text",
    [
        "水流变多还是变少尚未确定",
        "水流会变多吗",
        "水流变多吧",
        "水流变多不",
        "水流变多不多",
        "水流可能变多",
        "水流是否变多不清楚",
    ],
)
def test_softened_or_epistemic_predicates_assert_nothing(text):
    assert scalar_terms(text)
    assert scalar_directions(text) == set()


@pytest.mark.parametrize(
    ("claim_text", "evidence_text"),
    [
        # Whichever connective or punctuation joins the two polarities.
        ("水流变多", "水流变多却下降了"),
        ("水流变多", "水流变多然后下降了"),
        ("水流变多", "水流变多后来下降了"),
        ("水流变多", "水流变多结果下降了"),
        ("水流变多", "水流变多，随后下降了"),
        ("水流变多", "水流变多？其实是下降了"),
        ("水流变多", "水流变多或者变少"),
        ("水流变多", "水流变多或少"),
        ("水流变多", "水流变多不变少"),
        ("the flow increases", "the flow increases then decreases"),
        ("the flow increases", "the flow increases however it decreases"),
        # A repeated subject re-attaches the opposite polarity to the claim.
        ("温度升高", "温度升高然后温度降低"),
    ],
)
def test_both_polarities_over_the_claim_settle_nothing(claim_text, evidence_text):
    """No connective table decides this: the claim's own proposition carries both."""

    assert unsettled_directions(claim_text, evidence_text) == {"up", "down"}
    assert affirmed_directions(claim_text, evidence_text) == set()
    assert copy_is_affirmed(claim_text, evidence_text) is False


@pytest.mark.parametrize(
    "joiner",
    ["却", "随后", "紧接着", "反倒", "殊不知", "XYZZY", "，", "；", "……", " "],
)
def test_unsettling_does_not_depend_on_naming_the_connective(joiner):
    """Subject inheritance works without enumerating these joiners."""

    claim = "阀门打开后水流变多"
    evidence = f"阀门打开后水流变多{joiner}下降了"
    assert unsettled_directions(claim, evidence) == {"up", "down"}
    assert copy_is_affirmed(claim, evidence) is False


def test_a_faithful_quote_of_both_directions_stays_verifiable():
    """A claim reporting both directions picks no side, so it smuggles nothing."""

    for text in ["阀门打开后水流变多却下降了", "阀门打开后水流变多不变少"]:
        assert unsettled_directions(text, text) == set()
        assert affirmed_directions(text, text) == {"up", "down"}
        assert copy_is_affirmed(text, text) is True


def test_a_second_subject_keeps_its_own_polarity():
    # 压力降低 has a subject of its own, so it does not unsettle a 温度升高 claim.
    assert unsettled_directions("温度升高", "加热使温度升高并且压力降低") == set()
    assert affirmed_directions("温度升高", "加热使温度升高并且压力降低") == {"up", "down"}
    assert affirmed_directions("压力降低", "加热使温度升高并且压力降低") == {"up", "down"}
    # The questioned occurrence is dropped, the asserted one still counts.
    assert scalar_directions("水流变多吗？是的，确实变多了") == {"up"}
    assert scalar_directions("如果温度升高就停止") == {"up"}
    assert affirmed_directions("水流变多", "水流变多") == {"up"}


@pytest.mark.parametrize(
    "evidence_text",
    [
        "阀门打开后水流变多少取决于阀门",
        "阀门打开后水流变多还是变少尚未确定",
        "阀门打开后水流变多？其实是下降了",
        "阀门打开后水流可能变多",
        "阀门打开后水流会变多吗",
        # Connectives and punctuation the code never enumerates.
        "阀门打开后水流变多却下降了",
        "阀门打开后水流变多然后下降了",
        "阀门打开后水流变多后来下降了",
        "阀门打开后水流变多结果下降了",
        "阀门打开后水流变多，随后下降了",
        "阀门打开后水流变多；不过下降了",
        # Incomplete alternative and A-not-A.
        "阀门打开后水流变多或少",
        "阀门打开后水流变多不变少",
        # Soft assertion particles.
        "阀门打开后水流变多吧",
        "阀门打开后水流变多不",
    ],
)
def test_unsettled_evidence_never_supports_a_one_sided_claim(evidence_text):
    outcome, fixtures = _polarity_outcome("阀门打开后水流变多", evidence_text)
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict != "supported"
    assert not verdict.supporting_ids
    assert not any(page.quality_label == "verified" for page in outcome.plan.pages)
    assert copy_is_affirmed("阀门打开后水流变多", evidence_text) is False
    doc, _topics, transcript, visual, plan = fixtures
    with pytest.raises(StrictVerificationError):
        verify_claims(
            doc,
            plan=plan,
            transcript=transcript,
            visual=visual,
            provider=FakeProvider(frames_caps()),
            quality_mode="strict",
        )


@pytest.mark.parametrize(
    ("claim_text", "evidence_text"),
    [
        ("the flow increases", "the flow increases then decreases"),
        ("the flow increases", "the flow increases however it decreases"),
    ],
)
def test_unsettled_english_evidence_never_supports_a_one_sided_claim(claim_text, evidence_text):
    outcome, _fixtures = _polarity_outcome(claim_text, evidence_text)
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict != "supported"
    assert not verdict.supporting_ids


def test_a_second_subject_still_reaches_strict_verified():
    outcome, _fixtures = _polarity_outcome(
        "温度升高", "加热使温度升高并且压力降低", quality_mode="strict"
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict == "supported"
    assert outcome.report.quality_mode == "strict"
    assert outcome.m3_gate_ok() is True


def test_measure_evidence_does_not_forge_a_contradiction():
    """变多少 is not evidence for 变多, but it does not oppose 变少 either."""

    outcome, _fixtures = _polarity_outcome(
        "阀门打开后水流变少", "阀门打开后水流变多少取决于阀门"
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-p")
    assert verdict.verdict == "insufficient"
    assert not verdict.contradicting_ids


def test_empty_strict_report_fails_m3_gate_and_requires_claim_closure():
    with pytest.raises(ValidationError, match="closed non-empty claim set"):
        VerificationReport(
            schema_version="1.0",
            source_id="src-demo",
            quality_mode="strict",
            verdicts=[],
        )
    doc, topics, transcript, visual = lecture_knowledge()
    plan = _plan_for(doc, topics, transcript, visual)
    empty = VerificationReport.model_construct(
        schema_version="1.0",
        source_id="src-demo",
        quality_mode="strict",
        verdicts=[],
        structural_errors=[],
        pending_review=[],
        coverage_gaps=[],
        repaired_claim_ids=[],
        removed_from_formal=[],
        human_samples=[],
        human_sampling_required=True,
    )
    assert (
        VerifyOutcome(report=empty, knowledge=doc, plan=plan).m3_gate_ok() is False
    )
    partial = VerificationReport(
        schema_version="1.0",
        source_id="src-demo",
        quality_mode="strict",
        verdicts=[
            {
                "claim_id": "claim-def",
                "verdict": "supported",
                "supporting_ids": ["cap-001"],
                "reason": "one claim only",
            }
        ],
    )
    assert (
        VerifyOutcome(report=partial, knowledge=doc, plan=plan).m3_gate_ok() is False
    )


def test_unrelated_valid_evidence_is_never_supported():
    unit = concept_unit(
        "unit-false",
        "claim-false",
        "板书写了公式。",
        ["cap-other"],
        start=0.0,
        end=10.0,
        modality="audio",
    )
    doc = knowledge(unit)
    topics = course_map([("topic-1", "无关", 0.0, 10.0)])
    transcript = make_transcript([("cap-other", 0.0, 10.0, "例如：把水倒入烧杯。")], duration=10.0)
    visual = make_visual([], duration=10.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    verdict = next(item for item in outcome.report.verdicts if item.claim_id == "claim-false")
    assert verdict.verdict != "supported"
    assert verdict.verdict == "insufficient"
    assert not verdict.supporting_ids
    with pytest.raises(ValidationError, match="supporting"):
        VerificationReport(
            schema_version="1.0",
            source_id="src-demo",
            quality_mode="strict",
            verdicts=[
                {
                    "claim_id": "claim-false",
                    "verdict": "supported",
                    "supporting_ids": [],
                    "reason": "fail open",
                }
            ],
        )


def test_successful_strict_path_has_no_unresolved_critical_claims():
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
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="strict",
    )
    assert outcome.report.quality_mode == "strict"
    assert all(item.verdict == "supported" for item in outcome.report.verdicts)
    assert all(item.supporting_ids for item in outcome.report.verdicts)
    allowed = {segment.id for segment in transcript.segments} | {item.id for item in visual.occurrences}
    assert all(set(item.supporting_ids) <= allowed for item in outcome.report.verdicts)
    assert not outcome.report.pending_review
    assert all(
        page.quality_label == "verified"
        or page.type in {"cover", "summary"}
        for page in outcome.plan.pages
    )
    assert not any("[DRAFT]" in page.notes for page in outcome.plan.pages)


@pytest.mark.parametrize("prefix", ["", "本节课程讨论实验背景和观察方法。" * 30])
@pytest.mark.parametrize(
    ("claim_text", "continuation"),
    [
        ("水流变多", "水流变多却下降了"),
        ("水流变多", "水流变多，随后下降了"),
        ("the flow increases", "the flow increases and then it decreases"),
        ("the flow increases", "the flow increases. Later it drops"),
        ("水流变多", "水流变多，然后它下降了"),
        ("水流变多", "水流变多然后流量下降了"),
        ("水位升高", "水位升高随后液位降低"),
        ("压力升高", "压力升高然后压强降低"),
        ("prices increase", "prices increase. they decrease afterwards"),
        ("变多", "变多却下降了"),
    ],
)
def test_discourse_polarities_fail_closed_with_padding(prefix, claim_text, continuation):
    evidence = prefix + continuation
    assert unsettled_directions(claim_text, evidence) == {"up", "down"}
    assert not copy_is_affirmed(claim_text, evidence)
    outcome, _ = _polarity_outcome(claim_text, evidence)
    assert outcome.report.verdicts[0].verdict == "insufficient"
    with pytest.raises(StrictVerificationError) as caught:
        _polarity_outcome(claim_text, evidence, quality_mode="strict")
    strict = caught.value.outcome
    assert strict is not None
    assert strict.report.quality_mode == "draft"
    assert not strict.m3_gate_ok()
    assert all(page.quality_label != "verified" for page in strict.plan.pages)

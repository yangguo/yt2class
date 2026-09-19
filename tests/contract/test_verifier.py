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
    copy_is_affirmed,
    verify_claims,
)
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import grounding_provider, concept_unit, course_map, knowledge, lecture_knowledge


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
        provider=grounding_provider(),
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
    assert verdict.verdict == "insufficient"


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
        provider=grounding_provider(),
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
    assert verdict.verdict == "insufficient"
    assert not verdict.contradicting_ids
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
        provider=grounding_provider(),
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
    assert len(repairs) == 1 and repairs[0].startswith("verifier:repair:claim-r:")
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
    assert len(repairs) == 1 and repairs[0].startswith("verifier:repair:claim-r:")
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
        provider=grounding_provider(),
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




@pytest.mark.parametrize("text,evidence", [
    ("El caudal aumenta", "El flujo se incrementa"),
    ("يزداد التدفق", "التدفق يرتفع"),
    ("流量增加", "水流变多"),
])
@pytest.mark.parametrize("verdict", ["supported", "contradicted", "insufficient"])
def test_semantic_verdict_is_provider_owned(text, evidence, verdict):
    from yt2class.stages.verify_claims import check_grounding
    claim = KnowledgeClaim(id="claim", text=text, evidence_ids=["cap"], status="insufficient")
    provider = grounding_provider(verdict=verdict)
    result = check_grounding(claim, {"cap": evidence}, provider=provider)
    assert result.verdict == verdict
    assert result.passed == (verdict == "supported")
    assert provider.last_payload["evidence"] == {"cap": evidence}


@pytest.mark.parametrize("provider", [None, FakeProvider(frames_caps()),
    FakeProvider(frames_caps(), timeout=True), FakeProvider(frames_caps(), omit_structured=True),
    FakeProvider(frames_caps(), sequential=[RuntimeError("unavailable")])])
def test_missing_or_failed_provider_never_affirms_even_identical_text(provider):
    from yt2class.stages.verify_claims import check_grounding
    claim = KnowledgeClaim(id="claim", text="Identical text", evidence_ids=["cap"], status="insufficient")
    result = check_grounding(claim, {"cap": claim.text}, provider=provider)
    assert result.verdict == "insufficient"
    assert not result.passed
    assert not copy_is_affirmed(claim.text, claim.text, provider=provider)


@pytest.mark.parametrize("row", [
    {"claim_id": "other", "verdict": "supported", "supporting_ids": ["cap"], "reason": "yes"},
    {"claim_id": "claim", "verdict": "supported", "supporting_ids": ["unknown"], "reason": "yes"},
    {"claim_id": "claim", "verdict": "supported", "reason": "yes"},
    {"claim_id": "claim", "verdict": "contradicted", "reason": "no"},
    {"claim_id": "claim", "verdict": "maybe", "reason": "unsure"},
])
def test_invalid_provider_verdict_fails_closed(row):
    from yt2class.stages.verify_claims import check_grounding
    claim = KnowledgeClaim(id="claim", text="text", evidence_ids=["cap"], status="insufficient")
    result = check_grounding(claim, {"cap": "text"}, provider=FakeProvider(
        frames_caps(), structured={"verdicts": [row]}))
    assert result.verdict == "insufficient"


@pytest.mark.parametrize("index", [{}, {"cap": ""}, {"cap": "  "}])
def test_empty_evidence_cannot_be_overridden_by_provider(index):
    from yt2class.stages.verify_claims import check_grounding
    claim = KnowledgeClaim(id="claim", text="text", evidence_ids=["cap"], status="insufficient")
    assert not check_grounding(claim, index, provider=grounding_provider()).passed


@pytest.mark.parametrize("repaired_verdict", ["supported", "insufficient", "contradicted"])
def test_repair_requires_fresh_provider_grounding(repaired_verdict):
    unit = concept_unit("unit", "claim", "Original", ["cap"], start=0, end=10, modality="audio")
    doc = knowledge(unit)
    transcript = make_transcript([("cap", 0, 10, "Evidence")], duration=10)
    visual = make_visual([], duration=10)
    topics = course_map([("topic", "Topic", 0, 10)])
    plan = _plan_for(doc, topics, transcript, visual, target_pages=4, max_pages=6)
    checked = []

    def respond(provider, request):
        payload = provider.last_payload
        if "claim" in payload:
            return {"text": "Repaired"}
        claim = payload["claims"][0]
        checked.append(claim["text"])
        verdict = repaired_verdict if claim["text"] == "Repaired" else "insufficient"
        return {"verdicts": [{"claim_id": claim["id"], "verdict": verdict,
            "supporting_ids": claim["evidence_ids"] if verdict == "supported" else [],
            "contradicting_ids": claim["evidence_ids"] if verdict == "contradicted" else [],
            "reason": "scripted verification"}]}

    outcome = verify_claims(doc, plan=plan, transcript=transcript, visual=visual,
                           provider=FakeProvider(frames_caps(), responder=respond))
    assert checked[:2] == ["Original", "Repaired"]
    assert outcome.report.verdicts[0].verdict == repaired_verdict
    assert outcome.report.repaired_claim_ids == ["claim"]


def test_pedagogical_ordinals_soften_number_checks():
    from yt2class.stages.verify_claims import check_numbers

    claim = KnowledgeClaim(
        id="claim-ped",
        text="用法2 表示比例；第2条是公告体。",
        evidence_ids=["cap-ped"],
        status="draft",
    )
    result = check_numbers(claim, "这是书面公告中的比例用法说明。")
    assert result.passed


def test_caption_citations_require_temporal_overlap_with_unit():
    from yt2class.domain.knowledge import KnowledgeUnit
    from yt2class.stages.verify_claims import run_claim_checks

    unit = KnowledgeUnit(
        id="unit-sense",
        topic_id="topic-u2",
        segment_ids=["seg-1"],
        start_seconds=40.0,
        end_seconds=60.0,
        kind="concept",
        claims=[
            KnowledgeClaim(
                id="claim-sense",
                text="用法2 表示比例单位。",
                evidence_ids=["cap-far"],
                status="draft",
                modality="audio",
            )
        ],
        relations=[],
        visual_candidates=[],
    )
    transcript = make_transcript(
        [("cap-far", 0.0, 10.0, "用法2 是比例单位。")],
        duration=60.0,
    )
    visual = make_visual([], duration=60.0)
    checks = run_claim_checks(
        unit.claims[0],
        unit=unit,
        transcript=transcript,
        visual=visual,
        allowed={"cap-far"},
        index={"cap-far": "用法2 是比例单位。"},
        provider=None,
    )
    temporal = next(item for item in checks if item.note.startswith("caption"))
    assert not temporal.passed


def test_draft_mode_keeps_insufficient_examples_when_sense_concept_supported():
    concept = concept_unit(
        "unit-sense",
        "claim-sense",
        "用法2 表示比例单位。",
        ["cap-sense"],
        topic_id="topic-u2",
        start=40.0,
        end=60.0,
    )
    example = concept_unit(
        "unit-ex",
        "claim-ex",
        "例：一個につき五百円。",
        ["cap-ex"],
        topic_id="topic-u2",
        start=40.0,
        end=60.0,
        kind="example",
    )
    doc = knowledge(concept, example)
    topics = course_map([("topic-u2", "用法2", 40.0, 60.0)])
    transcript = make_transcript(
        [
            ("cap-sense", 40.0, 55.0, "用法2 是比例单位。"),
            ("cap-ex", 40.0, 55.0, "五百円です。"),
        ],
        duration=60.0,
    )
    visual = make_visual([], duration=60.0)
    plan = _plan_for(doc, topics, transcript, visual, target_pages=5, max_pages=6)

    def respond(provider, request):
        payload = provider.last_payload or {}
        rows = []
        for claim in payload.get("claims") or []:
            verdict = "supported" if claim["id"] == "claim-sense" else "insufficient"
            rows.append(
                {
                    "claim_id": claim["id"],
                    "verdict": verdict,
                    "supporting_ids": claim["evidence_ids"] if verdict == "supported" else [],
                    "contradicting_ids": [],
                    "reason": "scripted",
                }
            )
        return {"verdicts": rows}

    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps(), responder=respond),
        quality_mode="draft",
    )
    assert "claim-ex" not in outcome.report.removed_from_formal
    assert any("claim-ex" in page.claim_ids for page in outcome.plan.pages)

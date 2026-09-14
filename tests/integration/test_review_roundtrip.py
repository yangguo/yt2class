from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.editorial import relabel_page
from yt2class.domain.review import (
    IllegalReviewOpError,
    ReviewBundle,
    ReviewEdits,
    StaleReviewError,
)
from yt2class.domain.verification import StrictClosureError, VerificationReport
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.review import (
    apply_review_edits,
    build_review_bundle,
    render_review_html,
    stub_binder,
    stub_renderer,
)
from yt2class.stages.verify_claims import page_copy_grounded, verify_claims
from tests.helpers.m2 import DIGEST_A, frames_caps, make_transcript, make_visual
from tests.helpers.m3 import grounding_provider, concept_unit, course_map, knowledge, lecture_knowledge


def _verified_bundle():
    doc, topics, transcript, visual = lecture_knowledge()
    provider = grounding_provider()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=10,
        max_pages=12,
    )
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
    )
    return outcome, transcript, visual, topics


def test_review_bundle_shows_frames_claims_transcript_time_links_and_omissions():
    doc, topics, transcript, visual = lecture_knowledge()
    provider = grounding_provider()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=4,
        max_pages=4,
    )
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
    )
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
        source_url="https://www.youtube.com/watch?v=dQw4w9wgXcQ",
    )
    assert bundle.pages
    assert bundle.omitted_topics or outcome.plan.omissions
    html = render_review_html(bundle)
    assert "review" in html.lower()
    assert any(page.claim_texts for page in bundle.pages)
    assert any(page.transcript_excerpts for page in bundle.pages)
    assert any("t=" in link for page in bundle.pages for link in page.time_links)
    assert "dQw4w9wgXcQ" in html
    content = next(item for item in bundle.pages if item.page.type == "content")
    assert content.frame_ids or content.page.layout == "text"


def test_review_html_marks_draft_and_evidence_only():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        target_pages=8,
        max_pages=10,
    )
    draft = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        quality_mode="draft",
    )
    evidence = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        quality_mode="evidence-only",
    )
    draft_html = render_review_html(
        build_review_bundle(
            knowledge=draft.knowledge,
            plan=draft.plan,
            report=draft.report,
            transcript=transcript,
            visual=visual,
            course_map=topics,
        )
    )
    evidence_html = render_review_html(
        build_review_bundle(
            knowledge=evidence.knowledge,
            plan=evidence.plan,
            report=evidence.report,
            transcript=transcript,
            visual=visual,
            course_map=topics,
        )
    )
    assert "[DRAFT]" in draft_html or "draft" in draft_html.lower()
    assert "verified" not in evidence_html.lower() or "[EVIDENCE-ONLY]" in evidence_html
    assert "[EVIDENCE-ONLY]" in evidence_html
    assert "不能当作已核验" in evidence_html


def test_allowed_ops_and_rejected_unknown_ops():
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    content = next(item.page for item in bundle.pages if item.page.type == "content")
    allowed = ReviewEdits(
        revision=bundle.revision,
        baseline_hashes=bundle.baseline_hashes,
        ops=[
            {"op": "edit_copy", "page_id": content.id, "title": "改写后的标题", "notes": content.notes},
            {"op": "lock", "page_id": content.id},
        ],
    )
    updated = apply_review_edits(
        bundle,
        allowed,
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
    )
    changed = next(page for page in updated.plan.pages if page.id == content.id)
    assert changed.title == "改写后的标题"
    assert changed.locked is True

    with pytest.raises(IllegalReviewOpError):
        apply_review_edits(
            bundle,
            ReviewEdits.model_validate(
                {
                    "revision": bundle.revision,
                    "baseline_hashes": bundle.baseline_hashes,
                    "ops": [{"op": "edit_copy", "page_id": content.id, "title": "x"}],
                }
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
            allowed_ops=(),
        )


@pytest.mark.parametrize("copy_verdict", ["supported", "contradicted", "insufficient"])
def test_edit_copy_requires_provider_grounding(copy_verdict):
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
    provider = grounding_provider()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=4,
        max_pages=6,
    )
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        quality_mode="strict",
    )
    assert any(page.quality_label == "verified" and page.type == "content" for page in outcome.plan.pages)
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    target = next(page for page in bundle.plan.pages if "claim-ok" in page.claim_ids)
    updated = apply_review_edits(
        bundle,
        ReviewEdits(
            revision=bundle.revision,
            baseline_hashes=bundle.baseline_hashes,
            ops=[
                {
                    "op": "edit_copy",
                    "page_id": target.id,
                    "title": "El flujo aumenta.",
                    "notes": "El caudal se incrementa.",
                    "body_points": ["El flujo aumenta."],
                }
            ],
        ),
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(copy_verdict=copy_verdict),
        quality_mode="strict",
    )
    changed = next(page for page in updated.plan.pages if page.id == target.id)
    assert changed.title == "El flujo aumenta."
    assert changed.quality_label == ("verified" if copy_verdict == "supported" else "draft")


def _forged_strict_report(claim_ids, *, source_id="src-demo"):
    return VerificationReport(
        schema_version="1.0",
        source_id=source_id,
        quality_mode="strict",
        verdicts=[
            {
                "claim_id": claim_id,
                "verdict": "supported",
                "supporting_ids": ["cap-001"],
                "reason": "forged strict verdict",
            }
            for claim_id in claim_ids
        ],
    )


def test_partial_strict_report_cannot_render_verified_review_pages():
    doc, topics, transcript, visual = lecture_knowledge()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        target_pages=10,
        max_pages=12,
    )
    verified_plan = plan.model_copy(
        update={"pages": [relabel_page(page, "verified") for page in plan.pages]}
    )
    all_claim_ids = [claim.id for claim in doc.iter_claims()]
    assert len(all_claim_ids) > 1

    def render(report, target_plan=verified_plan):
        return build_review_bundle(
            knowledge=doc,
            plan=target_plan,
            report=report,
            transcript=transcript,
            visual=visual,
            course_map=topics,
        )

    with pytest.raises(StrictClosureError, match="missing verdicts"):
        render(_forged_strict_report(all_claim_ids[:1]))
    with pytest.raises(StrictClosureError, match="unknown claims"):
        render(_forged_strict_report([*all_claim_ids, "claim-forged"]))
    with pytest.raises(StrictClosureError, match="strict verification report"):
        render(
            VerificationReport(
                schema_version="1.0",
                source_id="src-demo",
                quality_mode="draft",
                verdicts=[
                    {
                        "claim_id": claim_id,
                        "verdict": "supported",
                        "supporting_ids": ["cap-001"],
                        "reason": "draft report behind verified pages",
                    }
                    for claim_id in all_claim_ids
                ],
            )
        )
    # A partial strict report is refused even when no page claims to be verified.
    with pytest.raises(StrictClosureError, match="missing verdicts"):
        render(_forged_strict_report(all_claim_ids[:1]), plan)


def test_review_bundle_rejects_verified_pages_without_supported_claims():
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    assert bundle.report.quality_mode == "draft"
    forged = bundle.model_copy(
        update={
            "pages": [
                view.model_copy(update={"page": relabel_page(view.page, "verified")})
                for view in bundle.pages
            ]
        }
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="verified review pages require a strict"):
        ReviewBundle.model_validate(forged)


def test_review_cli_refuses_a_forged_partial_strict_report(tmp_path):
    from typer.testing import CliRunner

    from yt2class.cli import app

    doc, topics, transcript, visual = lecture_knowledge()
    knowledge_path = tmp_path / "knowledge.json"
    transcript_path = tmp_path / "transcript.json"
    visual_path = tmp_path / "visual.json"
    course_path = tmp_path / "course-map.json"
    report_path = tmp_path / "verification-report.json"
    knowledge_path.write_text(doc.model_dump_json(), encoding="utf-8")
    transcript_path.write_text(transcript.model_dump_json(), encoding="utf-8")
    visual_path.write_text(visual.model_dump_json(), encoding="utf-8")
    course_path.write_text(topics.model_dump_json(), encoding="utf-8")
    output = tmp_path / "editorial"
    runner = CliRunner()
    planned = runner.invoke(
        app,
        [
            "plan",
            "--knowledge",
            str(knowledge_path),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--course-map",
            str(course_path),
            "--output",
            str(output),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert planned.exit_code == 0, planned.stdout + planned.stderr
    report_path.write_text(
        _forged_strict_report([next(iter(doc.iter_claims())).id]).model_dump_json(),
        encoding="utf-8",
    )
    reviewed = runner.invoke(
        app,
        [
            "review",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--report",
            str(report_path),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert reviewed.exit_code == 2, reviewed.stdout + reviewed.stderr
    assert "refused strict render" in reviewed.stdout + reviewed.stderr
    assert not (output / "review.html").exists()


def test_rejected_or_unrelated_pick_asset_is_rejected():
    unit = concept_unit(
        "unit-ok",
        "claim-ok",
        "这不是自动词。",
        ["cap-ok", "frame-ok"],
        start=0.0,
        end=10.0,
        frames=["frame-ok"],
    )
    other = concept_unit(
        "unit-other",
        "claim-other",
        "例如：把水倒入烧杯。",
        ["cap-other", "frame-other"],
        start=10.0,
        end=20.0,
        kind="example",
        frames=["frame-other"],
    )
    doc = knowledge(unit, other)
    topics = course_map([("topic-1", "主题", 0.0, 20.0)])
    transcript = make_transcript(
        [("cap-ok", 0.0, 10.0, "这不是自动词。"), ("cap-other", 10.0, 20.0, "例如：把水倒入烧杯。")],
        duration=20.0,
    )
    visual = make_visual(
        [
            ("frame-ok", 4.0, "scene-001"),
            ("frame-other", 14.0, "scene-001"),
            ("frame-rejected", 16.0, "scene-001"),
        ],
        duration=20.0,
        ocr=[("ocr-ok", "frame-ok", "自动词 ではない")],
    )
    visual = visual.model_copy(
        update={
            "occurrences": [
                item.model_copy(update={"reject_reason": "blurry"})
                if item.id == "frame-rejected"
                else item
                for item in visual.occurrences
            ]
        }
    )
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
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
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    assert "frame-rejected" not in bundle.allowed_frame_ids
    target = next(page for page in bundle.plan.pages if "claim-ok" in page.claim_ids)
    with pytest.raises(IllegalReviewOpError, match="rejected|unrelated|allowed"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision,
                baseline_hashes=bundle.baseline_hashes,
                ops=[{"op": "pick_asset", "page_id": target.id, "frame_ids": ["frame-rejected"]}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )
    with pytest.raises(IllegalReviewOpError, match="rejected|unrelated|allowed"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision,
                baseline_hashes=bundle.baseline_hashes,
                ops=[{"op": "pick_asset", "page_id": target.id, "frame_ids": ["frame-other"]}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )


def test_hostile_review_html_is_escaped_and_not_executed():
    outcome, transcript, visual, topics = _verified_bundle()
    hostile_plan = outcome.plan.model_copy(deep=True)
    hostile_plan.pages[0] = hostile_plan.pages[0].model_copy(
        update={
            "title": "<img src=x onerror=alert(1)>",
            "notes": "</script><script>alert(1)</script>",
        }
    )
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=hostile_plan,
        report=outcome.report,
        transcript=transcript.model_copy(
            update={
                "segments": [
                    transcript.segments[0].model_copy(
                        update={"text_original": "<svg onload=alert(1)>板书"}
                    ),
                    *transcript.segments[1:],
                ]
            }
        ),
        visual=visual,
        course_map=topics,
        source_url="javascript:alert(1)",
    )
    html = render_review_html(bundle)
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html
    assert "innerHTML" not in html
    assert "textContent" in html
    assert "setAttribute" in html
    assert "\\u003c" in html
    assert "javascript:alert(1)" not in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert "</script><script>alert(1)</script>" not in html


def test_two_review_rounds_repair_a_claim_only_once():
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
    repairs: list[str] = []

    def refuse_and_count(provider, request):
        if request.role == "verifier" and "repair" in request.request_id:
            repairs.append(request.request_id)
            return {"text": "加热 15 分钟。", "evidence_ids": ["cap-r"]}
        return {"verdicts": (provider.last_payload or {}).get("draft_verdicts") or []}

    provider = FakeProvider(frames_caps(), responder=refuse_and_count)
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        target_pages=4,
        max_pages=6,
    )
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
    )
    assert len(repairs) == 1 and repairs[0].startswith("verifier:repair:claim-r:")
    # Keep the pre-formal page that still cites the failing claim so the
    # second review round re-enters the verifier for that lifetime.
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    target = next(page for page in bundle.plan.pages if "claim-r" in page.claim_ids)
    first = apply_review_edits(
        bundle,
        ReviewEdits(
            revision=bundle.revision,
            baseline_hashes=bundle.baseline_hashes,
            ops=[{"op": "edit_copy", "page_id": target.id, "title": "加热 15 分钟。"}],
        ),
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=provider,
    )
    second = verify_claims(
        first.knowledge,
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


def test_pick_asset_delete_and_reorder():
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    content_pages = [item.page for item in bundle.pages if item.page.type == "content"]
    target = next((page for page in content_pages if page.layout != "text"), content_pages[0])
    ops = []
    victim = next(page for page in content_pages if page.id != target.id)
    ops.append({"op": "delete", "page_id": victim.id})
    remaining = [page.id for page in outcome.plan.pages if page.id != victim.id]
    reordered = list(reversed(remaining))
    # keep cover first / summary last if present
    cover = next((page.id for page in outcome.plan.pages if page.type == "cover"), None)
    summary = next((page.id for page in outcome.plan.pages if page.type == "summary"), None)
    body = [item for item in reordered if item not in {cover, summary}]
    order = [item for item in [cover, *reversed(body), summary] if item]
    ops.append({"op": "reorder", "order": order})
    updated = apply_review_edits(
        bundle,
        ReviewEdits(revision=bundle.revision, baseline_hashes=bundle.baseline_hashes, ops=ops),
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
    )
    assert victim.id not in {page.id for page in updated.plan.pages}
    assert [page.id for page in updated.plan.pages] == order


def test_stale_revision_and_hash_are_rejected():
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    page_id = bundle.plan.pages[0].id
    with pytest.raises(StaleReviewError, match="revision"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision + 1,
                baseline_hashes=bundle.baseline_hashes,
                ops=[{"op": "lock", "page_id": page_id}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )
    stale_hashes = dict(bundle.baseline_hashes)
    stale_hashes["knowledge"] = DIGEST_A
    with pytest.raises(StaleReviewError, match="hash"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision,
                baseline_hashes=stale_hashes,
                ops=[{"op": "lock", "page_id": page_id}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )
    assert "verification" in bundle.baseline_hashes
    stale_report = dict(bundle.baseline_hashes)
    stale_report["verification"] = DIGEST_A
    with pytest.raises(StaleReviewError, match="hash"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision,
                baseline_hashes=stale_report,
                ops=[{"op": "lock", "page_id": page_id}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )


def test_after_edits_only_affected_claims_are_reverified():
    unit = concept_unit(
        "unit-a",
        "claim-a",
        "这不是自动词。",
        ["cap-a", "frame-a"],
        start=0.0,
        end=10.0,
        frames=["frame-a"],
    )
    other = concept_unit(
        "unit-b",
        "claim-b",
        "例如：把水倒入烧杯。",
        ["cap-b", "frame-b"],
        start=10.0,
        end=20.0,
        kind="example",
        frames=["frame-b"],
    )
    doc = knowledge(unit, other)
    topics = course_map([("topic-1", "主题", 0.0, 20.0)])
    transcript = make_transcript(
        [("cap-a", 0.0, 10.0, "这不是自动词。"), ("cap-b", 10.0, 20.0, "例如：把水倒入烧杯。")],
        duration=20.0,
    )
    visual = make_visual(
        [("frame-a", 4.0, "scene-001"), ("frame-b", 14.0, "scene-001")],
        duration=20.0,
        ocr=[("ocr-a", "frame-a", "自动词 ではない")],
    )
    provider = grounding_provider()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=4,
        max_pages=6,
    )
    outcome = verify_claims(
        doc, plan=plan, transcript=transcript, visual=visual, provider=provider, quality_mode="draft"
    )
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    target = next(page for page in bundle.plan.pages if "claim-a" in page.claim_ids)
    counted = {"ids": []}

    def tracking(provider_obj, request):
        payload = provider_obj.last_payload or {}
        if request.role == "verifier":
            for item in payload.get("claims") or []:
                counted["ids"].append(item.get("id"))
        return {"verdicts": payload.get("draft_verdicts") or []}

    apply_review_edits(
        bundle,
        ReviewEdits(
            revision=bundle.revision,
            baseline_hashes=bundle.baseline_hashes,
            ops=[{"op": "edit_copy", "page_id": target.id, "title": "人工改写的标题"}],
        ),
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps(), responder=tracking),
    )
    assert "claim-a" in counted["ids"]
    assert "claim-b" not in counted["ids"]


def test_binder_and_renderer_stubs_remain_for_default_review_path():
    assert stub_binder()["status"] == "deferred"
    assert stub_renderer()["status"] == "deferred"
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    result = apply_review_edits(
        bundle,
        ReviewEdits(
            revision=bundle.revision,
            baseline_hashes=bundle.baseline_hashes,
            ops=[{"op": "lock", "page_id": bundle.plan.pages[0].id}],
        ),
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        binder=stub_binder,
        renderer=stub_renderer,
    )
    assert result.binder_status["milestone"] == "M4"
    assert result.renderer_status["milestone"] == "M4"


def test_plan_verify_review_cli_fake_provider(tmp_path):
    from typer.testing import CliRunner

    from yt2class.cli import app

    doc, topics, transcript, visual = lecture_knowledge()
    knowledge_path = tmp_path / "knowledge.json"
    transcript_path = tmp_path / "transcript.json"
    visual_path = tmp_path / "visual.json"
    course_path = tmp_path / "course-map.json"
    knowledge_path.write_text(doc.model_dump_json(), encoding="utf-8")
    transcript_path.write_text(transcript.model_dump_json(), encoding="utf-8")
    visual_path.write_text(visual.model_dump_json(), encoding="utf-8")
    course_path.write_text(topics.model_dump_json(), encoding="utf-8")
    output = tmp_path / "editorial"
    runner = CliRunner()
    planned = runner.invoke(
        app,
        [
            "plan",
            "--knowledge",
            str(knowledge_path),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--course-map",
            str(course_path),
            "--output",
            str(output),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert planned.exit_code == 0, planned.stdout + planned.stderr
    verified = runner.invoke(
        app,
        [
            "verify",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--mode",
            "draft",
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    reviewed = runner.invoke(
        app,
        [
            "review",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--report",
            str(output / "verification-report.json"),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert reviewed.exit_code == 0, reviewed.stdout + reviewed.stderr
    assert (output / "review.html").exists()
    rejected = runner.invoke(
        app,
        [
            "plan",
            "--knowledge",
            str(knowledge_path),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--provider",
            "openai",
        ],
        prog_name="yt2class",
    )
    assert rejected.exit_code == 2


def test_two_cli_review_rounds_preserve_revision_and_block_second_repair(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from yt2class.adapters.providers.base import FakeProvider
    from yt2class.cli import app
    from yt2class.domain.knowledge import KnowledgeDocument
    from yt2class.domain.review import ReviewBundle
    from yt2class.domain.verification import VerificationReport

    repairs: list[str] = []
    original = FakeProvider.complete

    def wrapped(self, request, *, cancel_event=None):
        if request.role == "verifier" and "repair" in request.request_id:
            repairs.append(request.request_id)
        saved_responder = self._responder
        if request.role == "verifier" and "ground" in request.request_id:
            self._responder = grounding_provider()._responder
        try:
            return original(self, request, cancel_event=cancel_event)
        finally:
            self._responder = saved_responder

    monkeypatch.setattr(FakeProvider, "complete", wrapped)

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
    knowledge_path = tmp_path / "knowledge.json"
    transcript_path = tmp_path / "transcript.json"
    visual_path = tmp_path / "visual.json"
    course_path = tmp_path / "course-map.json"
    knowledge_path.write_text(doc.model_dump_json(), encoding="utf-8")
    transcript_path.write_text(transcript.model_dump_json(), encoding="utf-8")
    visual_path.write_text(visual.model_dump_json(), encoding="utf-8")
    course_path.write_text(topics.model_dump_json(), encoding="utf-8")
    output = tmp_path / "editorial"
    runner = CliRunner()
    planned = runner.invoke(
        app,
        [
            "plan",
            "--knowledge",
            str(knowledge_path),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--course-map",
            str(course_path),
            "--output",
            str(output),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert planned.exit_code == 0, planned.stdout + planned.stderr
    verified = runner.invoke(
        app,
        [
            "verify",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--mode",
            "draft",
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    assert (output / "knowledge.json").exists()
    first_report = VerificationReport.model_validate_json(
        (output / "verification-report.json").read_text(encoding="utf-8")
    )
    assert "claim-r" in first_report.repaired_claim_ids
    assert len(repairs) == 1 and repairs[0].startswith("verifier:repair:claim-r:")
    reviewed = runner.invoke(
        app,
        [
            "review",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--report",
            str(output / "verification-report.json"),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert reviewed.exit_code == 0, reviewed.stdout + reviewed.stderr
    bundle = ReviewBundle.model_validate_json((output / "review.json").read_text(encoding="utf-8"))
    assert bundle.revision == 1
    target = next(page for page in bundle.plan.pages if page.claim_ids)
    edits_one = tmp_path / "edits-1.json"
    edits_one.write_text(
        ReviewEdits(
            revision=bundle.revision,
            baseline_hashes=bundle.baseline_hashes,
            ops=[{"op": "edit_copy", "page_id": target.id, "title": target.title}],
        ).model_dump_json(),
        encoding="utf-8",
    )
    first_apply = runner.invoke(
        app,
        [
            "review",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--report",
            str(output / "verification-report.json"),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--apply",
            str(edits_one),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert first_apply.exit_code == 0, first_apply.stdout + first_apply.stderr
    after_first = ReviewBundle.model_validate_json((output / "review.json").read_text(encoding="utf-8"))
    assert after_first.revision == 2
    target = next(page for page in after_first.plan.pages if page.id == target.id)
    edits_two = tmp_path / "edits-2.json"
    edits_two.write_text(
        ReviewEdits(
            revision=after_first.revision,
            baseline_hashes=after_first.baseline_hashes,
            ops=[{"op": "lock", "page_id": target.id}],
        ).model_dump_json(),
        encoding="utf-8",
    )
    second_apply = runner.invoke(
        app,
        [
            "review",
            "--knowledge",
            str(knowledge_path),
            "--plan",
            str(output / "editorial-plan.json"),
            "--report",
            str(output / "verification-report.json"),
            "--transcript",
            str(transcript_path),
            "--visual",
            str(visual_path),
            "--output",
            str(output),
            "--apply",
            str(edits_two),
            "--provider",
            "fake",
        ],
        prog_name="yt2class",
    )
    assert second_apply.exit_code == 0, second_apply.stdout + second_apply.stderr
    after_second = ReviewBundle.model_validate_json((output / "review.json").read_text(encoding="utf-8"))
    assert after_second.revision == 3
    assert len(repairs) == 1 and repairs[0].startswith("verifier:repair:claim-r:")
    second_report = VerificationReport.model_validate_json(
        (output / "verification-report.json").read_text(encoding="utf-8")
    )
    assert second_report.repaired_claim_ids == first_report.repaired_claim_ids
    persisted = KnowledgeDocument.model_validate_json((output / "knowledge.json").read_text(encoding="utf-8"))
    assert any(claim.id == "claim-r" for claim in persisted.iter_claims())


def test_fake_provider_demonstrates_m3_gate(tmp_path):
    doc, topics, transcript, visual = lecture_knowledge()
    provider = grounding_provider()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=10,
        max_pages=12,
    )
    draft = verify_claims(
        doc, plan=plan, transcript=transcript, visual=visual, provider=provider, quality_mode="draft"
    )
    bundle = build_review_bundle(
        knowledge=draft.knowledge,
        plan=draft.plan,
        report=draft.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    html_path = tmp_path / "review.html"
    json_path = tmp_path / "review.json"
    html_path.write_text(render_review_html(bundle), encoding="utf-8")
    json_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    dumped = json.loads(json_path.read_text(encoding="utf-8"))
    assert dumped["revision"] == 1
    assert "[DRAFT]" in html_path.read_text(encoding="utf-8") or dumped["report"]["quality_mode"] == "draft"
    if draft.report.quality_mode == "strict":
        assert all(item.verdict == "supported" for item in draft.report.verdicts)
    else:
        assert all(page.quality_label != "verified" or "[DRAFT]" not in page.notes for page in draft.plan.pages)
        assert any(page.quality_label in {"draft", "evidence-only"} for page in draft.plan.pages)

    ok = concept_unit(
        "unit-ok",
        "claim-ok",
        "这不是自动词。",
        ["cap-ok", "frame-ok"],
        start=0.0,
        end=10.0,
        frames=["frame-ok"],
    )
    ok_doc = knowledge(ok)
    ok_topics = course_map([("topic-1", "通过", 0.0, 10.0)])
    ok_transcript = make_transcript([("cap-ok", 0.0, 10.0, "这不是自动词。")], duration=10.0)
    ok_visual = make_visual(
        [("frame-ok", 4.0, "scene-001")],
        duration=10.0,
        ocr=[("ocr-ok", "frame-ok", "自动词 ではない")],
    )
    ok_plan = edit_deck(
        ok_doc,
        course_map=ok_topics,
        transcript=ok_transcript,
        visual=ok_visual,
        provider=grounding_provider(),
        target_pages=4,
        max_pages=6,
    )
    strict = verify_claims(
        ok_doc,
        plan=ok_plan,
        transcript=ok_transcript,
        visual=ok_visual,
        provider=grounding_provider(),
        quality_mode="strict",
    )
    assert strict.report.quality_mode == "strict"
    assert not strict.report.pending_review
    assert all(item.verdict == "supported" for item in strict.report.verdicts)

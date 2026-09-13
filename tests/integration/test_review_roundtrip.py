from __future__ import annotations

import json

import pytest

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.review import IllegalReviewOpError, ReviewEdits, StaleReviewError
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.review import (
    apply_review_edits,
    build_review_bundle,
    render_review_html,
    stub_binder,
    stub_renderer,
)
from yt2class.stages.verify_claims import verify_claims
from tests.helpers.m2 import DIGEST_A, frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, knowledge, lecture_knowledge


def _verified_bundle():
    doc, topics, transcript, visual = lecture_knowledge()
    provider = FakeProvider(frames_caps())
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
    outcome, transcript, visual, topics = _verified_bundle()
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
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
    )
    draft = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="draft",
    )
    evidence = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
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
        provider=FakeProvider(frames_caps()),
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
            provider=FakeProvider(frames_caps()),
            allowed_ops=(),
        )


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
    alt = next(
        (occ.id for occ in visual.occurrences if occ.id not in target.frame_ids),
        None,
    )
    ops = []
    if alt and target.layout in {"image-text", "comparison", "sequence"}:
        new_frames = list(target.frame_ids)
        if new_frames:
            new_frames[0] = alt
        else:
            new_frames = [alt]
        ops.append({"op": "pick_asset", "page_id": target.id, "frame_ids": new_frames[:3]})
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
        provider=FakeProvider(frames_caps()),
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
            provider=FakeProvider(frames_caps()),
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
            provider=FakeProvider(frames_caps()),
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
    provider = FakeProvider(frames_caps())
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


def test_binder_and_renderer_remain_m4_stubs():
    assert stub_binder()["milestone"] == "M4"
    assert stub_renderer()["milestone"] == "M4"
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
        provider=FakeProvider(frames_caps()),
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


def test_fake_provider_demonstrates_m3_gate(tmp_path):
    doc, topics, transcript, visual = lecture_knowledge()
    provider = FakeProvider(frames_caps())
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
        provider=FakeProvider(frames_caps()),
        target_pages=4,
        max_pages=6,
    )
    strict = verify_claims(
        ok_doc,
        plan=ok_plan,
        transcript=ok_transcript,
        visual=ok_visual,
        provider=FakeProvider(frames_caps()),
        quality_mode="strict",
    )
    assert strict.report.quality_mode == "strict"
    assert not strict.report.pending_review
    assert all(item.verdict == "supported" for item in strict.report.verdicts)

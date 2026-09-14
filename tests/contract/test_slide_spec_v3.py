"""Binder and SlideSpec 3.0 filesystem binding tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from PIL import Image

from yt2class.domain.editorial import EditorialPlan, PageIntent
from yt2class.domain.source import SourceManifest
from yt2class.stages.bind_spec import BindError, bind_editorial_plan, validate_bound_assets
from tests.helpers.m4 import seed_lecture_run


def test_bind_emits_physical_pages_for_all_layouts(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _topics = seed_lecture_run(tmp_path)
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    layouts = {
        (slide.type, slide.layout)
        for slide in bound.spec.slides
        if slide.type == "content"
    }
    assert ("content", "image-text") in layouts
    assert ("content", "comparison") in layouts
    assert ("content", "sequence") in layouts
    assert any(slide.type == "cover" for slide in bound.spec.slides)
    assert any(slide.type == "summary" for slide in bound.spec.slides)
    assert len(bound.spec.slides) >= len(outcome.plan.pages)
    validate_bound_assets(bound.spec, workspace.root)


def test_content_claims_paginate_without_dropping_ids(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    claim_ids = [f"extra-claim-{i}" for i in range(6)]
    from yt2class.domain.knowledge import KnowledgeClaim

    extra_claims = [
        KnowledgeClaim(
            id=cid,
            text=f"point {i}",
            evidence_ids=["cap-001", "frame-001"],
            status="draft",
            provenance="source",
        )
        for i, cid in enumerate(claim_ids)
    ]
    unit = outcome.knowledge.units[0].model_copy(update={"claims": extra_claims})
    knowledge = outcome.knowledge.model_copy(update={"units": [unit, *outcome.knowledge.units[1:]]})
    page = PageIntent(
        id="intent-text-many",
        type="content",
        layout="text",
        title="多点分页",
        claim_ids=claim_ids,
        frame_ids=[],
        notes="",
        selection_reason="pagination fixture",
        quality_label="draft",
    )
    plan = outcome.plan.model_copy(update={"pages": [outcome.plan.pages[0], page]})
    bound = bind_editorial_plan(
        plan=plan,
        knowledge=knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    text_slides = [s for s in bound.spec.slides if s.id == page.id or s.id.startswith(f"{page.id}-cont")]
    assert len(text_slides) == 2
    assert text_slides[1].continuation_of == page.id
    bound_ids = [cid for s in text_slides for cid in s.point_claim_ids]
    assert bound_ids == claim_ids


def test_quiz_claims_paginate_with_continuation(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    from yt2class.domain.knowledge import KnowledgeClaim

    claims = [
        KnowledgeClaim(
            id=f"quiz-{i}",
            text=f"Q{i}",
            evidence_ids=["cap-001"],
            status="draft",
            provenance="generated-practice",
        )
        for i in range(5)
    ]
    unit = outcome.knowledge.units[0].model_copy(update={"claims": claims})
    knowledge = outcome.knowledge.model_copy(update={"units": [unit, *outcome.knowledge.units[1:]]})
    quiz_page = PageIntent(
        id="intent-quiz-big",
        type="quiz",
        title="练习",
        claim_ids=[c.id for c in claims],
        frame_ids=[],
        notes="",
        selection_reason="quiz fixture",
        quality_label="draft",
    )
    plan = outcome.plan.model_copy(update={"pages": [outcome.plan.pages[0], quiz_page]})
    bound = bind_editorial_plan(
        plan=plan,
        knowledge=knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    quiz_slides = [s for s in bound.spec.slides if s.type == "quiz"]
    assert len(quiz_slides) == 2
    assert sum(len(s.questions) for s in quiz_slides) == 5


def test_bind_rejects_exceeding_editorial_max_pages(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    plan = outcome.plan.model_copy(update={"max_pages": 4, "target_pages": 4})
    with pytest.raises(BindError, match="max_pages"):
        bind_editorial_plan(
            plan=plan,
            knowledge=outcome.knowledge,
            report=outcome.report,
            source=source,
            transcript=transcript,
            visual=visual,
            workspace=workspace,
        )


def test_bind_rejects_symlink_escape(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (10, 10), "#fff").save(outside)
    link = workspace.root / "frames" / "evil.jpg"
    link.symlink_to(outside)
    asset = bound.spec.assets[0]
    evil_hash = sha256(outside.read_bytes()).hexdigest()
    tampered = bound.spec.model_copy(
        update={
            "assets": [
                item
                if item.id != asset.id
                else item.model_copy(update={"path": "frames/evil.jpg", "sha256": evil_hash})
                for item in bound.spec.assets
            ]
        }
    )
    with pytest.raises(BindError, match="symlink"):
        validate_bound_assets(tampered, workspace.root)


def test_bind_rejects_hash_mismatch(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    asset = bound.spec.assets[0]
    path = workspace.safe_path(asset.path)
    path.write_bytes(b"changed")
    with pytest.raises(BindError, match="hash"):
        validate_bound_assets(bound.spec, workspace.root)


def test_strict_verified_quality_requires_supported_claims(tmp_path: Path):
    from yt2class.stages.verify_claims import verify_claims
    from tests.helpers.m3 import grounding_provider

    workspace, outcome, source, transcript, visual, topics = seed_lecture_run(tmp_path)
    provider = grounding_provider()
    plan = outcome.plan.model_copy(
        update={
            "pages": [
                page.model_copy(update={"quality_label": "verified"}) for page in outcome.plan.pages
            ]
        }
    )
    strict = verify_claims(
        outcome.knowledge,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="strict",
    )
    bound = bind_editorial_plan(
        plan=strict.plan,
        knowledge=strict.knowledge,
        report=strict.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    assert bound.spec.quality_status == "verified"
    assert all(claim.verdict == "supported" for claim in bound.spec.claims)


def test_empty_summary_raises_readable_bind_error():
    from yt2class.stages.bind_spec import _bind_summary
    page = PageIntent(id="empty-summary", type="summary", title="Summary", claim_ids=[],
                      selection_reason="regression", quality_label="draft")
    with pytest.raises(BindError, match="empty-summary.*at least one claim"):
        _bind_summary(page, claims={})

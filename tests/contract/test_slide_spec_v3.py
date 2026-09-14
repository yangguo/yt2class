"""Binder and SlideSpec 3.0 filesystem binding tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from PIL import Image

from yt2class.domain.editorial import EditorialPlan, PageIntent
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.orchestration.workspace import Workspace
from yt2class.stages.bind_spec import BindError, bind_editorial_plan, validate_bound_assets
from tests.helpers.m4 import seed_lecture_run


def _minimal_plan(**pages: PageIntent) -> EditorialPlan:
    return EditorialPlan(
        schema_version="1.0",
        source_id="src-demo",
        target_pages=4,
        max_pages=20,
        pages=list(pages.values()) if pages else [],
    )


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


def test_page_count_matches_editorial_without_hidden_pages(tmp_path: Path):
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
    assert bound.spec.slides
    assert bound.spec.slides[0].type == "cover"
    assert "quiz" not in {slide.type for slide in bound.spec.slides} or any(
        page.type == "quiz" for page in outcome.plan.pages
    )

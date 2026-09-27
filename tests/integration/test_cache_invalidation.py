from __future__ import annotations

from pathlib import Path

import pytest

from yt2class.config import BuildSource, CourseConfig
from yt2class.orchestration import pipeline as orch_pipeline
from yt2class.orchestration.manifest_io import stage_record
from yt2class.orchestration.pipeline import execute_run
from yt2class.domain.editorial import EditorialPlan
from tests.helpers.m5 import build_evidence_bundle


def test_subtitle_change_invalidates_extract_cache(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    video = workspace.root / "media" / "source.mp4"
    vtt_a = tmp_path / "a.vtt"
    vtt_b = tmp_path / "b.vtt"
    vtt_a.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nA\n", encoding="utf-8")
    vtt_b.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nB\n", encoding="utf-8")
    calls = {"extract": 0}

    def fake_extract(source, **kwargs):
        calls["extract"] += 1
        return bundle

    monkeypatch.setattr(orch_pipeline, "run_ingest", lambda _ctx, _build: bundle.source)
    monkeypatch.setattr(orch_pipeline, "extract_evidence_locked", fake_extract)
    cfg = CourseConfig()
    build = BuildSource(source_id=bundle.source_id, video=video, subtitles=vtt_a)
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        stop_after="extract_evidence",
    )
    assert calls["extract"] == 1

    build_b = BuildSource(source_id=bundle.source_id, video=video, subtitles=vtt_b)
    execute_run(
        tmp_path,
        build=build_b,
        config=cfg,
        run_id=workspace.root.name,
        resume=True,
        stop_after="extract_evidence",
    )
    assert calls["extract"] == 2


def test_editorial_disk_wins_over_stale_edit_cache(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    cfg = CourseConfig()
    build = BuildSource(source_id=bundle.source_id)
    outcome = execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        stop_after="verify_claims",
    )
    record = stage_record(outcome.manifest, "edit_deck")
    assert record.cache_key is not None
    editorial = workspace.root / "editorial" / "editorial-plan.json"
    plan = EditorialPlan.model_validate_json(editorial.read_text(encoding="utf-8"))
    edited = plan.model_copy(
        update={
            "pages": [
                plan.pages[0].model_copy(update={"title": "人工修订标题"}),
                *plan.pages[1:],
            ]
        }
    )
    editorial.write_text(edited.model_dump_json(indent=2), encoding="utf-8")
    assert any(page.title == "人工修订标题" for page in edited.pages)
    from yt2class.orchestration.run_request import bump_review_revision

    bump_review_revision(workspace.root)
    calls = {"plan_deck": 0}
    real = orch_pipeline.plan_deck

    def counting(*args, **kwargs):
        calls["plan_deck"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orch_pipeline, "plan_deck", counting)
    calls["plan_deck"] = 0
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        resume=True,
        stop_after="verify_claims",
    )
    assert calls["plan_deck"] == 0
    reloaded = EditorialPlan.model_validate_json(editorial.read_text(encoding="utf-8"))
    assert any(page.title == "人工修订标题" for page in reloaded.pages)


def test_human_edit_survives_resume_with_revision_zero(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    cfg = CourseConfig()
    build = BuildSource(source_id=bundle.source_id)
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        stop_after="verify_claims",
    )
    editorial = workspace.root / "editorial" / "editorial-plan.json"
    plan = EditorialPlan.model_validate_json(editorial.read_text(encoding="utf-8"))
    edited = plan.model_copy(
        update={"pages": [plan.pages[0].model_copy(update={"title": "人工修订标题"}), *plan.pages[1:]]}
    )
    editorial.write_text(edited.model_dump_json(indent=2), encoding="utf-8")
    calls = {"plan_deck": 0}
    real = orch_pipeline.plan_deck

    def counting(*args, **kwargs):
        calls["plan_deck"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orch_pipeline, "plan_deck", counting)
    execute_run(
        tmp_path,
        build=BuildSource(source_id=bundle.source_id, run_id=workspace.root.name),
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        resume=True,
        stop_after="verify_claims",
    )
    assert calls["plan_deck"] == 0
    reloaded = EditorialPlan.model_validate_json(editorial.read_text(encoding="utf-8"))
    assert any(page.title == "人工修订标题" for page in reloaded.pages)


def test_content_coverage_is_written_and_tracks_the_final_plan_after_resume(tmp_path: Path):
    import json

    from yt2class.domain.knowledge import KnowledgeDocument
    from yt2class.domain.transcript import TranscriptDocument
    from yt2class.domain.verification import VerificationReport
    from yt2class.domain.visual import VisualCatalogue
    from yt2class.stages.content_coverage import build_content_coverage_report

    workspace, bundle = build_evidence_bundle(tmp_path)
    cfg = CourseConfig()
    build = BuildSource(source_id=bundle.source_id)
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        stop_after="verify_claims",
    )

    editorial = workspace.root / "editorial"
    coverage_path = editorial / "content-coverage.json"
    assert coverage_path.is_file()
    first_plan = EditorialPlan.model_validate_json(
        (editorial / "editorial-plan.json").read_text(encoding="utf-8")
    )
    edited = first_plan.model_copy(
        update={"pages": [first_plan.pages[0].model_copy(update={"title": "人工修订标题"}), *first_plan.pages[1:]]}
    )
    (editorial / "editorial-plan.json").write_text(edited.model_dump_json(indent=2), encoding="utf-8")

    execute_run(
        tmp_path,
        build=BuildSource(source_id=bundle.source_id, run_id=workspace.root.name),
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        resume=True,
        stop_after="verify_claims",
    )

    final_plan = EditorialPlan.model_validate_json(
        (editorial / "editorial-plan.json").read_text(encoding="utf-8")
    )
    final_knowledge = KnowledgeDocument.model_validate_json(
        (editorial / "knowledge.json").read_text(encoding="utf-8")
    )
    final_verification = VerificationReport.model_validate_json(
        (editorial / "verification-report.json").read_text(encoding="utf-8")
    )
    transcript = TranscriptDocument.model_validate_json(
        (workspace.root / "evidence" / "transcript-document.json").read_text(encoding="utf-8")
    )
    visual = VisualCatalogue.model_validate_json(
        (workspace.root / "evidence" / "visual-catalogue.json").read_text(encoding="utf-8")
    )
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    expected = build_content_coverage_report(
        final_knowledge,
        final_plan,
        transcript=transcript,
        visual=visual,
        verification=final_verification,
    )
    assert any(page.title == "人工修订标题" for page in final_plan.pages)
    assert coverage == expected
    assert coverage["digests"]["editorial_plan_sha256"] != build_content_coverage_report(
        final_knowledge, first_plan, transcript=transcript, visual=visual, verification=final_verification
    )["digests"]["editorial_plan_sha256"]


def test_downloaded_caption_change_invalidates_extract_inputs(tmp_path):
    import json
    from types import SimpleNamespace
    from yt2class.orchestration.pipeline import _extract_input_hashes

    workspace, bundle = build_evidence_bundle(tmp_path)
    source = bundle.source.model_copy(update={"kind": "youtube"})
    media = workspace.safe_path(source.media_path)
    caption = media.with_suffix(".ja.vtt")
    media.with_suffix(".captions.json").write_text(json.dumps({
        "filename": caption.name, "language": "ja", "origin": "auto-caption",
    }))
    ctx = SimpleNamespace(workspace=workspace, build=None)
    caption.write_text("first caption")
    first = _extract_input_hashes(ctx, source)
    caption.write_text("changed caption")
    assert _extract_input_hashes(ctx, source) != first

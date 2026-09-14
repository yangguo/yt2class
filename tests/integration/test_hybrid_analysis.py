"""Integration tests for hybrid / native-video routing with fake backends."""

from __future__ import annotations

import json
from pathlib import Path

from yt2class.adapters.providers.base import FakeProvider, ModelRequest
from yt2class.adapters.providers.native_video import FakeNativeVideoBackend, fake_native_adapter
from yt2class.adapters.providers.synthetic import course_responder, fake_course_provider
from yt2class.domain.knowledge import KnowledgeEvidenceRequest
from yt2class.domain.visual import VisualAsset
from yt2class.orchestration.analyze import analyze_course, write_analysis_artifacts
from yt2class.orchestration.edit import build_review
from yt2class.orchestration.hybrid_analysis import minimal_clip_range, unit_needs_native_upgrade
from yt2class.orchestration.scheduler import SchedulerConfig
from yt2class.stages.evidence_refinement import ExtractedClip
from yt2class.stages.review import apply_review_edits
from yt2class.domain.review import ReviewEdits, ReviewOp
from yt2class.domain.segment import AnalysisWindow
from tests.helpers.m2 import claim, frames_caps, lecture_fixture, make_transcript, make_visual, unit


def _sized_clip_extractor(full_media: Path):
    full_size = full_media.stat().st_size

    def extract(start: float, end: float, dest: Path) -> ExtractedClip:
        dest.mkdir(parents=True, exist_ok=True)
        rel = Path("clips") / f"clip_{start:.2f}_{end:.2f}.mp4"
        clip_path = dest.parent / rel
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(0.1, end - start)
        clip_path.write_bytes(b"CLIP" * max(1, int(duration * 10)))
        asset = VisualAsset(
            id=f"asset-clip-{start:.0f}",
            role="clip",
            path=str(rel).replace("\\", "/"),
            sha256="c" * 64,
            mime_type="video/mp4",
            width=640,
            height=360,
        )
        return ExtractedClip(asset=asset, start_seconds=start, end_seconds=end)

    extract.full_media_size = full_size  # type: ignore[attr-defined]
    return extract


def _procedure_segment_responder(provider: FakeProvider, request: ModelRequest):
    if request.role != "segment":
        return course_responder(provider, request)
    payload = provider.last_payload or {}
    segment_id = payload.get("segment_id") or "seg-0001"
    return {
        "units": [
            {
                "id": f"unit-{segment_id}-1",
                "topic_id": (payload.get("course_context") or {}).get("topic_id") or "topic-1",
                "segment_ids": [segment_id],
                "start_seconds": 5.0,
                "end_seconds": 25.0,
                "kind": "procedure",
                "claims": [
                    {
                        "id": f"claim-{segment_id}-1",
                        "text": "演示步骤",
                        "evidence_ids": ["cap-001", "frame-001", "frame-002"],
                        "status": "draft",
                        "qualifiers": [],
                        "modality": "visual",
                        "provenance": "source",
                    }
                ],
                "relations": [],
                "visual_candidates": [],
                "uncertainty": [],
                "evidence_requests": [
                    {
                        "start_seconds": 10.0,
                        "end_seconds": 20.0,
                        "reason": "missing_step",
                        "desired_modality": "clip",
                    }
                ],
            }
        ]
    }


def test_frames_mode_never_uploads_video(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 500_000)
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(backend=backend)
    caps = frames_caps(supports_video=True, max_video_seconds=120.0, max_input_tokens=32000)
    analyze_course(
        source_id="src-demo",
        duration_seconds=60.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(caps),
        capabilities=caps,
        analysis_mode="frames",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
    )
    assert backend.uploads == []


def test_hybrid_uploads_subclip_not_full_media(tmp_path: Path):
    transcript = make_transcript([("cap-001", 0.0, 30.0, "步骤演示")], duration=30.0)
    visual = make_visual(
        [("frame-001", 5.0, "scene-001"), ("frame-002", 15.0, "scene-001")],
        duration=30.0,
    )
    media = tmp_path / "source.mp4"
    media.write_bytes(b"FULL" * 125_000)
    extractor = _sized_clip_extractor(media)
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(
        capabilities=frames_caps(supports_video=True, max_video_seconds=30.0, max_input_tokens=32000),
        backend=backend,
    )
    provider = FakeProvider(
        frames_caps(supports_video=False, max_input_tokens=32000),
        responder=_procedure_segment_responder,
    )
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=30.0,
        transcript=transcript,
        visual=visual,
        provider=provider,
        capabilities=provider.capabilities,
        analysis_mode="hybrid",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=extractor,
        scheduler_config=SchedulerConfig(core_seconds=60.0, max_core_seconds=60.0),
    )
    assert backend.uploads
    uploaded = backend.uploads[0]
    assert uploaded.byte_length < media.stat().st_size / 10
    assert result.media_audit is not None
    assert result.media_audit.media_uploaded is True
    assert len(result.media_audit.uploads) == 1
    assert result.media_audit.uploads[0].usage_video_seconds == uploaded.duration_seconds


def test_native_empty_units_keeps_frames_no_synthetic_claims(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 10_000)
    backend = FakeNativeVideoBackend(structured={"units": []})
    adapter = fake_native_adapter(
        backend=backend,
        capabilities=frames_caps(supports_video=True, max_video_seconds=300.0, max_input_tokens=32000),
    )
    before = analyze_course(
        source_id="src-demo",
        duration_seconds=30.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(frames_caps(max_input_tokens=32000)),
        capabilities=frames_caps(max_input_tokens=32000),
        analysis_mode="frames",
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
        scheduler_config=SchedulerConfig(core_seconds=30.0, max_core_seconds=30.0),
    )
    after = analyze_course(
        source_id="src-demo",
        duration_seconds=30.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(frames_caps(max_input_tokens=32000)),
        capabilities=frames_caps(max_input_tokens=32000),
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
        scheduler_config=SchedulerConfig(core_seconds=30.0, max_core_seconds=30.0),
    )
    assert len(after.knowledge.iter_claims()) == len(before.knowledge.iter_claims())
    assert not any("native-supported" in claim.text for claim in after.knowledge.iter_claims())


def test_native_video_default_budget_uploads_capped_clip_on_long_lecture(tmp_path: Path):
    """Grok repro: 300s lecture, 30s default clip budget, 120s max_video — must still upload."""

    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 500_000)
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(
        backend=backend,
        capabilities=frames_caps(supports_video=True, max_video_seconds=120.0, max_input_tokens=32000),
    )
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=300.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(frames_caps(max_input_tokens=32000)),
        capabilities=frames_caps(max_input_tokens=32000),
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
    )
    assert len(backend.uploads) >= 1
    first = backend.uploads[0]
    assert 29.0 <= first.duration_seconds <= 30.0
    assert first.byte_length < media.stat().st_size / 10
    assert result.coverage_complete is False
    assert result.media_audit is not None
    assert result.media_audit.media_uploaded is True


def test_native_video_partial_window_marks_tail_and_coverage_incomplete(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 20_000)
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(
        backend=backend,
        capabilities=frames_caps(supports_video=True, max_video_seconds=30.0, max_input_tokens=32000),
    )
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=120.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(frames_caps(max_input_tokens=32000)),
        capabilities=frames_caps(supports_video=True, max_video_seconds=30.0, max_input_tokens=32000),
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
        scheduler_config=SchedulerConfig(core_seconds=120.0, max_core_seconds=120.0),
    )
    assert result.coverage_complete is False
    assert any(
        "partial coverage" in reason or "native-video" in reason
        for reason in result.gap_reasons
    ) or any(window.status == "degraded" for window in result.segments.windows)


def test_native_failure_falls_back_to_unresolved(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 10_000)
    backend = FakeNativeVideoBackend(fail_analyze=True)
    adapter = fake_native_adapter(
        backend=backend,
        capabilities=frames_caps(supports_video=True, max_video_seconds=300.0, max_input_tokens=32000),
    )
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=30.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(frames_caps(max_input_tokens=32000)),
        capabilities=frames_caps(max_input_tokens=32000),
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
        scheduler_config=SchedulerConfig(core_seconds=30.0, max_core_seconds=30.0),
    )
    assert backend.uploads
    assert result.media_audit is not None
    assert result.media_audit.media_uploaded is True
    assert any("native video failed" in reason for reason in result.gap_reasons)
    assert result.coverage_complete is False


def test_review_bundle_carries_media_privacy(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 10_000)
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(backend=backend)
    caps = frames_caps(max_input_tokens=32000)
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=30.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(caps),
        capabilities=caps,
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
        output_dir=tmp_path,
        clip_extractor=_sized_clip_extractor(media),
        scheduler_config=SchedulerConfig(core_seconds=30.0, max_core_seconds=30.0),
    )
    paths = write_analysis_artifacts(result, tmp_path / "analysis")
    audit = json.loads(paths["media_audit"].read_text(encoding="utf-8"))
    assert audit["media_uploaded"] is True
    assert len(audit["uploads"]) == 1
    assert sum(row["usage_video_seconds"] for row in audit["uploads"]) <= 30.0


def test_minimal_clip_range_helper_disjoint():
    procedure = unit(
        "unit-proc",
        start=40.0,
        end=70.0,
        kind="procedure",
        claims=[claim("c2", "步骤", ["cap-002"])],
    ).model_copy(
        update={
            "evidence_requests": [
                KnowledgeEvidenceRequest(
                    start_seconds=45.0,
                    end_seconds=50.0,
                    reason="missing_step",
                    desired_modality="clip",
                ),
                KnowledgeEvidenceRequest(
                    start_seconds=60.0,
                    end_seconds=65.0,
                    reason="missing_step",
                    desired_modality="clip",
                ),
            ]
        }
    )
    window = AnalysisWindow(
        id="seg-0001",
        core_start_seconds=40.0,
        core_end_seconds=80.0,
        context_start_seconds=35.0,
        context_end_seconds=85.0,
        evidence_ids=["cap-002"],
        status="degraded",
    )
    start, end = minimal_clip_range(procedure, window)
    assert start <= 45.0 and end >= 65.0
    assert unit_needs_native_upgrade(procedure, visual=make_visual([], duration=80.0))


def test_apply_review_edits_preserves_media_privacy():
    from yt2class.domain.media_audit import MediaPrivacyAudit
    from tests.integration.test_review_roundtrip import _verified_bundle

    outcome, transcript, visual, topics = _verified_bundle()
    audit = MediaPrivacyAudit(
        schema_version="1.0",
        source_id=outcome.knowledge.source_id,
        analysis_mode="hybrid",
        media_uploaded=True,
        uploads=[],
        summary="test-audit",
    )
    bundle = build_review(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
        media_privacy=audit,
    )
    assert bundle.media_privacy is not None
    applied = apply_review_edits(
        bundle,
        ReviewEdits(
            revision=1,
            baseline_hashes=bundle.baseline_hashes,
            ops=[ReviewOp(op="lock", page_id=bundle.pages[0].page.id)],
        ),
        knowledge=outcome.knowledge,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(frames_caps()),
        course_map=topics,
    )
    assert applied.bundle.media_privacy == audit

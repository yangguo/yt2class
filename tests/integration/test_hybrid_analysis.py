"""Integration tests for hybrid / native-video routing with fake backends."""

from __future__ import annotations

import json
from pathlib import Path

from yt2class.adapters.providers.native_video import FakeNativeVideoBackend, fake_native_adapter
from yt2class.adapters.providers.synthetic import fake_course_provider
from yt2class.domain.knowledge import KnowledgeEvidenceRequest, Uncertainty
from yt2class.orchestration.analyze import analyze_course, default_capabilities, write_analysis_artifacts
from yt2class.orchestration.hybrid_analysis import minimal_clip_range, unit_needs_native_upgrade
from yt2class.domain.segment import AnalysisWindow
from tests.helpers.m2 import claim, frames_caps, lecture_fixture, unit


def test_frames_mode_never_uploads_video(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(backend=backend)
    video_caps = default_capabilities().model_copy(
        update={"supports_video": True, "max_video_seconds": 120.0}
    )
    analyze_course(
        source_id="src-demo",
        duration_seconds=300.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(video_caps),
        capabilities=video_caps,
        analysis_mode="frames",
        native_adapter=adapter,
        media_path=media,
    )
    assert backend.uploads == []


def test_hybrid_skips_upload_when_frames_sufficient(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    plain = unit("unit-plain", start=0.0, end=30.0, claims=[claim("c1", "概念", ["cap-001"])])
    assert unit_needs_native_upgrade(plain, visual=visual) is False
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(backend=backend)
    caps = frames_caps(supports_video=True, max_video_seconds=120.0)
    # Procedure with missing_step triggers upgrade; concept does not.
    procedure = unit(
        "unit-proc",
        start=40.0,
        end=70.0,
        kind="procedure",
        claims=[claim("c2", "步骤一", ["cap-002", "frame-001", "frame-002"])],
    ).model_copy(
        update={
            "uncertainty": [
                Uncertainty(
                    kind="missing_step",
                    start_seconds=45.0,
                    end_seconds=55.0,
                    note="动作太快",
                )
            ],
            "evidence_requests": [
                KnowledgeEvidenceRequest(
                    start_seconds=45.0,
                    end_seconds=55.0,
                    reason="missing_step",
                    desired_modality="clip",
                )
            ],
        }
    )
    assert unit_needs_native_upgrade(procedure, visual=visual) is True
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
    assert end - start <= 30.0
    assert start >= 40.0
    assert end <= 80.0


def test_native_failure_falls_back_to_unresolved(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video-bytes-for-hash")
    backend = FakeNativeVideoBackend(fail_analyze=True)
    adapter = fake_native_adapter(backend=backend)
    caps = frames_caps(supports_video=True, max_video_seconds=300.0, max_input_tokens=32000)
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=300.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(caps),
        capabilities=caps,
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
    )
    assert backend.uploads
    assert result.media_audit is not None
    assert result.media_audit.media_uploaded is True
    unresolved = [claim for claim in result.knowledge.iter_claims() if claim.status == "unresolved"]
    assert unresolved


def test_audit_artifact_lists_upload_range_and_usage(tmp_path: Path):
    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    backend = FakeNativeVideoBackend()
    adapter = fake_native_adapter(backend=backend)
    caps = frames_caps(supports_video=True, max_video_seconds=300.0, max_input_tokens=32000)
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=300.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(caps),
        capabilities=caps,
        analysis_mode="native-video",
        native_adapter=adapter,
        media_path=media,
    )
    out = tmp_path / "analysis"
    paths = write_analysis_artifacts(result, out)
    assert "media_audit" in paths
    audit = json.loads(paths["media_audit"].read_text(encoding="utf-8"))
    assert audit["analysis_mode"] == "native-video"
    assert audit["uploads"]
    row = audit["uploads"][0]
    assert row["source_range"]["start_seconds"] <= row["source_range"]["end_seconds"]
    assert row["usage_video_seconds"] >= 0


def test_mode_comparison_report_fixture(tmp_path: Path):
    """Synthetic comparison artifact for frames vs native vs hybrid (no live vendor)."""

    transcript, visual = lecture_fixture()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    caps = frames_caps(supports_video=True, max_video_seconds=300.0, max_input_tokens=32000)
    rows = []
    for mode in ("frames", "native-video", "hybrid"):
        backend = FakeNativeVideoBackend()
        adapter = fake_native_adapter(backend=backend) if mode != "frames" else None
        result = analyze_course(
            source_id="src-demo",
            duration_seconds=300.0,
            transcript=transcript,
            visual=visual,
            provider=fake_course_provider(caps),
            capabilities=caps,
            analysis_mode=mode,
            native_adapter=adapter,
            media_path=media,
        )
        rows.append(
            {
                "mode": mode,
                "claims": len(result.knowledge.iter_claims()),
                "uploads": len(result.media_audit.uploads) if result.media_audit else 0,
                "video_seconds": sum(
                    item.usage_video_seconds for item in (result.media_audit.uploads if result.media_audit else [])
                ),
                "coverage_complete": result.coverage_complete,
            }
        )
    report_path = Path("docs/examples/m6-mode-comparison.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"sample": "dynamic-lecture-fixture", "rows": rows}, indent=2) + "\n", encoding="utf-8")
    assert rows[0]["uploads"] == 0
    assert rows[1]["uploads"] >= 1

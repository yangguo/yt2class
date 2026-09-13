from __future__ import annotations

import json

from typer.testing import CliRunner

from yt2class.adapters.providers.synthetic import fake_course_provider
from yt2class.cli import app
from yt2class.domain.evidence import EvidenceBundle
from yt2class.orchestration.analyze import analyze_course, default_capabilities
from yt2class.orchestration.scheduler import cores_cover_duration
from tests.helpers.m2 import lecture_fixture


def test_fake_provider_vertical_path_meets_m2_gate():
    transcript, visual = lecture_fixture()
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=300.0,
        transcript=transcript,
        visual=visual,
        provider=fake_course_provider(default_capabilities()),
        page_budget=2,
    )
    assert cores_cover_duration(result.segments.windows, 300.0)
    assert result.m2_gate_ok()
    kinds = {unit.kind for unit in result.knowledge.units}
    assert "concept" in kinds
    assert "example" in kinds
    assert "procedure" in kinds
    assert result.knowledge.iter_claims()
    assert all(claim.evidence_ids for claim in result.knowledge.iter_claims())
    assert all(window.status != "scheduled" for window in result.segments.windows)
    # Page budget must not truncate analysis coverage.
    covered = sum(
        window.core_end_seconds - window.core_start_seconds for window in result.segments.windows
    )
    assert abs(covered - 300.0) < 1e-6


def test_analyze_cli_fake_provider(tmp_path):
    from yt2class.domain.source import SourceManifest
    from tests.helpers.m2 import DIGEST_A

    transcript, visual = lecture_fixture()
    source = SourceManifest(
        schema_version="1.0",
        source_id="src-demo",
        kind="local",
        title="合成课程",
        media_path="media/source.mp4",
        sha256=DIGEST_A,
        duration_seconds=300.0,
        streams=[{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        timebase="1/90000",
        local_mode="copy",
    )
    bundle = EvidenceBundle(
        schema_version="1.0",
        source_id="src-demo",
        source_hash=DIGEST_A,
        duration_seconds=300.0,
        source=source,
        transcript=transcript,
        visual=visual,
        status="complete",
        artifacts=[
            {
                "id": "artifact-transcript",
                "kind": "transcript-document",
                "path": "evidence/transcript-document.json",
                "sha256": "d" * 64,
            },
            {
                "id": "artifact-visual",
                "kind": "visual-catalogue",
                "path": "evidence/visual-catalogue.json",
                "sha256": "e" * 64,
            },
        ],
        transcript_path="evidence/transcript-document.json",
        visual_path="evidence/visual-catalogue.json",
    )
    evidence_path = tmp_path / "evidence-bundle.json"
    evidence_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    output = tmp_path / "analysis"
    result = CliRunner().invoke(
        app,
        ["analyze", "--evidence", str(evidence_path), "--output", str(output), "--provider", "fake"],
        prog_name="yt2class",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    knowledge = json.loads((output / "knowledge-document.json").read_text(encoding="utf-8"))
    assert knowledge["units"]
    rejected = CliRunner().invoke(
        app,
        ["analyze", "--evidence", str(evidence_path), "--provider", "openai"],
        prog_name="yt2class",
    )
    assert rejected.exit_code == 2


def test_failed_window_is_not_coverage_complete():
    from yt2class.adapters.providers.base import FakeProvider
    from yt2class.adapters.providers.synthetic import course_responder
    from tests.helpers.m2 import frames_caps

    def fail_segments(provider, request):
        if request.role == "outline":
            return course_responder(provider, request)
        return None

    transcript, visual = lecture_fixture()
    result = analyze_course(
        source_id="src-demo",
        duration_seconds=300.0,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps(), responder=fail_segments),
        capabilities=frames_caps(),
    )
    assert result.coverage_complete is False
    assert result.m2_gate_ok() is False
    assert any(window.status == "failed" for window in result.segments.windows)


def test_empty_knowledge_fails_m2_gate():
    from yt2class.domain.course_map import CourseMap
    from yt2class.domain.knowledge import KnowledgeDocument
    from yt2class.domain.segment import AnalysisWindow, SegmentManifest
    from yt2class.orchestration.analyze import AnalysisResult
    from tests.helpers.m2 import lecture_fixture

    transcript, visual = lecture_fixture()
    manifest = SegmentManifest(
        schema_version="1.0",
        source_id="src-demo",
        duration_seconds=300.0,
        windows=[
            AnalysisWindow(
                id="seg-0001",
                core_start_seconds=0.0,
                core_end_seconds=300.0,
                context_start_seconds=0.0,
                context_end_seconds=300.0,
                evidence_ids=["cap-001"],
                status="complete",
            )
        ],
    )
    result = AnalysisResult(
        course_map=CourseMap(schema_version="1.0", source_id="src-demo", topics=[]),
        segments=manifest,
        knowledge=KnowledgeDocument(schema_version="1.0", source_id="src-demo", units=[]),
        visual=visual,
        outcomes=[],
        coverage_complete=True,
        gap_reasons=[],
    )
    assert result.m2_gate_ok() is False

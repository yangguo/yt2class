from __future__ import annotations

from pathlib import Path

from yt2class.config import BuildSource, CourseConfig
from yt2class.orchestration import pipeline as orch_pipeline
from yt2class.orchestration.pipeline import execute_run
from tests.helpers.m5 import build_evidence_bundle


def test_cli_shaped_resume_does_not_reextract(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    video = workspace.root / "media" / "source.mp4"
    vtt = tmp_path / "lesson.vtt"
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nA\n", encoding="utf-8")
    calls = {"extract": 0}

    def fake_extract(source, **kwargs):
        calls["extract"] += 1
        return bundle

    monkeypatch.setattr(orch_pipeline, "run_ingest", lambda _ctx, _build: bundle.source)
    monkeypatch.setattr(orch_pipeline, "extract_evidence_locked", fake_extract)
    cfg = CourseConfig()
    build = BuildSource(source_id=bundle.source_id, video=video, subtitles=vtt)
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        stop_after="extract_evidence",
    )
    assert calls["extract"] == 1

    cli_resume = BuildSource(source_id=bundle.source_id, run_id=workspace.root.name)
    execute_run(
        tmp_path,
        build=cli_resume,
        config=cfg,
        run_id=workspace.root.name,
        resume=True,
        stop_after="extract_evidence",
    )
    assert calls["extract"] == 1


def test_subtitle_content_change_reextracts(tmp_path: Path, monkeypatch):
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
    execute_run(
        tmp_path,
        build=BuildSource(source_id=bundle.source_id, video=video, subtitles=vtt_a),
        config=cfg,
        run_id=workspace.root.name,
        stop_after="extract_evidence",
    )
    assert calls["extract"] == 1
    execute_run(
        tmp_path,
        build=BuildSource(source_id=bundle.source_id, video=video, subtitles=vtt_b),
        config=cfg,
        run_id=workspace.root.name,
        resume=True,
        stop_after="extract_evidence",
    )
    assert calls["extract"] == 2

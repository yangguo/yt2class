from __future__ import annotations

from unittest.mock import MagicMock

from yt2class.domain.source import SourceManifest
from yt2class.orchestration.pipeline import run_extract_evidence


def test_run_extract_evidence_resolves_auto_ocr_into_extract_call(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_resolve(requested):
        assert requested == "auto"
        return "fake", None

    def fake_extract_locked(*args, **kwargs):
        captured.update(kwargs)
        bundle = MagicMock()
        bundle.model_dump.return_value = {"schema_version": "1.0"}
        return bundle

    monkeypatch.setattr("yt2class.orchestration.pipeline.resolve_ocr_engine", fake_resolve)
    monkeypatch.setattr("yt2class.orchestration.pipeline.extract_evidence_locked", fake_extract_locked)
    monkeypatch.setattr(
        "yt2class.orchestration.pipeline.validated_cache_hit",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "yt2class.orchestration.pipeline.load_validated_json",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("yt2class.orchestration.pipeline.atomic_write_json", lambda *args, **kwargs: None)
    monkeypatch.setattr("yt2class.orchestration.pipeline.stage_artifact_path", lambda *a, **k: tmp_path / "bundle.json")

    ctx = MagicMock()
    ctx.config.analysis.ocr_engine = "auto"
    ctx.config.analysis.ocr_languages = "jpn+eng"
    ctx.config_digest.return_value = "cfg"
    ctx.tool_versions.return_value = {}
    ctx.cancel_event = None
    ctx.build = None
    ctx.workspace.write_lock.return_value.__enter__ = lambda self: None
    ctx.workspace.write_lock.return_value.__exit__ = lambda *args: None
    ctx.workspace.root = tmp_path
    ctx.workspace.safe_path = lambda rel, **kw: tmp_path / rel
    ctx.manifest = MagicMock()
    ctx.manifest.model_copy.return_value = ctx.manifest
    ctx.evidence = None

    source = SourceManifest(
        schema_version="1.0",
        source_id="src-ocr",
        kind="local",
        title="lesson",
        media_path="media/x.mp4",
        sha256="a" * 64,
        duration_seconds=10.0,
        streams=[{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        timebase="1/90000",
        local_mode="reference",
        reference_path=str(tmp_path / "x.mp4"),
    )
    (tmp_path / "x.mp4").write_bytes(b"video")

    monkeypatch.setattr("yt2class.orchestration.pipeline.refresh_stage_keys", lambda m, *a, **k: m)
    monkeypatch.setattr("yt2class.orchestration.pipeline.set_stage_status", lambda m, *a, **k: m)
    monkeypatch.setattr("yt2class.orchestration.pipeline._persist", lambda ctx: None)
    monkeypatch.setattr("yt2class.orchestration.pipeline._complete_stage", lambda *a, **k: None)
    monkeypatch.setattr("yt2class.orchestration.pipeline._extract_input_hashes", lambda *a, **k: ["hash"])

    run_extract_evidence(ctx, source)
    assert captured["ocr_engine"] == "fake"
    assert captured["ocr_languages"] == "jpn+eng"
    assert captured.get("ocr_unavailable_reason") is None

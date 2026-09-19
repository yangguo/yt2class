from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2class.adapters.asr import ASRRequest
from yt2class.domain.source import SourceManifest, content_sha256
from yt2class.stages.extract_evidence import extract_evidence


def _write_media(path: Path, payload: bytes = b"reference-media") -> Path:
    path.write_bytes(payload)
    return path


def _manifest(path: Path, *, source_id: str = "src-test") -> SourceManifest:
    return SourceManifest(
        schema_version="1.0",
        source_id=source_id,
        kind="local",
        title="test",
        media_path="media/source.mp4",
        sha256=content_sha256(path),
        duration_seconds=2.0,
        streams=[{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        timebase="1/90000",
        local_mode="reference",
        reference_path=str(path.resolve()),
    )


def _audio_runner(command, **kwargs):
    Path(command[-1]).write_bytes(b"extracted-from-verified-media")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _visual_runner(command, **kwargs):
    from PIL import Image

    Image.new("RGB", (80, 40), "white").save(command[-1])
    return SimpleNamespace(
        returncode=0,
        stdout="",
        stderr="[Parsed_showinfo_0] n:1 pts_time:0.200000 duration:0.1",
    )


def _faster_whisper_runner(request, **kwargs):
    from yt2class.adapters.asr import parse_asr_json

    payload = {
        "engine": "faster-whisper",
        "model": "medium",
        "language": "ja",
        "status": "complete",
        "segments": [{"start": 0.0, "end": 1.5, "text": "ASRのみの講義です。"}],
    }
    return parse_asr_json(payload, request).model_copy(
        update={"audio_sha256": content_sha256(request.audio_path)}
    )


def test_subtitles_take_precedence_over_configured_asr(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    subtitle = tmp_path / "lesson.vtt"
    subtitle.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n字幕テキスト。\n",
        encoding="utf-8",
    )
    request = ASRRequest(
        request_id="asr-1",
        source_id=manifest.source_id,
        audio_path=tmp_path / "unused.wav",
        engine="faster-whisper",
        model="medium",
        align=False,
    )

    with patch(
        "yt2class.stages.extract_evidence.run_asr",
        side_effect=AssertionError("ASR must not run"),
    ):
        bundle = extract_evidence(
            manifest,
            media,
            tmp_path / "run-sub-first",
            sidecar=subtitle,
            detector=lambda path, **kwargs: [(0.0, 2.0)],
            runner=_visual_runner,
            ocr_engine="none",
            asr_request=request,
        )

    assert bundle.transcript.segments[0].text_original == "字幕テキスト。"
    assert bundle.transcript.segments[0].origin == "sidecar"


def test_no_subtitles_runs_faster_whisper_asr(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    request = ASRRequest(
        request_id="asr-1",
        source_id=manifest.source_id,
        audio_path=tmp_path / "tmp/asr-audio.wav",
        engine="faster-whisper",
        model="medium",
        align=False,
    )

    with patch("yt2class.stages.extract_evidence.run_asr", side_effect=_faster_whisper_runner):
        bundle = extract_evidence(
            manifest,
            media,
            tmp_path / "run-asr",
            detector=lambda path, **kwargs: [(0.0, 2.0)],
            runner=_visual_runner,
            audio_runner=_audio_runner,
            ocr_engine="none",
            asr_request=request,
        )

    assert bundle.transcript.segments
    assert bundle.transcript.segments[0].origin == "asr"
    assert "ASRのみ" in bundle.transcript.segments[0].text_original

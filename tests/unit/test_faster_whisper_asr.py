from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yt2class.adapters.asr import (
    ASRError,
    ASRRequest,
    resolve_asr_engine,
    run_asr,
)
from yt2class.orchestration.asr_policy import build_asr_request
from yt2class.config import CourseConfig


def test_resolve_asr_engine_auto_selects_faster_whisper():
    engine, reason = resolve_asr_engine("auto")
    assert engine == "faster-whisper"
    assert reason is None


def test_build_asr_request_uses_medium_and_autodetect_language(tmp_path: Path):
    from yt2class.domain.source import SourceManifest

    source = SourceManifest(
        schema_version="1.0",
        source_id="src-asr",
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
    analysis = CourseConfig().analysis
    request, reason = build_asr_request(
        source, workspace_root=tmp_path, analysis=analysis
    )
    assert reason is None
    assert request is not None
    assert request.engine == "faster-whisper"
    assert request.model == "medium"
    assert request.language is None


def test_run_asr_faster_whisper_uses_in_process_worker(tmp_path: Path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"wav")
    request = ASRRequest(
        request_id="asr-1",
        source_id="src-1",
        audio_path=audio,
        engine="faster-whisper",
        model="medium",
        language=None,
        align=False,
    )
    payload = {
        "engine": "faster-whisper",
        "model": "medium",
        "language": "ja",
        "status": "complete",
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "文法の説明です。"},
        ],
    }

    with patch(
        "yt2class.workers.faster_whisper_worker.transcribe_to_payload",
        return_value=payload,
    ) as mocked:
        result = run_asr(request, runner=lambda *args, **kwargs: None)

    mocked.assert_called_once()
    assert mocked.call_args.kwargs["language"] is None
    assert result.engine == "faster-whisper"
    assert result.segments[0].text_original == "文法の説明です。"


def test_run_asr_faster_whisper_missing_package_raises_clear_error(tmp_path: Path):
    from yt2class.workers.faster_whisper_worker import FasterWhisperUnavailable

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"wav")
    request = ASRRequest(
        request_id="asr-1",
        source_id="src-1",
        audio_path=audio,
        engine="faster-whisper",
        model="medium",
        align=False,
    )

    with patch(
        "yt2class.workers.faster_whisper_worker.transcribe_to_payload",
        side_effect=FasterWhisperUnavailable("faster-whisper is not installed"),
    ):
        with pytest.raises(ASRError, match="not installed"):
            run_asr(request)

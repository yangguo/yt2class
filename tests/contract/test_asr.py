from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest

from yt2class.domain.source import content_sha256
from yt2class.adapters.asr import (
    ASRCancelled,
    ASRContractError,
    ASRError,
    ASRRequest,
    ASRTimeout,
    build_audio_extract_command,
    build_asr_command,
    extract_audio,
    parse_asr_json,
    run_asr,
)


def make_request(tmp_path: Path, **overrides) -> ASRRequest:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"synthetic audio")
    data = dict(
        request_id="asr-req-1",
        source_id="src-asr",
        audio_path=audio,
        language="ja",
        engine="whisperx",
        model="tiny",
        device="cpu",
        align=True,
        diarize=False,
        offset_seconds=10.0,
    )
    data.update(overrides)
    return ASRRequest.model_validate(data)


def test_asr_command_records_offset_and_worker_boundary(tmp_path: Path):
    request = make_request(tmp_path)
    command = build_asr_command(request)
    assert command[:3] == [command[0], "-m", "yt2class.workers.whisperx_worker"]
    assert "--offset-seconds" in command
    assert command[command.index("--offset-seconds") + 1] == "10.000000"
    assert "--input" in command
    assert str(request.audio_path) in command


def test_audio_extract_command_requests_mono_16khz_pcm(tmp_path: Path):
    command = build_audio_extract_command(tmp_path / "source.mkv", tmp_path / "audio.wav")
    assert [command[index + 1] for index, item in enumerate(command) if item == "-map"] == ["0:a:0?"]
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"


def test_audio_extract_publishes_runner_output_atomically(tmp_path: Path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    output = tmp_path / "audio.wav"

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"wav")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert extract_audio(source, output, runner=runner) == output
    assert output.read_bytes() == b"wav"


def test_asr_runner_offsets_segments_and_words_and_records_configuration(tmp_path: Path):
    request = make_request(tmp_path)
    payload = {
        "schema_version": "1.0",
        "language": "ja",
        "alignment": "word",
        "diarization": False,
        "segments": [
            {
                "start": 1.0,
                "end": 2.5,
                "text": "こんにちは",
                "words": [
                    {"start": 1.0, "end": 1.5, "word": "こん"},
                    {"start": 1.5, "end": 2.5, "word": "にちは"},
                ],
            }
        ],
    }

    def runner(command, **kwargs):
        assert kwargs["shell"] is False
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    result = run_asr(request, runner=runner)
    assert result.status == "complete"
    assert result.audio_sha256 == content_sha256(request.audio_path)
    assert result.engine == "whisperx"
    assert result.model == "tiny"
    assert result.device == "cpu"
    assert result.alignment == "word"
    assert result.segments[0].start_seconds == 11.0
    assert result.segments[0].end_seconds == 12.5
    assert result.segments[0].words[0].start_seconds == 11.0


def test_asr_silence_is_a_valid_empty_result(tmp_path: Path):
    request = make_request(tmp_path)

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"segments": [], "language": "ja"}),
            stderr="",
        )

    result = run_asr(request, runner=runner)
    assert result.status == "complete"
    assert result.segments == []
    assert result.alignment == "none"


def test_asr_marks_unaligned_words_without_inventing_timestamps(tmp_path: Path):
    request = make_request(tmp_path, align=False)
    result = parse_asr_json(
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "silent alignment",
                    "words": [{"word": "silent"}],
                }
            ]
        },
        request,
    )
    segment = result.segments[0]
    assert segment.words is None
    assert segment.alignment_status == "unaligned"
    assert "word-alignment-missing" in segment.quality_flags


def test_asr_bad_ranges_and_process_failures_are_explicit(tmp_path: Path):
    request = make_request(tmp_path)
    with pytest.raises(ASRContractError, match="segment range"):
        parse_asr_json({"segments": [{"start": 2.0, "end": 1.0, "text": "bad"}]}, request)

    def failed_runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="model failed")

    with pytest.raises(ASRError, match="model failed"):
        run_asr(request, runner=failed_runner)

    cancelled = Event()
    cancelled.set()
    with pytest.raises(ASRCancelled):
        run_asr(request, runner=failed_runner, cancel_event=cancelled)


def test_asr_timeout_is_not_a_failed_transcript(tmp_path: Path):
    request = make_request(tmp_path)

    def timeout_runner(command, **kwargs):
        raise TimeoutError("slow")

    with pytest.raises(ASRTimeout):
        run_asr(request, runner=timeout_runner)

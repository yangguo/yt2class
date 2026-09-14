from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from threading import Event
from types import SimpleNamespace

import pytest

from yt2class.adapters.ffmpeg import (
    FfmpegCancelled,
    FfmpegError,
    FfmpegTimeout,
    MediaProbe,
    build_ffprobe_command,
    build_transform_command,
    parse_ffprobe_json,
    probe_media,
    run_transform,
)
from yt2class.adapters.ytdlp import (
    YtDlpCancelled,
    YtDlpError,
    YtDlpTimeout,
    build_download_command,
    download_video,
)
from yt2class.domain.source import SourceInput


def test_ytdlp_command_is_argv_and_disables_playlists(tmp_path: Path):
    source = SourceInput.from_value("https://youtu.be/fixture-id")
    command = build_download_command(source.normalized_value, tmp_path)

    assert command[0] == "yt-dlp"
    assert "--no-playlist" in command
    assert "--write-info-json" in command
    assert "--no-part" not in command
    assert "--print" in command
    assert "after_move:filepath" in command
    assert "--" in command
    assert command[-1] == source.normalized_value
    assert all(isinstance(item, str) for item in command)


def test_ytdlp_captures_actual_unicode_output_filename(tmp_path: Path):
    output = tmp_path / "课程 sample [fixture-id].webm"
    info = output.with_suffix(".info.json")

    def runner(command, **kwargs):
        assert kwargs["check"] is False
        assert kwargs["shell"] is False
        output.write_bytes(b"media")
        info.write_text(json.dumps({"id": "fixture-id", "title": "课程标题"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=f"{output}\n", stderr="")

    result = download_video(
        SourceInput.from_value("https://youtu.be/fixture-id"),
        tmp_path,
        runner=runner,
    )

    assert result.media_path == output
    assert result.media_path.name == "课程 sample [fixture-id].webm"
    assert result.info_path == info


def test_ytdlp_rejects_info_json_for_a_different_video(tmp_path: Path):
    output = tmp_path / "课程 sample [fixture-id].webm"
    info = output.with_suffix(".info.json")

    def runner(command, **kwargs):
        output.write_bytes(b"media")
        info.write_text(json.dumps({"id": "different-id", "title": "课程标题"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=f"{output}\n", stderr="")

    with pytest.raises(YtDlpError, match="id"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=runner,
        )


def test_ytdlp_does_not_reuse_stale_media_or_accept_output_outside_directory(tmp_path: Path):
    stale = tmp_path / "old.webm"
    stale.write_bytes(b"old")

    def no_output_runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(YtDlpError, match="media"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=no_output_runner,
        )

    outside = tmp_path.parent / "escaped fixture.webm"
    outside.write_bytes(b"outside")

    def escaping_runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{outside}\n", stderr="")

    with pytest.raises(YtDlpError, match="media"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=escaping_runner,
        )

    linked = tmp_path / "linked.webm"
    linked.symlink_to(outside)

    with pytest.raises(YtDlpError, match="media"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=no_output_runner,
        )


def test_ytdlp_does_not_reuse_stale_info_json(tmp_path: Path):
    output = tmp_path / "课程 [fixture-id].webm"
    stale_info = output.with_suffix(".info.json")
    stale_info.write_text(json.dumps({"id": "fixture-id", "title": "旧标题"}), encoding="utf-8")

    def media_only_runner(command, **kwargs):
        output.write_bytes(b"new media")
        return SimpleNamespace(returncode=0, stdout=f"{output}\n", stderr="")

    with pytest.raises(YtDlpError, match="info JSON"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=media_only_runner,
        )


def test_ytdlp_cancel_timeout_missing_tool_and_partial_download_fail(tmp_path: Path):
    cancelled = Event()
    cancelled.set()
    with pytest.raises(YtDlpCancelled):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            cancel_event=cancelled,
        )

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(YtDlpTimeout):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=timeout_runner,
        )

    def missing_runner(command, **kwargs):
        raise FileNotFoundError(command[0])

    with pytest.raises(YtDlpError, match="unavailable"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=missing_runner,
        )

    partial = tmp_path / "partial.webm.part"
    partial.write_bytes(b"partial")

    def failed_runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout=str(partial), stderr="network reset")

    with pytest.raises(YtDlpError, match="failed"):
        download_video(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            tmp_path,
            runner=failed_runner,
        )


def test_ffprobe_parser_extracts_duration_streams_rotation_fps_and_timebase(tmp_path: Path):
    payload = {
        "format": {"duration": "2.5"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "avg_frame_rate": "30000/1001",
                "time_base": "1/90000",
                "tags": {"rotate": "90"},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "time_base": "1/48000",
                "tags": {"language": "en"},
            },
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "ass",
                "time_base": "1/1000",
                "tags": {"language": "zh"},
            },
        ],
    }

    result = parse_ffprobe_json(payload, path=tmp_path / "source.webm")

    assert isinstance(result, MediaProbe)
    assert result.duration_seconds == 2.5
    assert result.rotation_degrees == 90
    assert result.fps == pytest.approx(30000 / 1001)
    assert result.timebase == "1/90000"
    assert [stream.codec_type for stream in result.streams] == ["video", "audio", "subtitle"]
    assert result.streams[1].language == "en"
    assert result.streams[2].language == "zh"


def test_ffprobe_rejects_zero_duration_and_missing_video_stream(tmp_path: Path):
    zero = {"format": {"duration": "0"}, "streams": []}
    with pytest.raises(FfmpegError, match="duration"):
        parse_ffprobe_json(zero, path=tmp_path / "empty.mp4")

    audio_only = {
        "format": {"duration": "1.0"},
        "streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac"}],
    }
    with pytest.raises(FfmpegError, match="video stream"):
        parse_ffprobe_json(audio_only, path=tmp_path / "audio.m4a")

    missing_timebase = {
        "format": {"duration": "1.0"},
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
    }
    with pytest.raises(FfmpegError, match="timebase"):
        parse_ffprobe_json(missing_timebase, path=tmp_path / "unknown-timebase.mp4")


def test_ffprobe_command_and_runner_parse_real_json(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"placeholder")
    payload = {
        "format": {"duration": "1.25"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "r_frame_rate": "25/1",
                "time_base": "1/12800",
            }
        ],
    }

    def runner(command, **kwargs):
        assert command == build_ffprobe_command(source)
        assert kwargs["shell"] is False
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    result = probe_media(source, runner=runner)
    assert result.duration_seconds == 1.25
    assert result.timebase == "1/12800"


def test_ffmpeg_transform_records_parent_hash_command_digest_offset_and_speed(tmp_path: Path):
    parent = tmp_path / "parent.mkv"
    output = tmp_path / "derived.mp4"
    parent.write_bytes(b"parent media")

    def runner(command, **kwargs):
        assert kwargs["check"] is False
        assert kwargs["shell"] is False
        Path(command[-1]).write_bytes(b"derived media")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_transform(
        parent,
        output,
        kind="proxy",
        start_seconds=2.0,
        end_seconds=8.0,
        speed_ratio=1.5,
        runner=runner,
    )

    assert result.output_path == output
    assert result.output_path.read_bytes() == b"derived media"
    assert result.transformation.parent_hash
    assert result.transformation.command_digest
    assert result.transformation.time_offset_seconds == 2.0
    assert result.transformation.speed_ratio == 1.5
    assert result.transformation.kind == "proxy"


def test_ffmpeg_transform_cancel_timeout_and_command_shape(tmp_path: Path):
    parent = tmp_path / "parent.mp4"
    output = tmp_path / "clip.mp4"
    parent.write_bytes(b"parent")
    command = build_transform_command(
        parent,
        output,
        kind="clip",
        start_seconds=1.25,
        end_seconds=3.5,
    )
    assert command[0] == "ffmpeg"
    assert "-ss" in command and "-to" in command
    assert "--" not in command

    cancelled = Event()
    cancelled.set()
    with pytest.raises(FfmpegCancelled):
        run_transform(parent, output, kind="clip", cancel_event=cancelled)

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(FfmpegTimeout):
        run_transform(parent, output, kind="clip", runner=timeout_runner)


def test_ffmpeg_speed_command_keeps_audio_and_video_in_sync(tmp_path: Path):
    parent = tmp_path / "parent.mp4"
    parent.write_bytes(b"parent")
    command = build_transform_command(
        parent,
        tmp_path / "fast.mp4",
        kind="clip",
        start_seconds=0.0,
        end_seconds=4.0,
        speed_ratio=2.0,
    )

    assert "-c" not in command or command[command.index("-c") + 1] != "copy"
    assert "-filter:v" in command
    assert "-filter:a" in command
    assert "atempo=2" in command[command.index("-filter:a") + 1]


def test_ffmpeg_video_transforms_map_only_primary_video_and_audio(tmp_path: Path):
    command = build_transform_command(
        tmp_path / "parent.mkv",
        tmp_path / "proxy.mp4",
        kind="proxy",
    )

    map_values = [command[index + 1] for index, item in enumerate(command) if item == "-map"]
    assert map_values == ["0:v:0", "0:a:0?"]


def test_ffmpeg_audio_extract_maps_only_primary_audio(tmp_path: Path):
    command = build_transform_command(
        tmp_path / "parent.mkv",
        tmp_path / "audio.wav",
        kind="audio-extract",
    )

    map_values = [command[index + 1] for index, item in enumerate(command) if item == "-map"]
    assert map_values == ["0:a:0?"]


def test_ffmpeg_copy_speed_is_rejected_before_runner(tmp_path: Path):
    parent = tmp_path / "parent.mp4"
    parent.write_bytes(b"parent")
    called = False

    def runner(command, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(FfmpegError, match="copy.*speed|speed.*copy"):
        run_transform(parent, tmp_path / "fast.mp4", kind="copy", speed_ratio=2.0, runner=runner)
    assert called is False


def test_ffmpeg_audio_extract_speed_is_explicitly_represented(tmp_path: Path):
    command = build_transform_command(
        tmp_path / "parent.mp4",
        tmp_path / "audio.wav",
        kind="audio-extract",
        speed_ratio=0.25,
    )

    assert "-filter:a" in command
    assert command[command.index("-filter:a") + 1].count("atempo=") == 2


def test_ffmpeg_transform_rejects_missing_parent_before_runner(tmp_path: Path):
    called = False

    def runner(command, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(FfmpegError, match="parent"):
        run_transform(tmp_path / "missing.mp4", tmp_path / "output.mp4", kind="copy", runner=runner)
    assert called is False


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe unavailable for local media smoke",
)
def test_real_ffmpeg_and_ffprobe_smoke_fixture(tmp_path: Path):
    source = tmp_path / "generated smoke.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=10:d=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    probe = probe_media(source)
    assert probe.duration_seconds > 0
    assert any(stream.codec_type == "video" for stream in probe.streams)

    output = tmp_path / "generated clip.mp4"
    transformed = run_transform(source, output, kind="clip", start_seconds=0.1, end_seconds=0.4)
    assert transformed.output_path.is_file()
    assert probe_media(transformed.output_path).duration_seconds > 0


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe unavailable for local media smoke",
)
def test_real_ffmpeg_speed_smoke_preserves_audio_and_video(tmp_path: Path):
    source = tmp_path / "generated av source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:r=12:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    original = probe_media(source)
    output = tmp_path / "generated av fast.mp4"
    result = run_transform(source, output, kind="clip", start_seconds=0.0, end_seconds=1.0, speed_ratio=2.0)
    transformed = probe_media(result.output_path)

    assert any(stream.codec_type == "video" for stream in transformed.streams)
    assert any(stream.codec_type == "audio" for stream in transformed.streams)
    assert transformed.duration_seconds < original.duration_seconds * 0.75

@pytest.mark.parametrize("manual,auto,language,expected,origin", [
    ({}, {"ja": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]}, None, "ja", "auto-caption"),
    ({}, {"ja-orig": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]}, "ja", "ja-orig", "auto-caption"),
    ({"en": [{"ext": "vtt"}]}, {"ja": [{"ext": "vtt"}]}, "ja", "en", "manual-caption"),
    ({}, {"fr": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]}, "fr", "fr", "auto-caption"),
    ({}, {"fr-orig": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]}, None, "fr-orig", "auto-caption"),
])
def test_download_selects_and_persists_captions(tmp_path, manual, auto, language, expected, origin):
    from yt2class.adapters.ytdlp import downloaded_subtitle
    from yt2class.adapters.subtitles import build_transcript_document

    media = tmp_path / "lesson [fixture-id].webm"
    def runner(command, **kwargs):
        if "--skip-download" in command:
            selected = command[command.index("--sub-langs") + 1]
            flag = "--write-subs" if origin == "manual-caption" else "--write-auto-subs"
            assert flag in command
            media.with_suffix(f".{selected}.vtt").write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nにつき\n", encoding="utf-8")
        else:
            media.write_bytes(b"video")
            media.with_suffix(".info.json").write_text(json.dumps({
                "id": "fixture-id", "title": "lesson", "language": language,
                "subtitles": manual, "automatic_captions": auto,
            }))
        return SimpleNamespace(returncode=0, stdout=str(media), stderr="")

    download_video("https://youtu.be/fixture-id", tmp_path, runner=runner)
    track = downloaded_subtitle(media)
    assert track is not None
    assert track.path == media.with_suffix(f".{expected}.vtt")
    assert track.origin == origin
    transcript = build_transcript_document(track, source_id="fixture-id", duration_seconds=1.0)
    assert transcript.segments[0].text_original == "につき"


def test_download_without_supported_captions_keeps_media(tmp_path):
    from yt2class.adapters.ytdlp import downloaded_subtitle
    media = tmp_path / "lesson [fixture-id].webm"

    def runner(command, **kwargs):
        assert "--skip-download" not in command
        media.write_bytes(b"media")
        media.with_suffix(".info.json").write_text(json.dumps({
            "id": "fixture-id", "title": "lesson", "subtitles": {}, "automatic_captions": {},
        }))
        return SimpleNamespace(returncode=0, stdout=str(media), stderr="")

    result = download_video("https://youtu.be/fixture-id", tmp_path, runner=runner)
    assert result.media_path.read_bytes() == b"media"
    assert downloaded_subtitle(result.media_path) is None

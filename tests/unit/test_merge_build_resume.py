from __future__ import annotations

from pathlib import Path

from yt2class.config import BuildSource
from yt2class.orchestration.run_request import (
    merge_build_for_resume,
    record_from_build,
    save_run_request,
    subtitles_digest,
)


def test_omitted_cli_fields_do_not_mark_inputs_changed(tmp_path: Path):
    vtt = tmp_path / "lesson.vtt"
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA\n", encoding="utf-8")
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"video-bytes")
    run_root = tmp_path / "run-a"
    run_root.mkdir()
    save_run_request(
        run_root,
        record_from_build(
            BuildSource(video=video, subtitles=vtt, source_id="src-demo"),
            review_revision=0,
        ),
    )
    _, _, changed = merge_build_for_resume(run_root, BuildSource(source_id="src-demo"))
    assert changed is False


def test_explicit_subtitle_change_marks_inputs_changed(tmp_path: Path):
    vtt_a = tmp_path / "a.vtt"
    vtt_b = tmp_path / "b.vtt"
    vtt_a.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA\n", encoding="utf-8")
    vtt_b.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nB\n", encoding="utf-8")
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"video-bytes")
    run_root = tmp_path / "run-b"
    run_root.mkdir()
    save_run_request(
        run_root,
        record_from_build(BuildSource(video=video, subtitles=vtt_a, source_id="src-demo")),
    )
    _, record, changed = merge_build_for_resume(
        run_root,
        BuildSource(video=video, subtitles=vtt_b, source_id="src-demo"),
    )
    assert changed is True
    assert record.subtitles_sha256 == subtitles_digest(vtt_b)

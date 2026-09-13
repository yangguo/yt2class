from __future__ import annotations

from pathlib import Path

import pytest

from yt2class.adapters.subtitles import (
    SubtitleTrack,
    build_transcript_document,
    choose_subtitle_track,
    parse_srt,
    parse_vtt,
)


def test_subtitle_parsers_strip_markup_and_support_vtt_and_srt():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:02.500
<c.green>你好</c> &amp; world

"""
    cues = parse_vtt(vtt)
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (1.0, 2.5, "你好 & world")
    ]

    srt = """1
00:00:03,000 --> 00:00:04,250
<i>第二行</i>
字幕

"""
    srt_cues = parse_srt(srt)
    assert [(cue.start, cue.end, cue.text) for cue in srt_cues] == [
        (3.0, 4.25, "第二行 字幕")
    ]


def test_subtitle_selection_is_sidecar_then_manual_then_auto(tmp_path: Path):
    sidecar = tmp_path / "sidecar.vtt"
    manual = tmp_path / "manual.srt"
    auto = tmp_path / "auto.vtt"
    for path in (sidecar, manual, auto):
        path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\ntext\n", encoding="utf-8")

    selected = choose_subtitle_track(
        sidecar=sidecar,
        manual=[manual],
        auto=[auto],
        language="zh-CN",
    )
    assert selected is not None
    assert selected.path == sidecar
    assert selected.origin == "sidecar"

    sidecar.unlink()
    selected = choose_subtitle_track(sidecar=sidecar, manual=[manual], auto=[auto])
    assert selected is not None
    assert selected.path == manual
    assert selected.origin == "manual-caption"

    manual.unlink()
    selected = choose_subtitle_track(sidecar=sidecar, manual=[manual], auto=[auto])
    assert selected is not None
    assert selected.path == auto
    assert selected.origin == "auto-caption"


def test_subtitle_language_is_inferred_from_bcp47_filename(tmp_path: Path):
    track = tmp_path / "lesson.zh-Hans.vtt"
    track.write_text("WEBVTT\n", encoding="utf-8")
    selected = choose_subtitle_track(sidecar=track)
    assert selected is not None
    assert selected.language == "zh-Hans"


def test_transcript_normalization_deduplicates_scrolling_but_keeps_real_repeat_and_overlap(
    tmp_path: Path,
):
    subtitle = tmp_path / "lesson.vtt"
    subtitle.write_text(
        """WEBVTT

00:00:00.000 --> 00:00:02.000
滚动字幕

00:00:01.500 --> 00:00:03.000
滚动字幕

00:00:04.000 --> 00:00:05.000
重复

00:00:06.000 --> 00:00:07.000
重复

00:00:08.000 --> 00:00:09.500
甲

00:00:08.500 --> 00:00:10.000
乙
""",
        encoding="utf-8",
    )
    document = build_transcript_document(
        SubtitleTrack(path=subtitle, language="zh-CN", origin="sidecar"),
        source_id="src-test",
        duration_seconds=12.0,
    )

    assert document.raw_artifact_hash
    assert [segment.text_original for segment in document.segments] == [
        "滚动字幕",
        "重复",
        "重复",
        "甲",
        "乙",
    ]
    assert document.segments[-2].quality_flags == ["overlap"]
    assert document.segments[-1].quality_flags == ["overlap"]
    assert document.speech_coverage.denominator == "timeline"
    assert document.speech_coverage.denominator_seconds == 12.0
    assert document.speech_coverage.coverage_ratio == pytest.approx(6.0 / 12.0)


def test_empty_and_bad_time_subtitles_create_explainable_gaps(tmp_path: Path):
    subtitle = tmp_path / "bad.vtt"
    subtitle.write_text(
        """WEBVTT

not-a-time --> 00:00:01.000
bad time

00:00:03.000 --> 00:00:02.000
backwards
""",
        encoding="utf-8",
    )
    document = build_transcript_document(
        SubtitleTrack(path=subtitle, language="en", origin="auto-caption"),
        source_id="src-empty",
        duration_seconds=5.0,
    )

    assert document.segments == []
    assert document.status == "degraded"
    assert any("invalid" in gap.reason or "empty" in gap.reason for gap in document.gaps)
    assert document.speech_coverage.coverage_ratio == 0.0

    empty = tmp_path / "empty.vtt"
    empty.write_text("WEBVTT\n", encoding="utf-8")
    empty_document = build_transcript_document(
        SubtitleTrack(path=empty, language="en", origin="sidecar"),
        source_id="src-empty",
        duration_seconds=5.0,
    )
    assert any(gap.reason == "empty-subtitle" for gap in empty_document.gaps)


def test_subtitle_normalization_merges_overlapping_prefix_extensions(tmp_path: Path):
    subtitle = tmp_path / "rolling.vtt"
    subtitle.write_text(
        """WEBVTT

00:00:00.000 --> 00:00:01.000
hello

00:00:00.500 --> 00:00:01.500
hello world

00:00:02.000 --> 00:00:02.500
hello

00:00:03.000 --> 00:00:03.500
hello
""",
        encoding="utf-8",
    )
    document = build_transcript_document(
        SubtitleTrack(subtitle, language="en", origin="auto-caption"),
        source_id="src-rolling",
        duration_seconds=4.0,
    )
    assert [(segment.start_seconds, segment.end_seconds, segment.text_original) for segment in document.segments] == [
        (0.0, 1.5, "hello world"),
        (2.0, 2.5, "hello"),
        (3.0, 3.5, "hello"),
    ]
    assert any(gap.reason == "uncovered" for gap in document.gaps)
    assert document.status == "degraded"


def test_transcript_without_raw_artifact_hash_is_valid_only_without_segments():
    from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptGap, TranscriptSegment

    empty = TranscriptDocument(
        schema_version="1.0",
        source_id="src-empty",
        language="und",
        raw_artifact_hash=None,
        alignment="none",
        speech_coverage=SpeechCoverage(
            speech_seconds=0.0,
            covered_seconds=0.0,
            denominator="timeline",
            denominator_seconds=2.0,
            coverage_ratio=0.0,
        ),
        duration_seconds=2.0,
        status="degraded",
        gaps=[
            TranscriptGap(id="gap-0001", start_seconds=0.0, end_seconds=2.0, reason="empty")
        ],
    )
    assert empty.raw_artifact_hash is None

    with pytest.raises(ValueError, match="raw_artifact_hash"):
        TranscriptDocument(
            schema_version="1.0",
            source_id="src-without-hash",
            language="en",
            raw_artifact_hash=None,
            alignment="sentence",
            speech_coverage=SpeechCoverage(
                speech_seconds=1.0,
                covered_seconds=1.0,
                denominator="timeline",
                denominator_seconds=2.0,
                coverage_ratio=0.5,
            ),
            segments=[
                TranscriptSegment(
                    id="seg-1",
                    start_seconds=0.0,
                    end_seconds=1.0,
                    text_original="hello",
                    language="en",
                    origin="sidecar",
                )
            ],
            duration_seconds=2.0,
            status="degraded",
        )


def test_subtitle_cues_after_source_duration_create_bounded_gaps(tmp_path: Path):
    subtitle = tmp_path / "after.vtt"
    subtitle.write_text(
        """WEBVTT

00:00:05.000 --> 00:00:06.000
late cue
""",
        encoding="utf-8",
    )
    document = build_transcript_document(
        SubtitleTrack(subtitle, language="en", origin="sidecar"),
        source_id="src-late",
        duration_seconds=2.0,
    )
    assert document.status == "degraded"
    assert document.segments == []
    assert document.gaps
    assert all(0.0 <= gap.start_seconds < gap.end_seconds <= 2.0 for gap in document.gaps)


def test_complete_timeline_transcript_rejects_uncovered_duration():
    from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptSegment

    with pytest.raises(ValueError, match="uncovered duration|complete"):
        TranscriptDocument(
            schema_version="1.0",
            source_id="src-incomplete",
            language="en",
            raw_artifact_hash="a" * 64,
            alignment="sentence",
            speech_coverage=SpeechCoverage(
                speech_seconds=1.0,
                covered_seconds=1.0,
                denominator="timeline",
                denominator_seconds=2.0,
                coverage_ratio=0.5,
            ),
            segments=[
                TranscriptSegment(
                    id="seg-1",
                    start_seconds=0.0,
                    end_seconds=1.0,
                    text_original="hello",
                    language="en",
                    origin="sidecar",
                )
            ],
            duration_seconds=2.0,
            status="complete",
        )


def test_complete_transcript_requires_explicit_duration_and_timeline_denominator():
    from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptSegment

    with pytest.raises(ValueError, match="duration|denominator"):
        TranscriptDocument(
            schema_version="1.0",
            source_id="src-no-duration",
            language="en",
            raw_artifact_hash="a" * 64,
            alignment="sentence",
            speech_coverage=SpeechCoverage(
                speech_seconds=1.0,
                covered_seconds=1.0,
                denominator="timeline",
                coverage_ratio=1.0,
            ),
            segments=[
                TranscriptSegment(
                    id="seg-1",
                    start_seconds=0.0,
                    end_seconds=1.0,
                    text_original="hello",
                    language="en",
                    origin="sidecar",
                )
            ],
            status="complete",
        )


def test_forged_complete_coverage_is_derived_from_segment_unions():
    from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptSegment

    with pytest.raises(ValueError, match="derived|uncovered|complete"):
        TranscriptDocument(
            schema_version="1.0",
            source_id="src-forged",
            language="en",
            raw_artifact_hash="a" * 64,
            alignment="sentence",
            speech_coverage=SpeechCoverage(
                speech_seconds=2.0,
                covered_seconds=2.0,
                denominator="timeline",
                denominator_seconds=2.0,
                coverage_ratio=1.0,
            ),
            segments=[
                TranscriptSegment(
                    id="seg-1",
                    start_seconds=0.5,
                    end_seconds=1.0,
                    text_original="sparse",
                    language="en",
                    origin="sidecar",
                )
            ],
            duration_seconds=2.0,
            status="complete",
        )

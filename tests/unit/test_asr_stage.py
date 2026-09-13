from __future__ import annotations

from yt2class.adapters.asr import ASRResult, ASRSegment
from yt2class.domain.transcript import TranscriptWord
from yt2class.stages.extract_evidence import _transcript_from_asr


def test_asr_transcript_clips_words_to_clipped_segment_and_marks_quality():
    result = ASRResult(
        request_id="asr-1",
        source_id="src-1",
        engine="whisperx",
        model="tiny",
        language="en",
        device="cpu",
        alignment="word",
        diarization=False,
        offset_seconds=0.0,
        status="complete",
        raw_artifact_hash="a" * 64,
        audio_sha256="b" * 64,
        parent_hash="c" * 64,
        segments=[
            ASRSegment(
                id="asr-1",
                start_seconds=1.0,
                end_seconds=3.0,
                text_original="hello world",
                language="en",
                words=[
                    TranscriptWord(text="before", start_seconds=0.5, end_seconds=1.2),
                    TranscriptWord(text="inside", start_seconds=1.2, end_seconds=2.0),
                    TranscriptWord(text="after", start_seconds=2.8, end_seconds=3.2),
                ],
                alignment_status="aligned",
            )
        ],
    )

    document = _transcript_from_asr(result, source_id="src-1", duration=2.5)
    segment = document.segments[0]
    assert segment.end_seconds == 2.5
    assert [(word.text, word.start_seconds, word.end_seconds) for word in segment.words or []] == [
        ("before", 1.0, 1.2),
        ("inside", 1.2, 2.0),
    ]
    assert "word-outside-clipped-segment" in segment.quality_flags

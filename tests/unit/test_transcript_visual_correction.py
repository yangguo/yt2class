from __future__ import annotations

from yt2class.stages.transcript_visual_correction import correct_transcript_from_visual
from tests.helpers.m2 import make_transcript, make_visual


def test_ocr_tsuki_corrects_asr_nigatsu_in_overlapping_segment():
    transcript = make_transcript(
        [
            (
                "cap-head",
                0.0,
                20.0,
                "今日は2月の文法、～につきの用法を説明します。",
            )
        ],
        duration=60.0,
        language="ja",
    )
    transcript.segments[0] = transcript.segments[0].model_copy(update={"origin": "asr"})
    visual = make_visual(
        [("frame-board", 8.0, "scene-001")],
        duration=60.0,
        ocr=[("ocr-tsuki", "frame-board", "文法 ～につき")],
    )
    corrected, applied = correct_transcript_from_visual(transcript, visual)
    assert applied
    assert "2月" not in corrected.segments[0].text_original
    assert "～につき" in corrected.segments[0].text_original
    assert "visual-asr-corrected" in corrected.segments[0].quality_flags
    assert applied[0].visual_evidence_ids


def test_without_ocr_headword_asr_surface_is_unchanged():
    transcript = make_transcript(
        [("cap-cal", 0.0, 20.0, "来月は2月です。")],
        duration=60.0,
        language="ja",
    )
    transcript.segments[0] = transcript.segments[0].model_copy(update={"origin": "asr"})
    visual = make_visual(
        [("frame-cal", 8.0, "scene-001")],
        duration=60.0,
        ocr=[("ocr-cal", "frame-cal", "2月の予定")],
    )
    corrected, applied = correct_transcript_from_visual(transcript, visual)
    assert not applied
    assert corrected.segments[0].text_original == "来月は2月です。"

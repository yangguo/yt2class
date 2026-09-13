from __future__ import annotations

from threading import Event

import pytest

from yt2class.adapters.providers.base import RequestCancelled
from yt2class.domain.segment import cores_cover_duration, cores_overlap
from yt2class.orchestration.scheduler import (
    SchedulerConfig,
    SchedulerError,
    schedule_windows,
    sentence_boundaries,
)
from tests.helpers.m2 import empty_visual, frames_caps, make_transcript, make_visual


def _schedule(duration: float, transcript=None, visual=None, caps=None, **kwargs):
    transcript = transcript or make_transcript([], duration=duration)
    visual = visual if visual is not None else empty_visual(duration=duration)
    return schedule_windows(
        source_id="src-demo",
        duration_seconds=duration,
        transcript=transcript,
        visual=visual,
        capabilities=caps or frames_caps(),
        **kwargs,
    )


def test_sentence_boundaries_require_terminal_punctuation():
    transcript = make_transcript(
        [
            ("cap-001", 0.0, 10.0, "这是一句完整的话。"),
            ("cap-002", 10.0, 20.0, "这句没有句末标点"),
            ("cap-003", 20.0, 30.0, "Done!"),
            ("cap-004", 30.0, 40.0, "wait,"),
        ],
        duration=40.0,
    )
    assert sentence_boundaries(transcript) == [10.0, 30.0]


def test_zero_duration_is_rejected():
    with pytest.raises(SchedulerError, match="non-positive"):
        schedule_windows(
            source_id="src-demo",
            duration_seconds=0.0,
            transcript=make_transcript([("cap-001", 0.0, 1.0, "x")], duration=1.0),
            visual=empty_visual(duration=1.0),
            capabilities=frames_caps(),
        )


@pytest.mark.parametrize("duration", [0.5, 5.0, 30.0])
def test_short_videos_get_one_core_window(duration: float):
    manifest = _schedule(duration)
    assert len(manifest.windows) == 1
    assert cores_cover_duration(manifest.windows, duration)
    assert not cores_overlap(manifest.windows)
    window = manifest.windows[0]
    assert window.core_start_seconds == 0.0
    assert window.core_end_seconds == duration
    assert window.context_start_seconds <= window.core_start_seconds
    assert window.context_end_seconds >= window.core_end_seconds
    assert window.status == "scheduled"


def test_long_video_tiles_without_core_overlap():
    duration = 600.0
    transcript = make_transcript(
        [(f"cap-{i:03d}", float(i * 10), float(i * 10 + 10), f"句子{i}。") for i in range(60)],
        duration=duration,
    )
    manifest = _schedule(duration, transcript=transcript)
    assert len(manifest.windows) >= 4
    assert cores_cover_duration(manifest.windows, duration)
    assert not cores_overlap(manifest.windows)
    for previous, current in zip(manifest.windows, manifest.windows[1:]):
        assert previous.core_end_seconds == current.core_start_seconds
        assert current.context_start_seconds < current.core_start_seconds or current.core_start_seconds == 0
        assert previous.context_end_seconds > previous.core_end_seconds or previous.core_end_seconds == duration


def test_ultra_long_sentence_splits_at_max_core():
    transcript = make_transcript(
        [("cap-long", 0.0, 400.0, "这是一句没有句号的超长讲解 " * 40)],
        duration=400.0,
    )
    manifest = _schedule(
        400.0,
        transcript=transcript,
        config=SchedulerConfig(core_seconds=120.0, max_core_seconds=180.0),
    )
    lengths = [window.core_end_seconds - window.core_start_seconds for window in manifest.windows]
    assert all(length <= 180.0 + 1e-9 for length in lengths)
    assert any(abs(length - 180.0) < 1e-6 for length in lengths[:-1])
    assert cores_cover_duration(manifest.windows, 400.0)


def test_no_scenes_still_schedules_full_duration():
    transcript = make_transcript([("cap-001", 0.0, 90.0, "只有语音。")], duration=90.0)
    manifest = _schedule(90.0, transcript=transcript, visual=empty_visual(duration=90.0))
    assert cores_cover_duration(manifest.windows, 90.0)
    assert all(not window.image_batches[0].evidence_ids for window in manifest.windows)


def test_multi_batch_images_stay_in_one_core_window():
    frames = [(f"frame-{index:03d}", float(index * 2), "scene-001") for index in range(20)]
    visual = make_visual(frames, duration=40.0)
    transcript = make_transcript([("cap-001", 0.0, 40.0, "多图课件。")], duration=40.0)
    manifest = _schedule(
        40.0,
        transcript=transcript,
        visual=visual,
        caps=frames_caps(max_images=8),
        config=SchedulerConfig(core_seconds=120.0, max_core_seconds=180.0),
    )
    assert len(manifest.windows) == 1
    batches = manifest.windows[0].image_batches
    assert len(batches) == 3
    assert [batch.image_count for batch in batches] == [8, 8, 4]
    assert sum(len(batch.evidence_ids) for batch in batches) == 20


def test_cancel_stops_scheduling():
    cancel = Event()
    cancel.set()
    with pytest.raises(RequestCancelled, match="cancelled"):
        _schedule(120.0, cancel_event=cancel)


def test_low_output_reserve_is_scheduled_not_overflowed():
    from yt2class.adapters.providers.base import FakeProvider
    from yt2class.stages.analyze_segments import analyze_segments

    transcript = make_transcript([("cap-001", 0.0, 40.0, "短课。")], duration=40.0)
    visual = make_visual([("frame-001", 10.0, "scene-001")], duration=40.0)
    caps = frames_caps(max_output_tokens=256)
    manifest = _schedule(40.0, transcript=transcript, visual=visual, caps=caps)
    assert manifest.windows
    assert all(window.estimated_output_tokens == 256 for window in manifest.windows)
    assert all(window.status == "scheduled" for window in manifest.windows)
    provider = FakeProvider(
        caps,
        structured={
            "units": [
                {
                    "id": "unit-1",
                    "topic_id": "topic-1",
                    "segment_ids": [manifest.windows[0].id],
                    "start_seconds": 0.0,
                    "end_seconds": 20.0,
                    "kind": "concept",
                    "claims": [
                        {
                            "id": "claim-1",
                            "text": "短课。",
                            "evidence_ids": ["cap-001"],
                            "status": "draft",
                            "qualifiers": [],
                            "modality": "audio",
                            "provenance": "source",
                        }
                    ],
                }
            ]
        },
    )
    from tests.helpers.m2 import sample_course_map

    updated, units, _outcomes = analyze_segments(
        manifest,
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(duration=40.0),
        provider=provider,
    )
    assert units
    assert all(window.status != "failed" or "context" not in (window.failure_reason or "") for window in updated.windows)
    assert provider.requests[0].estimated_output_tokens == 256


def test_oversized_transcript_only_window_splits_or_gaps():
    transcript = make_transcript(
        [("cap-huge", 0.0, 60.0, "讲解" * 3900)],
        duration=60.0,
    )
    manifest = _schedule(
        60.0,
        transcript=transcript,
        visual=empty_visual(duration=60.0),
        caps=frames_caps(max_input_tokens=200),
    )
    assert cores_cover_duration(manifest.windows, 60.0)
    assert len(manifest.windows) > 1 or any(
        window.status == "failed" and "unschedulable" in (window.failure_reason or "")
        for window in manifest.windows
    )
    assert any(
        "unschedulable" in (window.failure_reason or "") or len(manifest.windows) > 1
        for window in manifest.windows
    )


def test_core_union_property_across_durations():
    for duration in (1.0, 119.0, 120.0, 121.0, 180.0, 181.0, 240.0, 1000.0):
        manifest = _schedule(duration)
        assert cores_cover_duration(manifest.windows, duration)
        assert not cores_overlap(manifest.windows)
        assert all(window.status == "scheduled" for window in manifest.windows)

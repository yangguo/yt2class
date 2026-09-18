from __future__ import annotations

from pathlib import Path

from yt2class.adapters.providers.synthetic import fake_course_provider
from yt2class.domain.segment import AnalysisWindow
from yt2class.stages.analyze_segments import (
    analyze_segments,
    read_cached_window_outcome,
    write_cached_window_outcome,
    SegmentAnalysisOutcome,
)
from tests.helpers.m2 import empty_visual, frames_caps, make_transcript, sample_course_map
from yt2class.orchestration.scheduler import schedule_windows


def test_window_cache_skips_repeat_provider_calls(tmp_path: Path):
    transcript = make_transcript([("cap-001", 0.0, 40.0, "讲解。")], duration=40.0)
    visual = empty_visual(duration=40.0)
    course_map = sample_course_map(duration=40.0)
    manifest = schedule_windows(
        source_id="src-demo",
        duration_seconds=40.0,
        transcript=transcript,
        visual=visual,
        capabilities=frames_caps(max_input_tokens=32000),
        course_map=course_map,
    )
    provider = fake_course_provider(frames_caps(max_input_tokens=32000))
    cache_dir = tmp_path / "windows"

    first_manifest, _, _ = analyze_segments(
        manifest,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=provider,
        window_cache_dir=cache_dir,
    )
    calls_after_first = len(provider.requests)

    second_manifest, _, _ = analyze_segments(
        first_manifest,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=provider,
        window_cache_dir=cache_dir,
    )
    assert len(provider.requests) == calls_after_first
    assert second_manifest.windows[0].status == first_manifest.windows[0].status


def test_write_and_read_cached_outcome_roundtrip(tmp_path: Path):
    window = AnalysisWindow(
        id="seg-0001",
        core_start_seconds=0.0,
        core_end_seconds=10.0,
        context_start_seconds=0.0,
        context_end_seconds=10.0,
        evidence_ids=[],
        status="complete",
    )
    outcome = SegmentAnalysisOutcome(window=window, payload={}, units=[], repaired=False)
    write_cached_window_outcome(tmp_path, outcome)
    loaded = read_cached_window_outcome(tmp_path, "seg-0001")
    assert loaded is not None
    assert loaded.window.id == "seg-0001"
    assert loaded.window.status == "complete"

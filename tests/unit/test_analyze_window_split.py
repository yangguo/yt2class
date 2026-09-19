from __future__ import annotations

from yt2class.adapters.providers.base import (
    ContextOverflow,
    FakeProvider,
    MissingStructuredOutput,
    ModelResult,
    Usage,
)
from yt2class.domain.segment import AnalysisWindow
from yt2class.stages.analyze_segments import analyze_window, split_window_core_half
from tests.helpers.m2 import empty_visual, frames_caps, make_transcript, sample_course_map


def test_split_window_core_half_requires_minimum_span():
    window = AnalysisWindow(
        id="win-1",
        core_start_seconds=0.0,
        core_end_seconds=1.0,
        context_start_seconds=0.0,
        context_end_seconds=1.0,
        status="scheduled",
    )
    assert split_window_core_half(window) is None


def test_analyze_window_splits_on_context_overflow():
    transcript = make_transcript([("cap-1", 0.0, 120.0, "课程内容。")], duration=120.0)
    visual = empty_visual(duration=120.0)
    course_map = sample_course_map()
    window = AnalysisWindow(
        id="win-split",
        core_start_seconds=0.0,
        core_end_seconds=120.0,
        context_start_seconds=0.0,
        context_end_seconds=120.0,
        status="scheduled",
    )
    calls: list[str] = []

    class SplitProvider(FakeProvider):
        def _complete(self, request, *, cancel_event=None):
            calls.append(request.request_id)
            if ":a" not in request.request_id and ":b" not in request.request_id:
                raise ContextOverflow("payload too large")
            return ModelResult(
                request_id=request.request_id,
                structured={"units": []},
                usage=Usage(
                    request_id=request.request_id,
                    input_tokens=10,
                    output_tokens=1,
                ),
            )

    provider = SplitProvider(frames_caps(max_input_tokens=32000))
    outcome = analyze_window(
        window,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=provider,
    )
    assert outcome.window.status in {"degraded", "complete", "failed"}
    assert any(":a" in item for item in calls)
    assert any(":b" in item for item in calls)


def test_analyze_window_splits_after_invalid_json_repair():
    transcript = make_transcript([("cap-1", 0.0, 120.0, "课程内容。")], duration=120.0)
    visual = empty_visual(duration=120.0)
    course_map = sample_course_map()
    window = AnalysisWindow(
        id="win-json-split",
        core_start_seconds=0.0,
        core_end_seconds=120.0,
        context_start_seconds=0.0,
        context_end_seconds=120.0,
        status="scheduled",
    )
    calls: list[str] = []

    class JsonRepairFailProvider(FakeProvider):
        def _complete(self, request, *, cancel_event=None):
            calls.append(request.request_id)
            if ":repair" in request.request_id:
                raise MissingStructuredOutput("model response did not contain a JSON object")
            if ":a" not in request.request_id and ":b" not in request.request_id:
                raise MissingStructuredOutput("invalid json")
            return ModelResult(
                request_id=request.request_id,
                structured={"units": []},
                usage=Usage(
                    request_id=request.request_id,
                    input_tokens=10,
                    output_tokens=1,
                ),
            )

    provider = JsonRepairFailProvider(frames_caps(max_input_tokens=32000))
    outcome = analyze_window(
        window,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=provider,
    )
    assert outcome.window.status in {"degraded", "complete", "failed"}
    assert any(":repair" in item for item in calls)
    assert any(":a" in item for item in calls)
    assert any(":b" in item for item in calls)

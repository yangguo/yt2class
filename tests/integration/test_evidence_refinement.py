from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.knowledge import KnowledgeEvidenceRequest, KnowledgeUnit
from yt2class.domain.segment import AnalysisWindow
from yt2class.stages.analyze_segments import SegmentAnalysisOutcome
from yt2class.stages.evidence_refinement import (
    RefinementBudget,
    accept_refinement_request,
    refine_window,
)
from tests.helpers.m2 import claim, frames_caps, make_transcript, make_visual, sample_course_map, unit


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        id="seg-0001",
        core_start_seconds=0.0,
        core_end_seconds=60.0,
        context_start_seconds=0.0,
        context_end_seconds=60.0,
        evidence_ids=["cap-001", "frame-001"],
        status="degraded",
    )


def _request(kind: str, start: float, end: float, modality: str) -> KnowledgeEvidenceRequest:
    return KnowledgeEvidenceRequest(
        start_seconds=start,
        end_seconds=end,
        reason=kind,
        desired_modality=modality,
    )


def _unit_with_request(request: KnowledgeEvidenceRequest) -> KnowledgeUnit:
    return unit(
        "unit-1",
        start=10.0,
        end=20.0,
        claims=[claim("claim-1", "板书看不清", ["cap-001"])],
    ).model_copy(update={"evidence_requests": [request]})


def test_scheduler_rejects_out_of_range_modality_and_budget():
    window = _window()
    budget = RefinementBudget(remaining_frames=0, remaining_clip_seconds=0.0)
    caps = frames_caps(supports_video=False)
    assert (
        accept_refinement_request(
            _request("unreadable_text", 50.0, 70.0, "frame"),
            window=window,
            duration_seconds=60.0,
            capabilities=caps,
            budget=RefinementBudget(),
            segment_id=window.id,
        ).reason
        == "out_of_range"
    )
    assert (
        accept_refinement_request(
            _request("missing_step", 10.0, 20.0, "clip"),
            window=window,
            duration_seconds=60.0,
            capabilities=caps,
            budget=RefinementBudget(),
            segment_id=window.id,
        ).reason
        == "modality_not_allowed"
    )
    assert (
        accept_refinement_request(
            _request("unreadable_text", 10.0, 12.0, "frame"),
            window=window,
            duration_seconds=60.0,
            capabilities=caps,
            budget=budget,
            segment_id=window.id,
        ).reason
        == "budget_exhausted"
    )
    accepted = accept_refinement_request(
        _request("audio_visual_conflict", 12.0, 18.0, "frame"),
        window=window,
        duration_seconds=60.0,
        capabilities=caps,
        budget=RefinementBudget(),
        segment_id=window.id,
    )
    assert accepted.accepted is True


def test_unreadable_text_pulls_nearby_hd_frames_and_reanalyzes_only_that_window():
    transcript = make_transcript([("cap-001", 10.0, 20.0, "板书文字。")], duration=60.0)
    visual = make_visual([("frame-001", 12.5, "scene-001")], duration=60.0)
    request = _request("unreadable_text", 11.0, 14.0, "frame")
    outcome = SegmentAnalysisOutcome(
        window=_window(),
        payload={"segment_id": "seg-0001"},
        units=[_unit_with_request(request)],
    )
    valid = {
        "units": [
            {
                "id": "unit-1",
                "topic_id": "topic-1",
                "segment_ids": ["seg-0001"],
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "kind": "concept",
                "claims": [
                    {
                        "id": "claim-1",
                        "text": "板书文字。",
                        "evidence_ids": ["cap-001", "frame-001"],
                        "status": "draft",
                        "qualifiers": [],
                        "modality": "both",
                        "provenance": "source",
                    }
                ],
            }
        ]
    }
    provider = FakeProvider(frames_caps(), structured=valid)
    updated, new_visual, budget = refine_window(
        outcome,
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
        provider=provider,
        capabilities=frames_caps(),
        duration_seconds=60.0,
    )
    assert any(item.id.startswith("frame-refine-") for item in new_visual.occurrences)
    assert updated.units
    assert budget.rounds_for("seg-0001") == 1
    assert updated.window.status in {"complete", "degraded"}


def test_missing_step_clip_must_be_five_to_thirty_seconds():
    window = _window()
    caps = frames_caps(supports_video=True, max_video_seconds=30.0)
    short = accept_refinement_request(
        _request("missing_step", 10.0, 12.0, "clip"),
        window=window,
        duration_seconds=60.0,
        capabilities=caps,
        budget=RefinementBudget(),
        segment_id=window.id,
    )
    long = accept_refinement_request(
        _request("missing_step", 10.0, 50.0, "clip"),
        window=window,
        duration_seconds=60.0,
        capabilities=caps,
        budget=RefinementBudget(),
        segment_id=window.id,
    )
    ok = accept_refinement_request(
        _request("missing_step", 10.0, 25.0, "clip"),
        window=window,
        duration_seconds=60.0,
        capabilities=caps,
        budget=RefinementBudget(),
        segment_id=window.id,
    )
    assert short.reason == "clip_duration"
    assert long.reason == "clip_duration"
    assert ok.accepted is True


def test_two_rounds_then_budget_exhaust_marks_unresolved():
    transcript = make_transcript([("cap-001", 10.0, 20.0, "冲突内容。")], duration=60.0)
    visual = make_visual([("frame-001", 12.5, "scene-001")], duration=60.0)
    request = _request("audio_visual_conflict", 11.0, 14.0, "frame")

    def needy_unit():
        return _unit_with_request(request)

    structured = {
        "units": [
            {
                "id": "unit-1",
                "topic_id": "topic-1",
                "segment_ids": ["seg-0001"],
                "start_seconds": 10.0,
                "end_seconds": 20.0,
                "kind": "concept",
                "claims": [
                    {
                        "id": "claim-1",
                        "text": "冲突内容。",
                        "evidence_ids": ["cap-001"],
                        "status": "draft",
                        "qualifiers": [],
                        "modality": "audio",
                        "provenance": "source",
                    }
                ],
                "evidence_requests": [
                    {
                        "start_seconds": 11.0,
                        "end_seconds": 14.0,
                        "reason": "audio_visual_conflict",
                        "desired_modality": "frame",
                    }
                ],
            }
        ]
    }
    provider = FakeProvider(frames_caps(), structured=structured)
    outcome = SegmentAnalysisOutcome(window=_window(), payload={}, units=[needy_unit()])
    updated, _visual, budget = refine_window(
        outcome,
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
        provider=provider,
        capabilities=frames_caps(),
        duration_seconds=60.0,
        budget=RefinementBudget(max_rounds=2, remaining_frames=8),
    )
    assert budget.rounds_for("seg-0001") == 2
    assert any(claim.status == "unresolved" for unit in updated.units for claim in unit.claims)
    assert updated.window.status == "degraded"
    # A third implicit request must not loop forever.
    assert budget.rounds_for("seg-0001") <= 2

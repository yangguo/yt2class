from __future__ import annotations

import pytest

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.segment import AnalysisWindow
from yt2class.stages.analyze_segments import (
    SegmentContractError,
    analyze_window,
    build_segment_payload,
    validate_knowledge_units,
)
from tests.helpers.m2 import frames_caps, make_transcript, make_visual, sample_course_map


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        id="seg-0001",
        core_start_seconds=0.0,
        core_end_seconds=60.0,
        context_start_seconds=0.0,
        context_end_seconds=60.0,
        evidence_ids=["cap-001", "frame-001"],
        status="running",
    )


def _valid_unit(**overrides):
    payload = {
        "id": "unit-1",
        "topic_id": "topic-1",
        "segment_ids": ["seg-0001"],
        "start_seconds": 10.0,
        "end_seconds": 20.0,
        "kind": "concept",
        "claims": [
            {
                "id": "claim-1",
                "text": "此画面为课程原始截图。",
                "evidence_ids": ["cap-001", "frame-001"],
                "status": "draft",
                "qualifiers": [],
                "modality": "both",
                "provenance": "source",
            }
        ],
        "relations": [],
        "visual_candidates": [],
        "uncertainty": [],
        "evidence_requests": [],
    }
    payload.update(overrides)
    return payload


def test_request_includes_ordered_frames_transcript_ocr_and_allowed_ids():
    transcript = make_transcript(
        [("cap-001", 10.0, 20.0, "此画面为课程原始截图。")],
        duration=60.0,
    )
    visual = make_visual(
        [("frame-002", 40.0, "scene-001"), ("frame-001", 12.5, "scene-001")],
        duration=60.0,
        ocr=[("ocr-001", "frame-001", "原始截图")],
    )
    payload = build_segment_payload(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
    )
    assert [frame["id"] for frame in payload["frames"]] == ["frame-001", "frame-002"]
    assert all("path" not in frame for frame in payload["frames"])
    assert payload["transcript"][0]["id"] == "cap-001"
    assert payload["ocr"][0]["id"] == "ocr-001"
    assert payload["course_context"]["topic_id"] == "topic-1"
    assert set(payload["allowed_evidence_ids"]) >= {"cap-001", "frame-001", "ocr-001"}
    assert payload["constraints"]["page_budget"] is None


def test_rejects_unknown_ids_bare_paths_and_unsourced_facts():
    payload = build_segment_payload(
        _window(),
        transcript=make_transcript([("cap-001", 10.0, 20.0, "内容。")], duration=60.0),
        visual=make_visual([("frame-001", 12.5, "scene-001")], duration=60.0),
        course_map=sample_course_map(),
    )
    with pytest.raises(SegmentContractError, match="unknown evidence"):
        validate_knowledge_units({"units": [_valid_unit(claims=[{
            "id": "claim-1",
            "text": "无来源之外的事实",
            "evidence_ids": ["frame-missing"],
            "status": "draft",
            "qualifiers": [],
            "modality": "visual",
            "provenance": "source",
        }])]}, payload)
    with pytest.raises(SegmentContractError, match="bare image path"):
        validate_knowledge_units(
            {"units": [_valid_unit(claims=[{
                "id": "claim-1",
                "text": "见图",
                "evidence_ids": ["/tmp/slide.jpg"],
                "status": "draft",
                "qualifiers": [],
                "modality": "visual",
                "provenance": "source",
            }])]},
            {**payload, "allowed_evidence_ids": ["/tmp/slide.jpg", *payload["allowed_evidence_ids"]]},
        )
    with pytest.raises(SegmentContractError, match="unsourced|at least 1"):
        validate_knowledge_units(
            {"units": [_valid_unit(claims=[{
                "id": "claim-1",
                "text": "无证据",
                "evidence_ids": [],
                "status": "draft",
                "qualifiers": [],
                "modality": "audio",
                "provenance": "source",
            }])]},
            payload,
        )


def test_rejects_single_frame_temporal_sequence_and_dropped_negation_or_units():
    transcript = make_transcript(
        [("cap-001", 10.0, 20.0, "这不是自动词。加热 3 分钟。")],
        duration=60.0,
    )
    visual = make_visual([("frame-001", 12.5, "scene-001")], duration=60.0)
    payload = build_segment_payload(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
    )
    with pytest.raises(SegmentContractError, match="temporal sequence"):
        validate_knowledge_units(
            {"units": [_valid_unit(
                kind="procedure",
                relations=[{"from_id": "claim-1", "to_id": "unit-1", "kind": "step_before"}],
            )]},
            payload,
        )
    with pytest.raises(SegmentContractError, match="negation"):
        validate_knowledge_units(
            {"units": [_valid_unit(claims=[{
                "id": "claim-1",
                "text": "这是自动词。",
                "evidence_ids": ["cap-001"],
                "status": "draft",
                "qualifiers": [],
                "modality": "audio",
                "provenance": "source",
            }])]},
            payload,
        )
    with pytest.raises(SegmentContractError, match="unit or quantity"):
        validate_knowledge_units(
            {"units": [_valid_unit(claims=[{
                "id": "claim-1",
                "text": "这不是自动词。加热一段时间。",
                "evidence_ids": ["cap-001"],
                "status": "draft",
                "qualifiers": [],
                "modality": "audio",
                "provenance": "source",
            }])]},
            payload,
        )


def test_http_ok_contract_failure_repairs_once_then_fails_ledger():
    transcript = make_transcript([("cap-001", 10.0, 20.0, "内容。")], duration=60.0)
    visual = make_visual([("frame-001", 12.5, "scene-001")], duration=60.0)
    course_map = sample_course_map()
    valid = {"units": [_valid_unit()]}
    provider = FakeProvider(frames_caps(), sequential=[{"units": "nope"}, valid])
    outcome = analyze_window(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=provider,
    )
    assert outcome.repaired is True
    assert outcome.window.status == "complete"
    assert outcome.units[0].id == "unit-1"

    failing = FakeProvider(frames_caps(), sequential=[{"units": "nope"}, {"units": "still-nope"}])
    failed = analyze_window(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=failing,
    )
    assert failed.window.status == "failed"
    assert failed.units == []
    assert failed.repaired is True
    assert failed.error
    assert len(failing.requests) == 2


def test_out_of_window_known_ids_are_rejected():
    transcript = make_transcript(
        [("cap-001", 10.0, 20.0, "当前窗口。"), ("cap-late", 80.0, 90.0, "后段。")],
        duration=120.0,
    )
    visual = make_visual(
        [("frame-001", 12.5, "scene-001"), ("frame-late", 85.0, "scene-001")],
        duration=120.0,
    )
    window = AnalysisWindow(
        id="seg-0001",
        core_start_seconds=0.0,
        core_end_seconds=60.0,
        context_start_seconds=0.0,
        context_end_seconds=60.0,
        evidence_ids=["cap-001", "frame-001"],
        status="running",
    )
    payload = build_segment_payload(
        window,
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(duration=120.0),
    )
    assert "cap-late" not in payload["allowed_evidence_ids"]
    assert "frame-late" not in payload["allowed_evidence_ids"]
    with pytest.raises(SegmentContractError, match="unknown evidence"):
        validate_knowledge_units(
            {"units": [_valid_unit(claims=[{
                "id": "claim-1",
                "text": "引用后段",
                "evidence_ids": ["cap-late"],
                "status": "draft",
                "qualifiers": [],
                "modality": "audio",
                "provenance": "source",
            }])]},
            payload,
        )


def test_temporal_claim_does_not_borrow_sibling_frames():
    transcript = make_transcript([("cap-001", 10.0, 20.0, "然后打开，之后关闭。")], duration=60.0)
    visual = make_visual(
        [("frame-001", 12.5, "scene-001"), ("frame-002", 40.0, "scene-001")],
        duration=60.0,
    )
    payload = build_segment_payload(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
    )
    with pytest.raises(SegmentContractError, match="temporal sequence"):
        validate_knowledge_units(
            {"units": [_valid_unit(
                kind="concept",
                claims=[
                    {
                        "id": "claim-temporal",
                        "text": "然后打开，之后关闭。",
                        "evidence_ids": ["frame-001"],
                        "status": "draft",
                        "qualifiers": [],
                        "modality": "visual",
                        "provenance": "source",
                    },
                    {
                        "id": "claim-other",
                        "text": "标题可见。",
                        "evidence_ids": ["frame-002"],
                        "status": "draft",
                        "qualifiers": [],
                        "modality": "visual",
                        "provenance": "source",
                    },
                ],
            )]},
            payload,
        )


def test_single_cited_frame_temporal_fails_even_if_two_frames_supplied():
    transcript = make_transcript([("cap-001", 10.0, 20.0, "然后打开，之后关闭。")], duration=60.0)
    visual = make_visual(
        [("frame-001", 12.5, "scene-001"), ("frame-002", 40.0, "scene-001")],
        duration=60.0,
    )
    payload = build_segment_payload(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
    )
    assert payload["allowed_frame_ids"] == ["frame-001", "frame-002"]
    with pytest.raises(SegmentContractError, match="temporal sequence"):
        validate_knowledge_units(
            {"units": [_valid_unit(
                kind="concept",
                claims=[{
                    "id": "claim-1",
                    "text": "然后打开，之后关闭。",
                    "evidence_ids": ["frame-001"],
                    "status": "draft",
                    "qualifiers": [],
                    "modality": "visual",
                    "provenance": "source",
                }],
            )]},
            payload,
        )


def test_missing_structured_output_repairs_then_fails_window():
    transcript = make_transcript([("cap-001", 10.0, 20.0, "内容。")], duration=60.0)
    visual = make_visual([("frame-001", 12.5, "scene-001")], duration=60.0)
    provider = FakeProvider(frames_caps(), sequential=[None, None])
    failed = analyze_window(
        _window(),
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(),
        provider=provider,
    )
    assert failed.window.status == "failed"
    assert failed.repaired is True
    assert failed.units == []
    assert len(provider.requests) == 2


def test_mid_analysis_cancel_persists_failed_ledger():
    from threading import Event

    from yt2class.orchestration.scheduler import schedule_windows
    from yt2class.stages.analyze_segments import analyze_segments

    transcript = make_transcript(
        [("cap-001", 0.0, 120.0, "第一段。"), ("cap-002", 120.0, 240.0, "第二段。")],
        duration=240.0,
    )
    visual = make_visual(
        [("frame-001", 30.0, "scene-001"), ("frame-002", 150.0, "scene-001")],
        duration=240.0,
    )
    manifest = schedule_windows(
        source_id="src-demo",
        duration_seconds=240.0,
        transcript=transcript,
        visual=visual,
        capabilities=frames_caps(),
    )
    cancel = Event()

    class OnceThenCancel(FakeProvider):
        def _complete(self, request, *, cancel_event=None):
            result = super()._complete(request, cancel_event=cancel_event)
            cancel.set()
            return result

    provider = OnceThenCancel(
        frames_caps(),
        structured={"units": [_valid_unit()]},
    )
    updated, _units, _outcomes = analyze_segments(
        manifest,
        transcript=transcript,
        visual=visual,
        course_map=sample_course_map(duration=240.0),
        provider=provider,
        cancel_event=cancel,
    )
    statuses = {window.id: window.status for window in updated.windows}
    assert any(status == "complete" for status in statuses.values())
    assert any(
        window.status == "failed" and window.failure_reason == "cancelled"
        for window in updated.windows
    )

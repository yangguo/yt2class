from __future__ import annotations

from yt2class.domain.knowledge import KnowledgeEvidenceRequest, Uncertainty
from yt2class.domain.segment import AnalysisWindow
from yt2class.orchestration.hybrid_analysis import (
    apply_coverage_gaps,
    merge_native_into_unit,
    minimal_clip_range,
)
from tests.helpers.m2 import claim, unit


def test_minimal_clip_range_unions_disjoint_evidence_intervals():
    procedure = unit(
        "unit-proc",
        start=40.0,
        end=70.0,
        kind="procedure",
        claims=[claim("c1", "步骤", ["cap-001"])],
    ).model_copy(
        update={
            "evidence_requests": [
                KnowledgeEvidenceRequest(
                    start_seconds=45.0,
                    end_seconds=50.0,
                    reason="missing_step",
                    desired_modality="clip",
                ),
                KnowledgeEvidenceRequest(
                    start_seconds=60.0,
                    end_seconds=65.0,
                    reason="missing_step",
                    desired_modality="clip",
                ),
            ]
        }
    )
    window = AnalysisWindow(
        id="seg-1",
        core_start_seconds=40.0,
        core_end_seconds=80.0,
        context_start_seconds=35.0,
        context_end_seconds=85.0,
        evidence_ids=["cap-001"],
        status="degraded",
    )
    start, end = minimal_clip_range(procedure, window)
    assert start <= 45.0
    assert end >= 65.0
    assert end - start <= 30.0


def test_merge_native_does_not_promote_unresolved_without_support():
    frame = unit(
        "u1",
        start=10.0,
        end=20.0,
        kind="procedure",
        claims=[claim("claim-1", "步骤一", ["cap-001"], status="unresolved")],
    ).model_copy(
        update={
            "uncertainty": [
                Uncertainty(
                    kind="missing_step",
                    start_seconds=12.0,
                    end_seconds=18.0,
                    note="gap",
                )
            ]
        }
    )
    native = unit(
        "u-native",
        start=10.0,
        end=20.0,
        kind="procedure",
        claims=[claim("claim-1", "native guess", ["cap-002"], status="draft")],
    )
    merged = merge_native_into_unit(frame, [native])
    assert merged.claims[0].status == "unresolved"
    assert merged.uncertainty


def test_apply_coverage_gaps_marks_straddling_unit_unresolved():
    frame = unit(
        "u-span",
        start=25.0,
        end=45.0,
        claims=[claim("claim-1", "步骤跨边界", ["cap-001"], status="draft")],
    )
    merged = apply_coverage_gaps([frame], covered_end=30.0, note="budget cap")
    assert merged[0].claims[0].status == "unresolved"
    assert any(item.start_seconds == 30.0 for item in merged[0].uncertainty)


def test_merge_native_supports_matching_claim():
    frame = unit(
        "u1",
        start=10.0,
        end=20.0,
        kind="procedure",
        claims=[claim("claim-1", "步骤一", ["cap-001"], status="unresolved")],
    )
    native = unit(
        "u-native",
        start=10.0,
        end=20.0,
        kind="procedure",
        claims=[claim("claim-1", "步骤一（清晰）", ["cap-001", "frame-002"], status="supported")],
    )
    merged = merge_native_into_unit(frame, [native])
    assert merged.claims[0].status == "supported"
    assert "frame-002" in merged.claims[0].evidence_ids

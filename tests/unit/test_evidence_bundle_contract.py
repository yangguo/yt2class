from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from yt2class.domain.evidence import EvidenceBundle


FIXTURE = Path(__file__).parents[1] / "fixtures" / "contracts" / "evidence_bundle.valid.json"


def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_evidence_bundle_rejects_transcript_duration_mismatch():
    value = payload()
    value["transcript"]["duration_seconds"] = 59.0
    with pytest.raises(ValueError, match="duration"):
        EvidenceBundle.model_validate(value)


def test_evidence_bundle_rejects_nested_timestamps_past_source_duration():
    value = payload()
    value["transcript"]["duration_seconds"] = 60.0
    value["transcript"]["segments"] = [
        {
            "id": "seg-1",
            "start_seconds": 59.0,
            "end_seconds": 61.0,
            "text_original": "越界",
            "language": "zh-CN",
            "origin": "sidecar",
        }
    ]
    value["transcript"]["raw_artifact_hash"] = "c" * 64
    with pytest.raises(ValueError, match="transcript segment|duration"):
        EvidenceBundle.model_validate(value)

    value = payload()
    value["transcript"]["duration_seconds"] = 60.0
    value["transcript"]["segments"] = [
        {
            "id": "seg-1",
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text_original": "词",
            "language": "zh-CN",
            "origin": "sidecar",
            "words": [
                {"text": "越界", "start_seconds": 1.5, "end_seconds": 61.0}
            ],
        }
    ]
    value["transcript"]["raw_artifact_hash"] = "c" * 64
    with pytest.raises(ValueError, match="word|duration"):
        EvidenceBundle.model_validate(value)

    value = payload()
    value["transcript"]["duration_seconds"] = 60.0
    value["transcript"]["gaps"] = [
        {"id": "gap-1", "start_seconds": 59.0, "end_seconds": 61.0, "reason": "越界"}
    ]
    with pytest.raises(ValueError, match="gap|duration"):
        EvidenceBundle.model_validate(value)


def test_evidence_bundle_rejects_visual_scene_and_bundle_gap_past_duration():
    value = payload()
    value["visual"]["scenes"] = [
        {
            "id": "scene-1",
            "start_seconds": 59.0,
            "end_seconds": 61.0,
            "detector": "content",
        }
    ]
    with pytest.raises(ValueError, match="scene|duration"):
        EvidenceBundle.model_validate(value)

    value = payload()
    value["gaps"].append(
        {
            "id": "gap-outside",
            "modality": "visual",
            "start_seconds": 59.0,
            "end_seconds": 61.0,
            "reason": "outside source",
        }
    )
    with pytest.raises(ValueError, match="gap|duration"):
        EvidenceBundle.model_validate(value)

    value = payload()
    value["visual"]["scenes"] = [
        {"id": "scene-1", "start_seconds": 0.0, "end_seconds": 60.0, "detector": "content"}
    ]
    value["visual"]["assets"] = [
        {
            "id": "asset-frame-1",
            "role": "frame",
            "path": "frames/f1.jpg",
            "sha256": "b" * 64,
            "mime_type": "image/jpeg",
            "width": 1280,
            "height": 720,
        }
    ]
    value["visual"]["occurrences"] = [
        {
            "id": "occ-1",
            "scene_id": "scene-1",
            "asset_id": "asset-frame-1",
            "requested_seconds": 61.0,
            "timestamp_seconds": 59.0,
            "quality": {
                "width": 1280,
                "height": 720,
                "brightness": 0.5,
                "sharpness": 0.5,
                "ocr_density": 0.0,
            },
        }
    ]
    with pytest.raises(ValueError, match="timestamp|duration"):
        EvidenceBundle.model_validate(value)

    value = payload()
    value["visual"]["scenes"] = [
        {"id": "scene-1", "start_seconds": 0.0, "end_seconds": 60.0, "detector": "content"}
    ]
    value["visual"]["gaps"] = [
        {"id": "visual-gap-1", "start_seconds": 59.0, "end_seconds": 61.0, "reason": "越界"}
    ]
    with pytest.raises(ValueError, match="visual gap|duration"):
        EvidenceBundle.model_validate(value)


def test_evidence_bundle_cannot_claim_complete_with_component_gaps():
    value = payload()
    value["status"] = "complete"
    with pytest.raises(ValueError, match="complete"):
        EvidenceBundle.model_validate(value)


def test_complete_bundle_rejects_one_second_transcript_and_empty_scene():
    value = payload()
    value["status"] = "complete"
    value["gaps"] = []
    value["transcript"]["status"] = "complete"
    value["transcript"]["gaps"] = []
    value["transcript"]["duration_seconds"] = 60.0
    value["transcript"]["segments"] = [
        {
            "id": "seg-1s",
            "start_seconds": 0.0,
            "end_seconds": 1.0,
            "text_original": "one second",
            "language": "en",
            "origin": "sidecar",
        }
    ]
    value["transcript"]["raw_artifact_hash"] = "c" * 64
    value["transcript"]["speech_coverage"] = {
        "speech_seconds": 1.0,
        "covered_seconds": 1.0,
        "denominator": "timeline",
        "coverage_ratio": 1.0 / 60.0,
        "denominator_seconds": 60.0,
    }
    value["visual"]["status"] = "complete"
    value["visual"]["scenes"] = [
        {"id": "scene-1", "start_seconds": 0.0, "end_seconds": 60.0, "detector": "content"}
    ]
    value["visual"]["assets"] = []
    value["visual"]["occurrences"] = []
    value["visual"]["ocr_regions"] = []
    value["visual"]["gaps"] = []
    with pytest.raises(ValueError, match="derived|uncovered|complete|occurrence|timeline"):
        EvidenceBundle.model_validate(value)


def test_forged_complete_sparse_transcript_without_visual_fails_validation():
    value = payload()
    value["status"] = "complete"
    value["gaps"] = []
    value["transcript"]["status"] = "complete"
    value["transcript"]["gaps"] = []
    value["transcript"]["segments"] = [
        {
            "id": "seg-sparse",
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text_original": "sparse",
            "language": "en",
            "origin": "sidecar",
        }
    ]
    value["transcript"]["raw_artifact_hash"] = "c" * 64
    value["transcript"]["speech_coverage"] = {
        "speech_seconds": 60.0,
        "covered_seconds": 60.0,
        "denominator": "timeline",
        "coverage_ratio": 1.0,
        "denominator_seconds": 60.0,
    }
    value["transcript"]["duration_seconds"] = 60.0
    value["visual"]["status"] = "complete"
    value["visual"]["scenes"] = []
    value["visual"]["assets"] = []
    value["visual"]["occurrences"] = []
    value["visual"]["ocr_regions"] = []
    value["visual"]["gaps"] = []
    with pytest.raises(ValueError, match="derived|uncovered|complete|timeline"):
        EvidenceBundle.model_validate(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transcript_path", "evidence/missing-transcript.json"),
        ("visual_path", "evidence/missing-visual.json"),
    ],
)
def test_evidence_bundle_paths_must_reference_matching_artifacts(field: str, value: str):
    document = payload()
    document[field] = value
    with pytest.raises(ValueError, match="artifact"):
        EvidenceBundle.model_validate(document)

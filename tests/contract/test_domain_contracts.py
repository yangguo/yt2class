"""Contract tests for 3.0 domain documents, schemas, resolvers, and v2 migration."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from yt2class.domain.common import (
    EXTRA_FIELD_MESSAGE,
    FINITE_NUMBER_MESSAGE,
    HALF_OPEN_INTERVAL_MESSAGE,
    JSON_SCHEMA_DIALECT,
    UNIQUE_ID_MESSAGE,
    published_schema,
)
from yt2class.domain.course_map import CourseMap
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.editorial import EditorialPlan, Omission
from yt2class.domain.knowledge import (
    KnowledgeClaim,
    KnowledgeDocument,
    KnowledgeEvidenceRequest,
    Uncertainty,
)
from yt2class.domain.migration import assess_v2_migration, migrate_v2_to_v3
from yt2class.domain.registry import SCHEMA_DESCRIPTIONS, SCHEMA_MODELS
from yt2class.domain.render_report import RenderReport
from yt2class.domain.resolvers import ClosureError, DocumentBundle, resolve_reference_closure
from yt2class.domain.run_manifest import RunManifest
from yt2class.domain.segment import SegmentManifest
from yt2class.domain.slide_spec_v3 import SlidePage, SlideSpecV3
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import ClaimVerdict, VerificationReport
from yt2class.domain.visual import VisualCatalogue
from yt2class.slide_spec import SlideSpec as SlideSpecV2

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts"
SCHEMAS = ROOT / "schemas"

DOCUMENTS = {
    "source_manifest.valid.json": SourceManifest,
    "evidence_bundle.valid.json": EvidenceBundle,
    "transcript.valid.json": TranscriptDocument,
    "visual_catalogue.valid.json": VisualCatalogue,
    "segment_manifest.valid.json": SegmentManifest,
    "course_map.valid.json": CourseMap,
    "knowledge.valid.json": KnowledgeDocument,
    "editorial_plan.valid.json": EditorialPlan,
    "verification.valid.json": VerificationReport,
    "slide_spec_v3.valid.json": SlideSpecV3,
    "run_manifest.valid.json": RunManifest,
    "render_report.valid.json": RenderReport,
}


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def valid_bundle() -> DocumentBundle:
    return DocumentBundle(
        source=SourceManifest.model_validate(load_json("source_manifest.valid.json")),
        transcript=TranscriptDocument.model_validate(load_json("transcript.valid.json")),
        visual=VisualCatalogue.model_validate(load_json("visual_catalogue.valid.json")),
        segments=SegmentManifest.model_validate(load_json("segment_manifest.valid.json")),
        course_map=CourseMap.model_validate(load_json("course_map.valid.json")),
        knowledge=KnowledgeDocument.model_validate(load_json("knowledge.valid.json")),
        editorial=EditorialPlan.model_validate(load_json("editorial_plan.valid.json")),
        verification=VerificationReport.model_validate(load_json("verification.valid.json")),
        slide_spec=SlideSpecV3.model_validate(load_json("slide_spec_v3.valid.json")),
    )


@pytest.mark.parametrize("filename, model", DOCUMENTS.items())
def test_minimal_valid_fixtures(filename, model):
    model.model_validate(load_json(filename))


def test_synthetic_slide_spec_v3_matches_design_example():
    spec = SlideSpecV3.model_validate(load_json("slide_spec_v3.valid.json"))
    assert spec.schema_version == "3.0"
    assert spec.slides[0].layout == "image-text"
    assert spec.claims[0].evidence_ids == ["ev-frame-1"]


def test_extra_fields_rejected_by_model_and_schema_flag():
    payload = load_json("slide_spec_v3.extra_field.json")
    with pytest.raises(ValidationError, match="unexpected|extra"):
        SlideSpecV3.model_validate(payload)
    schema = json.loads((SCHEMAS / "slide-spec.v3.schema.json").read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert EXTRA_FIELD_MESSAGE  # capability boundary documented for reviewers


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_finite_numbers_rejected(value):
    payload = load_json("slide_spec_v3.valid.json")
    payload["assets"][0]["timestamp_seconds"] = value
    with pytest.raises(ValidationError, match="finite|inf|nan"):
        SlideSpecV3.model_validate(payload)
    assert FINITE_NUMBER_MESSAGE


def test_half_open_intervals_rejected():
    payload = load_json("transcript.valid.json")
    payload["segments"][0]["end_seconds"] = payload["segments"][0]["start_seconds"]
    with pytest.raises(ValidationError, match="half-open"):
        TranscriptDocument.model_validate(payload)
    payload = load_json("slide_spec_v3.valid.json")
    payload["evidence"].append(
        {
            "id": "ev-clip-1",
            "kind": "clip",
            "asset_id": "asset-frame-1",
            "start_seconds": 10.0,
            "end_seconds": 10.0,
        }
    )
    with pytest.raises(ValidationError, match="half-open"):
        SlideSpecV3.model_validate(payload)
    assert "JSON Schema cannot express" in HALF_OPEN_INTERVAL_MESSAGE


def test_unique_ids_rejected():
    payload = load_json("slide_spec_v3.valid.json")
    payload["claims"].append(deepcopy(payload["claims"][0]))
    with pytest.raises(ValidationError, match="duplicate claim"):
        SlideSpecV3.model_validate(payload)
    assert UNIQUE_ID_MESSAGE


def test_unknown_claim_reference_rejected():
    payload = load_json("slide_spec_v3.valid.json")
    payload["slides"][0]["point_claim_ids"] = ["missing-claim"]
    with pytest.raises(ValidationError, match="unknown point_claim_ids"):
        SlideSpecV3.model_validate(payload)


def test_published_schemas_match_models_and_draft_2020_12():
    for filename, model in SCHEMA_MODELS.items():
        stored = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
        assert stored["$schema"] == JSON_SCHEMA_DIALECT
        stored.pop("$schema")
        stored.pop("description")
        assert stored == model.model_json_schema()
        expected = published_schema(model, description=SCHEMA_DESCRIPTIONS[filename])
        expected.pop("$schema")
        expected.pop("description")
        assert expected == model.model_json_schema()


def test_reference_closure_accepts_synthetic_bundle():
    resolve_reference_closure(valid_bundle())


def test_reference_closure_allows_unselected_knowledge_claims():
    bundle = valid_bundle()
    knowledge = bundle.knowledge.model_copy(deep=True)
    knowledge.units[0].claims.append(
        KnowledgeClaim(
            id="claim-omitted",
            text="这个知识点因页面预算未选入讲义。",
            evidence_ids=["cap-001"],
            status="supported",
            modality="audio",
        )
    )
    verification = bundle.verification.model_copy(deep=True)
    verification.verdicts.append(
        ClaimVerdict(
            claim_id="claim-omitted",
            verdict="supported",
            supporting_ids=["cap-001"],
            reason="字幕支持该知识点",
        )
    )
    editorial = bundle.editorial.model_copy(deep=True)
    editorial.omissions.append(
        Omission(claim_id="claim-omitted", reason="页面预算不足，保留在审阅记录中")
    )
    # EditorialPlan selects only claim-1; the omitted knowledge claim is still
    # verified but does not need a SlideSpec claim.
    resolve_reference_closure(
        replace(
            bundle,
            knowledge=knowledge,
            verification=verification,
            editorial=editorial,
        )
    )


def test_reference_closure_rejects_unknown_evidence():
    bundle = valid_bundle()
    broken = bundle.knowledge.model_copy(deep=True)
    broken.units[0].claims[0] = broken.units[0].claims[0].model_copy(
        update={"evidence_ids": ["cap-001", "missing-ev"]}
    )
    with pytest.raises(ClosureError, match="unknown evidence"):
        resolve_reference_closure(
            DocumentBundle(
                source=bundle.source,
                transcript=bundle.transcript,
                visual=bundle.visual,
                segments=bundle.segments,
                course_map=bundle.course_map,
                knowledge=broken,
                editorial=bundle.editorial,
                verification=bundle.verification,
                slide_spec=bundle.slide_spec,
            )
        )


def test_reference_closure_rejects_omitted_slidespec_frame_asset():
    bundle = valid_bundle()
    assert bundle.editorial.pages[1].frame_ids == ["frame-001"]
    assert bundle.visual.occurrences[0].asset_id == "asset-frame-1"
    broken_spec = bundle.slide_spec.model_copy(update={"assets": []}, deep=True)
    assert not any(asset.id == "asset-frame-1" for asset in broken_spec.assets)
    with pytest.raises(ClosureError, match="missing bound frame asset"):
        resolve_reference_closure(
            DocumentBundle(
                source=bundle.source,
                transcript=bundle.transcript,
                visual=bundle.visual,
                segments=bundle.segments,
                course_map=bundle.course_map,
                knowledge=bundle.knowledge,
                editorial=bundle.editorial,
                verification=bundle.verification,
                slide_spec=broken_spec,
            )
        )


def test_reference_closure_rejects_unknown_claim():
    bundle = valid_bundle()
    broken = bundle.editorial.model_copy(deep=True)
    broken.pages[1] = broken.pages[1].model_copy(update={"claim_ids": ["no-such-claim"]})
    with pytest.raises(ClosureError, match="unknown claim"):
        resolve_reference_closure(
            DocumentBundle(
                source=bundle.source,
                transcript=bundle.transcript,
                visual=bundle.visual,
                segments=bundle.segments,
                course_map=bundle.course_map,
                knowledge=bundle.knowledge,
                editorial=broken,
                verification=bundle.verification,
                slide_spec=bundle.slide_spec,
            )
        )


def test_reference_closure_rejects_unknown_verification_evidence():
    bundle = valid_bundle()
    broken = bundle.verification.model_copy(deep=True)
    broken.verdicts[0] = broken.verdicts[0].model_copy(
        update={"supporting_ids": ["missing-evidence"]}
    )
    with pytest.raises(ClosureError, match="verification claim.*unknown evidence"):
        resolve_reference_closure(replace(bundle, verification=broken))


def test_reference_closure_rejects_verdict_mismatch_with_knowledge():
    bundle = valid_bundle()
    broken = bundle.knowledge.model_copy(deep=True)
    broken.units[0].claims[0] = broken.units[0].claims[0].model_copy(
        update={"status": "contradicted"}
    )
    with pytest.raises(ClosureError, match="does not match Knowledge status"):
        resolve_reference_closure(replace(bundle, knowledge=broken))


def test_reference_closure_rejects_verdict_mismatch_with_slidespec():
    bundle = valid_bundle()
    verification = bundle.verification.model_copy(update={"quality_mode": "draft"})
    slide_spec = bundle.slide_spec.model_copy(deep=True)
    slide_spec.quality_status = "review_required"
    slide_spec.claims[0] = slide_spec.claims[0].model_copy(
        update={"verdict": "contradicted"}
    )
    with pytest.raises(ClosureError, match="SlideSpec verdict"):
        resolve_reference_closure(replace(bundle, verification=verification, slide_spec=slide_spec))


def test_reference_closure_rejects_quality_mode_status_mismatch():
    bundle = valid_bundle()
    broken = bundle.slide_spec.model_copy(update={"quality_status": "review_required"})
    with pytest.raises(ClosureError, match="quality_mode.*quality_status"):
        resolve_reference_closure(replace(bundle, slide_spec=broken))


def test_reference_closure_rejects_timestamps_past_source_duration():
    bundle = valid_bundle()

    transcript = bundle.transcript.model_copy(deep=True)
    transcript.segments[0] = transcript.segments[0].model_copy(
        update={"end_seconds": 90.0}
    )
    with pytest.raises(ClosureError, match="transcript segment"):
        resolve_reference_closure(replace(bundle, transcript=transcript))

    visual = bundle.visual.model_copy(deep=True)
    visual.scenes[0] = visual.scenes[0].model_copy(update={"end_seconds": 90.0})
    with pytest.raises(ClosureError, match="scene"):
        resolve_reference_closure(replace(bundle, visual=visual))

    visual = bundle.visual.model_copy(deep=True)
    visual.occurrences[0] = visual.occurrences[0].model_copy(
        update={"timestamp_seconds": 60.0}
    )
    with pytest.raises(ClosureError, match="occurrence.*timestamp"):
        resolve_reference_closure(replace(bundle, visual=visual))

    course_map = bundle.course_map.model_copy(deep=True)
    course_map.topics[0] = course_map.topics[0].model_copy(update={"end_seconds": 90.0})
    with pytest.raises(ClosureError, match="course topic"):
        resolve_reference_closure(replace(bundle, course_map=course_map))

    knowledge = bundle.knowledge.model_copy(deep=True)
    knowledge.units[0].uncertainty = [
        Uncertainty(
            kind="missing_step",
            start_seconds=50.0,
            end_seconds=90.0,
            note="超出视频",
        )
    ]
    with pytest.raises(ClosureError, match="uncertainty"):
        resolve_reference_closure(replace(bundle, knowledge=knowledge))

    knowledge = bundle.knowledge.model_copy(deep=True)
    knowledge.units[0].evidence_requests = [
        KnowledgeEvidenceRequest(
            start_seconds=50.0,
            end_seconds=90.0,
            reason="超出视频",
            desired_modality="frame",
        )
    ]
    with pytest.raises(ClosureError, match="evidence request"):
        resolve_reference_closure(replace(bundle, knowledge=knowledge))


def test_reference_closure_rejects_invalid_transcript_coverage():
    bundle = valid_bundle()
    transcript = bundle.transcript.model_copy(deep=True)
    transcript.speech_coverage = transcript.speech_coverage.model_copy(
        update={"covered_seconds": 30.0, "speech_seconds": 20.0}
    )
    with pytest.raises(ClosureError, match="covered duration exceeds speech"):
        resolve_reference_closure(replace(bundle, transcript=transcript))


def test_slidespec_rejects_inapplicable_page_fields():
    payload = load_json("slide_spec_v3.valid.json")
    payload["slides"][0]["hero_asset_id"] = "asset-frame-1"
    with pytest.raises(ValidationError, match="does not allow field 'hero_asset_id'"):
        SlideSpecV3.model_validate(payload)

    payload = load_json("slide_spec_v3.valid.json")
    payload["slides"][0].update(
        {"type": "cover", "source_id": "src-demo", "layout": None}
    )
    with pytest.raises(ValidationError, match="does not allow field 'point_claim_ids'"):
        SlideSpecV3.model_validate(payload)


def test_sequence_layout_uses_steps_without_point_claims():
    payload = load_json("slide_spec_v3.valid.json")
    payload["slides"][0].update(
        {
            "layout": "sequence",
            "point_claim_ids": [],
            "frame_asset_ids": [],
            "steps": [
                {"asset_id": "asset-frame-1", "claim_ids": ["claim-1"]},
                {"asset_id": "asset-frame-1", "claim_ids": ["claim-1"]},
            ],
        }
    )
    spec = SlideSpecV3.model_validate(payload)
    assert spec.slides[0].layout == "sequence"
    assert len(spec.slides[0].steps) == 2


def test_sequence_layout_rejects_point_claims():
    payload = load_json("slide_spec_v3.valid.json")
    payload["slides"][0].update(
        {
            "layout": "sequence",
            "point_claim_ids": ["claim-1"],
            "frame_asset_ids": [],
            "steps": [
                {"asset_id": "asset-frame-1", "claim_ids": ["claim-1"]},
                {"asset_id": "asset-frame-1", "claim_ids": ["claim-1"]},
            ],
        }
    )
    with pytest.raises(ValidationError, match="sequence layout must use steps"):
        SlideSpecV3.model_validate(payload)


def test_slidespec_rejects_wrong_asset_roles_and_time_binding():
    payload = load_json("slide_spec_v3.valid.json")
    payload["assets"].append(
        {
            "id": "asset-clip",
            "role": "clip",
            "path": "clips/c1.mp4",
            "sha256": "d" * 64,
            "mime_type": "video/mp4",
            "timestamp_seconds": None,
            "start_seconds": 10.0,
            "end_seconds": 20.0,
            "derived_from": None,
            "transformations": [],
        }
    )
    payload["slides"][0]["frame_asset_ids"] = ["asset-clip"]
    with pytest.raises(ValidationError, match="must have role 'frame'"):
        SlideSpecV3.model_validate(payload)

    payload["slides"][0].update(
        {
            "layout": "sequence",
            "point_claim_ids": [],
            "frame_asset_ids": [],
            "steps": [
                {"asset_id": "asset-clip", "claim_ids": ["claim-1"]},
                {"asset_id": "asset-clip", "claim_ids": ["claim-1"]},
            ],
        }
    )
    with pytest.raises(ValidationError, match="sequence asset.*role 'frame'"):
        SlideSpecV3.model_validate(payload)

    payload = load_json("slide_spec_v3.valid.json")
    payload["assets"].append(
        {
            "id": "asset-clip",
            "role": "clip",
            "path": "clips/c1.mp4",
            "sha256": "d" * 64,
            "mime_type": "video/mp4",
            "timestamp_seconds": None,
            "start_seconds": 10.0,
            "end_seconds": 20.0,
            "derived_from": None,
            "transformations": [],
        }
    )
    payload["slides"].insert(
        0,
        {
            "id": "cover-1",
            "type": "cover",
            "title": "封面",
            "source_id": "src-demo",
            "hero_asset_id": "asset-clip",
        },
    )
    with pytest.raises(ValidationError, match="hero asset.*role 'frame'"):
        SlideSpecV3.model_validate(payload)

    payload = load_json("slide_spec_v3.valid.json")
    payload["evidence"][0]["timestamp_seconds"] = 13.0
    with pytest.raises(ValidationError, match="timestamp must match frame asset"):
        SlideSpecV3.model_validate(payload)

    payload = load_json("slide_spec_v3.valid.json")
    payload["evidence"].append(
        {
            "id": "ev-transcript-1",
            "kind": "transcript",
            "asset_id": "asset-frame-1",
            "start_seconds": 10.0,
            "end_seconds": 20.0,
            "text": "字幕",
            "origin": "sidecar",
        }
    )
    with pytest.raises(ValidationError, match="transcript asset"):
        SlideSpecV3.model_validate(payload)


def test_slidepage_validates_sequence_and_cover_asset_roles():
    with pytest.raises(ValidationError, match="does not allow field 'point_claim_ids'"):
        SlidePage(
            id="cover",
            type="cover",
            title="封面",
            source_id="src-demo",
            point_claim_ids=["claim-1"],
        )


def test_v2_contract_still_independent():
    v2 = json.loads((ROOT / "docs/examples/slide-spec.v2.json").read_text(encoding="utf-8"))
    SlideSpecV2.model_validate(v2)


def test_v2_migration_report_forbids_supported_upgrade():
    v2 = json.loads((ROOT / "docs/examples/slide-spec.v2.json").read_text(encoding="utf-8"))
    report = assess_v2_migration(v2)
    assert report.can_mark_supported is False
    assert report.allowed_quality_status == "evidence-only"
    assert any(issue.kind == "missing_claim_ref" for issue in report.issues)
    migrated_report, spec = migrate_v2_to_v3(v2)
    assert migrated_report.can_mark_supported is False
    assert spec.quality_status == "evidence-only"
    assert spec.schema_version == "3.0"
    assert all(claim.verdict != "supported" for claim in spec.claims)
    with pytest.raises(ValidationError, match="supported"):
        SlideSpecV3.model_validate(spec.model_dump() | {"quality_status": "verified"})
    assert not any(item.kind == "transcript" for item in spec.evidence)
    assert any(issue.kind == "unresolved" and "transcript" in issue.message for issue in migrated_report.issues)


def test_v2_migration_does_not_reuse_source_hash_for_other_roles():
    v2 = json.loads((ROOT / "docs/examples/slide-spec.v2.json").read_text(encoding="utf-8"))
    _, spec = migrate_v2_to_v3(v2)
    for asset in spec.assets:
        if asset.sha256 == spec.source.sha256:
            assert asset.path == spec.source.media_path
            assert asset.role != "transcript"
    assert all(asset.role != "transcript" for asset in spec.assets)
    assert spec.source.sha256 not in {asset.sha256 for asset in spec.assets}

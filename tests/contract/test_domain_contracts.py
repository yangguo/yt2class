"""Contract tests for 3.0 domain documents, schemas, resolvers, and v2 migration."""

from __future__ import annotations

import json
from copy import deepcopy
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
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.migration import assess_v2_migration, migrate_v2_to_v3
from yt2class.domain.registry import SCHEMA_DESCRIPTIONS, SCHEMA_MODELS
from yt2class.domain.render_report import RenderReport
from yt2class.domain.resolvers import ClosureError, DocumentBundle, resolve_reference_closure
from yt2class.domain.run_manifest import RunManifest
from yt2class.domain.segment import SegmentManifest
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import VisualCatalogue
from yt2class.slide_spec import SlideSpec as SlideSpecV2

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts"
SCHEMAS = ROOT / "schemas"

DOCUMENTS = {
    "source_manifest.valid.json": SourceManifest,
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
